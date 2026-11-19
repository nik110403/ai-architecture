import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import CLI, ContCase, L, alive, dispatch, jobs, reconcile, wait_for  # noqa: E402

SLEEP_WITH_CHILDREN = (
    "import subprocess\n"
    "kid = subprocess.Popen(['sleep', '60'])\n"
    "esc = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
    "open(os.path.join(here, 'kids'), 'w').write('%d %d' % (kid.pid, esc.pid))\n"
    "time.sleep(60)"
)

# A worker that runs until the test creates `go` beside it (a sync point instead of a fixed sleep).
GATED = (
    "while not os.path.exists(os.path.join(here, 'go')):\n"
    "    time.sleep(0.05)\n"
    "print('done')"
)


class Seams(ContCase):
    def opts(self, **over):
        base = dict(config=self.cfg, role="researcher", intent="x", prompt_file=None, model=None, allow_spend=False,
                    workdir=str(self.dir), session=None, budget_tokens=None, job_id=None)
        base.update(over)
        return SimpleNamespace(**base)

    def set_config(self, **over):
        path = self.state / "config.json"
        config = json.loads(path.read_text())
        config.update(over)
        path.write_text(json.dumps(config))

    def kill_dispatcher(self, job_id):
        pid = self.job(job_id)["dispatcher"]["pid"]
        os.kill(pid, signal.SIGKILL)
        self.assertTrue(wait_for(lambda: not alive(pid)), "dispatcher did not die")

    def make_submitter_dead(self, job_id):
        dead = self.dead_pid()
        self.edit(lambda d: d["jobs"][job_id]["submitter"].update(pid=dead, start_ticks=1))

    def kids(self):
        path = self.bin / "kids"
        return [int(x) for x in path.read_text().split()] if path.exists() else []


class RunTests(Seams):
    def test_run_banks_a_succeeded_job_with_the_exit_code(self):
        code, out = self.run_job()
        self.assertEqual((code, out["outcome"], out["cause"], out["state"]), (0, "succeeded", "completed", "banked"))
        job = self.job(out["job_id"])
        result = job["result"]
        self.assertEqual((result["source"], result["exit_code"], result["trust"]), ("wait", 0, "dispatcher"))
        self.assertEqual(result["record"]["event_id"], job["job_id"])
        self.assertRegex(result["result_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["bank_revision"], self.doc()["revision"])
        self.assertIsNone(job["acknowledged_revision"])
        self.assertEqual(job["dispatcher"]["start_ticks"], dispatch.proc_start_ticks(job["dispatcher"]["pid"]) or
                         job["dispatcher"]["start_ticks"])

    def test_failed_worker_exits_1(self):
        self.fake("worker", "sys.exit(3)")
        code, out = self.run_job()
        self.assertEqual((code, out["outcome"], out["cause"]), (1, "failed", "completed"))

    def test_dispatcher_refusal_is_banked_unsupported_with_its_reason(self):
        code, out = self.cli("run", *self.launch_args(cfg=self.config(permit=[])))
        self.assertEqual((code, out["outcome"], out["cause"]), (2, "unsupported", "refused"))
        job = self.job(out["job_id"])
        self.assertIn("not permitted", job["result"]["note"])
        self.assertIsNone(job["result"]["record"])
        self.assertFalse((self.state / "runs" / out["job_id"] / "result.json").exists())

    def test_missing_dispatcher_executable_banks_not_launched(self):
        with mock.patch.object(jobs, "PYTHON", str(self.dir / "no-such-python")):
            code, out = self.inproc("start", *self.launch_args())
        self.assertEqual((code, out["cause"]), (1, "not_launched"), out)
        job = self.job(out["job_id"])
        self.assertEqual((job["state"], job["result"]["outcome"]), ("banked", "failed"))
        self.assertIn("could not be started", job["result"]["note"])

    def test_write_job_is_worker_attested_and_keeps_its_branch(self):
        repo = self.repo()
        cfg = self.config(permit=["builder"], allow_write=True, worktree_root=str(self.dir / "wt"))
        code, out = self.cli("run", "--config", cfg, "--role", "builder", "--intent", "x", "--workdir", str(repo))
        self.assertEqual((code, out["outcome"]), (0, "succeeded"), out)
        job = self.job(out["job_id"])
        self.assertEqual((job["access"], job["result"]["trust"]), ("write", "worker-attested"))
        self.assertEqual(job["worktree_plan"]["path"], str(self.dir / "wt" / job["job_id"]))
        self.assertTrue(job["result"]["worktree"]["kept"])
        self.assertEqual(job["workdir"], str(repo))

    def test_prompt_file_and_budget_are_recorded(self):
        prompt = self.dir / "p.txt"
        prompt.write_text("hello")
        code, out = self.cli("run", "--config", self.cfg, "--role", "researcher", "--prompt-file", str(prompt),
                             "--workdir", str(self.dir), "--budget-tokens", "500", "--session", "cli:abc")
        self.assertEqual(code, 0, out)
        job = self.job(out["job_id"])
        self.assertEqual((job["budget"], job["owner"]), ({"tokens": 500}, {"harness": "cli", "session_id": "abc"}))
        self.assertEqual(job["prompt_sha256"], L.sha256_hex(b"hello"))
        self.assertEqual((self.state / "spool" / f"{job['job_id']}.prompt").read_text(), "hello")

    def test_start_returns_at_once_and_records_the_dispatcher(self):
        job_id = self.start_sleeper()
        job = self.job(job_id)
        disp = job["dispatcher"]
        self.assertEqual(job["state"], "launched")
        self.assertEqual(disp["start_ticks"], dispatch.proc_start_ticks(disp["pid"]))
        self.assertEqual(disp["boot_id"], jobs.boot_id())
        self.assertLess(disp["launched_monotonic"], jobs.monotonic())
        self.assertEqual(os.getpgid(disp["pid"]), disp["pid"], "the dispatcher must lead its own session")
        self.assertEqual(os.getsid(disp["pid"]), disp["pid"])
        self.assertEqual(stat_mode(self.state / "spool" / f"{job_id}.out"), 0o600)

    def test_admission_refusals(self):
        code, out = self.start(intent="x", role="nope")
        self.assertEqual(code, 2, out)
        self.set_config(max_active_jobs=1)
        self.start_sleeper()
        code, out = self.start()
        self.assertEqual(code, 2, out)
        self.assertIn("max_active_jobs", out["error"])
        self.set_config(max_active_jobs=8)
        code, first = self.start("--job-id", "dup-1")
        self.assertEqual(code, 0, first)
        code, out = self.start("--job-id", "dup-1")
        self.assertEqual(code, 2, out)
        self.assertIn("already exists", out["error"])
        (self.state / "runs" / "dir-1").mkdir()
        code, out = self.start("--job-id", "dir-1")
        self.assertEqual(code, 2, out)
        code, out = self.start("--job-id", "bad id!")
        self.assertEqual(code, 2, out)

    def test_late_results_are_arrivals_for_a_later_collect(self):
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "result.json").exists()))
        self.assertEqual(self.job(job_id)["state"], "launched")  # nobody was watching
        code, out = self.cli("collect")
        self.assertEqual((code, out["total"], out["arrivals"][0]["job_id"]), (0, 1, job_id))
        self.assertEqual(self.job(job_id)["result"]["source"], "file")


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


class ExactlyOnceTests(Seams):
    def test_run_status_collect_wait_reconcile_never_change_the_bank(self):
        code, out = self.run_job()
        job_id = out["job_id"]
        before = self.job(job_id)
        for argv in (["status"], ["collect"], ["wait", "--job", job_id], ["reconcile"], ["status", "--job", job_id]):
            code, got = self.cli(*argv)
            self.assertIn(code, (0, 1), (argv, got))
        after = self.job(job_id)
        self.assertEqual(after["result"], before["result"])
        self.assertEqual(after["result"]["bank_revision"], before["result"]["bank_revision"])
        self.assertEqual(after["post_bank"], [])

    def test_a_forged_second_result_lands_in_post_bank_only(self):
        code, out = self.run_job()
        job_id = out["job_id"]
        before = self.job(job_id)["result"]
        path = self.state / "runs" / job_id / "result.json"
        forged = json.loads(path.read_text())
        forged["result"]["outcome"] = "failed"
        path.write_text(json.dumps(forged))
        for _ in range(2):
            self.cli("status")
        job = self.job(job_id)
        self.assertEqual(job["result"], before)
        self.assertEqual([e["outcome"] for e in job["post_bank"]], ["failed"])
        self.assertEqual(job["post_bank"][0]["sha256"], L.sha256_hex(path.read_bytes()))
        path.write_text("not json")
        self.cli("status")
        self.assertEqual(len(self.job(job_id)["post_bank"]), 2)

    def test_bank_in_one_transaction_returns_banked_now_once(self):
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "result.json").exists()))
        ctx = self.ctx()
        state, obs = jobs.read_result_file(ctx, ctx.ledger.read()["jobs"][job_id])
        self.assertEqual(state, "valid")
        first, second = jobs.bank(ctx.ledger, job_id, obs), jobs.bank(ctx.ledger, job_id, obs)
        self.assertEqual((first["banked_now"], second["banked_now"]), (True, False))
        self.assertEqual(first["outcome"], second["outcome"])  # both come from the committed result
        self.assertEqual(self.job(job_id)["post_bank"], [])

    def test_a_timed_out_or_killed_success_banks_failed(self):
        ctx = self.ctx()
        code, out = self.run_job()
        job = self.job(out["job_id"])
        envelope = json.loads((self.state / "runs" / job["job_id"] / "result.json").read_text())
        for field, value in (("timed_out", True), ("exit_code", -9)):
            env = json.loads(json.dumps(envelope))
            env["dispatch"][field] = value
            obs = jobs.observation_from_envelope(job, env, "0" * 64, "file")
            self.assertEqual(obs["outcome"], "failed", field)
        env = json.loads(json.dumps(envelope))
        env["result"]["event_id"] = "another-job"
        self.assertIsNone(jobs.observation_from_envelope(job, env, "0" * 64, "file"))

    def test_failed_banking_keeps_the_job_launched_then_banks_once(self):
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "result.json").exists()))
        before = self.ledger_bytes()
        (self.state / "ledger.json.tmp").mkdir()
        code, got = self.cli("wait", "--job", job_id, "--timeout", "5")
        self.assertEqual(code, 4, got)
        self.assertEqual(self.ledger_bytes(), before)
        (self.state / "ledger.json.tmp").rmdir()
        self.assertEqual(self.job(job_id)["state"], "launched")
        code, got = self.cli("wait", "--job", job_id, "--timeout", "10")
        self.assertEqual((code, got["outcome"]), (0, "succeeded"))
        banked = self.job(job_id)["result"]["bank_revision"]
        self.cli("wait", "--job", job_id)
        self.assertEqual(self.job(job_id)["result"]["bank_revision"], banked)

    def test_failed_start_leaves_no_dispatcher_and_no_prompt(self):
        before = self.ledger_bytes()
        (self.state / "ledger.json.tmp").mkdir()
        code, out = self.start()
        self.assertEqual(code, 4, out)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(list((self.state / "spool").iterdir()), [], "the prompt spool was not removed")
        self.assertEqual(list((self.state / "runs").iterdir()), [], "a dispatcher was launched")

    def test_run_final_bank_under_a_held_lock_exits_6_and_status_banks_once(self):
        self.set_config(lock_wait_seconds=0.5)
        self.fake("worker", "time.sleep(2)")
        proc = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "run", "--project", str(self.project), *self.launch_args()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        self.addCleanup(self.reap_proc, proc)
        self.assertTrue(wait_for(lambda: any(j["state"] == "launched" for j in self.doc()["jobs"].values())))
        fd = os.open(self.state / "ledger.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            out, err = proc.communicate(timeout=60)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(proc.returncode, 6, (out, err))
        payload = json.loads(out)
        self.assertEqual(set(payload), {"job_id", "state", "dispatcher_pid", "result_path", "changed"})
        self.assertEqual(self.job(payload["job_id"])["state"], "launched")
        code, got = self.cli("status")
        self.assertEqual(code, 0, got)
        banked = self.job(payload["job_id"])
        self.assertEqual((banked["state"], banked["result"]["outcome"], banked["result"]["source"]),
                         ("banked", "succeeded", "file"))
        self.cli("status")
        self.assertEqual(self.job(payload["job_id"])["result"], banked["result"])

    def test_start_step_three_failure_is_exit_6_and_reconcile_adopts(self):
        self.fake("worker", "time.sleep(2)")
        real = jobs.spawn

        def spawn_then_break_the_ledger(ctx, opts, job_id):
            proc = real(ctx, opts, job_id)
            (ctx.state / "ledger.json.tmp").mkdir()  # makes the next write fail, root included
            return proc

        with mock.patch.object(jobs, "spawn", spawn_then_break_the_ledger):
            code, out = self.inproc("start", *self.launch_args())
        (self.state / "ledger.json.tmp").rmdir()
        self.assertEqual((code, out["state"]), (6, "launching"), out)
        self.assertIn("dispatcher_pid", out)
        job_id = out["job_id"]
        self.assertEqual(self.job(job_id)["state"], "launching")
        self.make_submitter_dead(job_id)
        code, got = self.cli("reconcile")
        self.assertEqual(code, 0, got)
        job = self.job(job_id)
        self.assertEqual((job["state"], job["dispatcher"]["pid"]), ("launched", out["dispatcher_pid"]))
        code, got = self.cli("wait", "--job", job_id, "--timeout", "30")
        self.assertEqual((code, got["outcome"]), (0, "succeeded"))


class CancelTests(Seams):
    def test_launching_with_the_submitter_gone_and_no_process_is_cancelled(self):
        job_id = jobs.admit(self.ctx(), self.opts())
        self.make_submitter_dead(job_id)
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual((code, out["state"], out["outcome"], out["cause"]), (0, "banked", "failed", "cancelled"))
        self.assertIn("not_launched", self.job(job_id)["result"]["note"])

    def test_launching_with_a_live_submitter_only_records_the_request(self):
        job_id = jobs.admit(self.ctx(), self.opts())
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual((code, out["state"], out["cancel"]), (0, "launching", "requested"))
        self.assertIsNotNone(self.job(job_id)["cancel"])

    def test_launching_with_the_submitter_gone_and_a_live_dispatcher_adopts_and_signals_it(self):
        self.fake("worker", "time.sleep(60)")
        ctx = self.ctx()
        job_id = jobs.admit(ctx, self.opts())
        proc = jobs.spawn(ctx, self.opts(), job_id)  # the submitter "dies" before step 3
        self.addCleanup(self.reap_proc, proc)
        self.make_submitter_dead(job_id)
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual((code, out["state"], out["cause"]), (0, "banked", "cancelled"), out)
        job = self.job(job_id)
        self.assertEqual(job["dispatcher"]["pid"], proc.pid, "the dispatcher must be adopted, not just banked over")
        proc.wait(timeout=30)
        self.assertNotEqual(proc.returncode, None)

    def test_cancelling_a_running_job_stops_the_worker_and_its_children(self):
        self.fake("worker", SLEEP_WITH_CHILDREN)
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.kids() and self.start_json(job_id).get("state") == "running"))
        kid, escaped = self.kids()
        pid = self.job(job_id)["dispatcher"]["pid"]
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual((code, out["outcome"], out["cause"]), (0, "failed", "cancelled"), out)
        self.assertFalse(alive(pid))
        self.assertTrue(wait_for(lambda: not alive(kid)), "background child survived")
        self.assertTrue(wait_for(lambda: not alive(escaped)), "setsid child survived")
        killed, _ = dispatch.sweep_marker(dispatch.job_marker(self.state / "runs" / job_id))
        self.assertEqual(killed, set())
        result = self.job(job_id)["result"]
        self.assertEqual(result["source"], "file")
        self.assertIsNotNone(result["record"], "banked from the dispatcher's own result.json")

    def test_cancel_after_completion_stays_succeeded(self):
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "result.json").exists()))
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual((code, out["outcome"]), (0, "succeeded"), out)
        self.assertEqual(self.job(job_id)["result"]["note"], "cancel arrived after completion")

    def test_cancel_by_another_owner_is_refused_and_force_works(self):
        job_id = self.start_sleeper_for("claude-code:aaa")
        code, out = self.cli("cancel", "--job", job_id, "--session", "opencode:bbb")
        self.assertEqual(code, 2, out)
        self.assertIn("claude-code:aaa", out["error"])
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual(code, 2, out)
        self.assertTrue(alive(self.job(job_id)["dispatcher"]["pid"]))
        code, out = self.cli("cancel", "--job", job_id, "--force")
        self.assertEqual((code, out["cause"]), (0, "cancelled"), out)

    def test_the_owner_session_can_cancel(self):
        job_id = self.start_sleeper_for("opencode:me")
        code, out = self.cli("cancel", "--job", job_id, "--session", "opencode:me")
        self.assertEqual((code, out["cause"]), (0, "cancelled"), out)

    def start_sleeper_for(self, session):
        self.fake("worker", "time.sleep(60)")
        code, out = self.start("--session", session)
        self.assertEqual(code, 0, out)
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "running"))
        return job_id

    def test_cancelling_one_of_two_jobs_leaves_the_other_to_finish(self):
        self.fake("worker", GATED)  # held open until the test releases it: no wall-clock race with the cancel
        _, a = self.start()
        _, b = self.start()
        for job_id in (a["job_id"], b["job_id"]):
            self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "running"))
        code, out = self.cli("cancel", "--job", a["job_id"])
        self.assertEqual(out["cause"], "cancelled", out)
        (self.bin / "go").write_text("")
        code, out = self.cli("wait", "--job", b["job_id"], "--timeout", "30")
        self.assertEqual((code, out["outcome"]), (0, "succeeded"), out)

    def test_cancel_of_a_banked_job_is_a_no_op(self):
        code, out = self.run_job()
        code, got = self.cli("cancel", "--job", out["job_id"])
        self.assertEqual((code, got), (0, {"job_id": out["job_id"], "already_banked": True}))

    def test_cancel_of_an_unknown_job_is_refused(self):
        code, out = self.cli("cancel", "--job", "nope")
        self.assertEqual(code, 2, out)


class CrashTests(Seams):
    def test_killing_the_coordinator_loses_nothing(self):
        self.fake("worker", GATED)
        proc = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "run", "--project", str(self.project), *self.launch_args()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        self.addCleanup(self.reap_proc, proc)
        self.assertTrue(wait_for(lambda: any(j["state"] == "launched" for j in self.doc()["jobs"].values())))
        job_id = next(iter(self.doc()["jobs"]))
        pid = self.job(job_id)["dispatcher"]["pid"]
        proc.kill()
        proc.wait()
        self.assertTrue(alive(pid), "the dispatcher must survive its coordinator")
        (self.bin / "go").write_text("")
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "result.json").exists(), 30))
        code, out = self.cli("status")
        job = self.job(job_id)
        self.assertEqual((job["state"], job["result"]["outcome"], job["result"]["source"]),
                         ("banked", "succeeded", "file"))
        self.assertIsNone(job["result"]["exit_code"])

    def test_dispatcher_killed_while_the_worker_runs_is_orphaned_and_cleaned_up(self):
        self.fake("worker", SLEEP_WITH_CHILDREN)
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.kids() and self.start_json(job_id).get("state") == "running"))
        kid, escaped = self.kids()
        worker = self.start_json(job_id)["pid"]
        self.kill_dispatcher(job_id)
        self.assertTrue(alive(worker))
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"], result["trust"]), ("failed", "orphaned", "recovery"))
        for pid in (worker, kid, escaped):
            self.assertTrue(wait_for(lambda: not alive(pid)), f"{pid} survived")

    def test_dispatcher_and_worker_killed_is_lost_with_the_stdout_copy_as_evidence(self):
        self.fake("worker", "print('partial output', flush=True)\ntime.sleep(60)")
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "running"))
        self.kill_dispatcher(job_id)
        worker = self.start_json(job_id)["pid"]
        os.killpg(self.start_json(job_id)["pgid"], signal.SIGKILL)
        self.assertTrue(wait_for(lambda: not alive(worker)))
        code, out = self.cli("reconcile")
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"]), ("failed", "lost"))
        self.assertEqual(result["evidence"][0]["kind"], "stdout-copy-unverified")
        self.assertEqual(result["evidence"][0]["sha256"], jobs.sha_file(Path(result["evidence"][0]["reference"])))
        self.assertIn("unverified copy", result["note"])

    def test_a_dead_submitter_between_the_two_transactions(self):
        self.fake("worker", "time.sleep(60)")
        ctx = self.ctx()
        job_id = jobs.admit(ctx, self.opts())
        proc = jobs.spawn(ctx, self.opts(), job_id)
        self.addCleanup(self.reap_proc, proc)
        self.make_submitter_dead(job_id)
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        job = self.job(job_id)
        self.assertEqual((job["state"], job["dispatcher"]["pid"]), ("launched", proc.pid))
        self.assertEqual(job["dispatcher"]["boot_id"], jobs.boot_id())

    def test_a_dead_submitter_before_any_dispatcher_is_not_launched(self):
        job_id = jobs.admit(self.ctx(), self.opts())
        self.make_submitter_dead(job_id)
        code, out = self.cli("reconcile")
        self.assertEqual(self.job(job_id)["state"], "launching", "inside the launch grace the job is left")
        self.edit(lambda d: d["jobs"][job_id].update(created_at="2020-01-01T00:00:00Z"))
        code, out = self.cli("reconcile")
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"]), ("failed", "not_launched"))

    def test_a_live_submitter_is_left_alone(self):
        self.set_config(launch_grace_seconds=0.2)
        job_id = jobs.admit(self.ctx(), self.opts())
        time.sleep(0.4)
        self.cli("reconcile")
        self.assertEqual(self.job(job_id)["state"], "launching")


class DrainTests(Seams):
    def test_drain_refuses_new_work_waits_for_the_running_job_and_ends(self):
        self.fake("worker", "time.sleep(2)")
        code, out = self.start()
        job_id = out["job_id"]
        code, out = self.cli("drain", "start", "--reason", "restart")
        self.assertEqual((code, out["drain"]["reason"]), (0, "restart"))
        code, out = self.start()
        self.assertEqual(code, 2, out)
        self.assertIn("draining", out["error"])
        code, out = self.cli("drain", "status")
        self.assertEqual(out["pending"], [job_id])
        code, out = self.cli("drain", "wait", "--timeout", "60")
        self.assertEqual((code, out["state"]), (0, "DRAINED"), out)
        self.assertEqual(self.job(job_id)["state"], "banked")
        self.assertIsNotNone(self.doc()["drain"], "the flag stays until drain end")
        code, out = self.cli("drain", "end")
        self.assertEqual((code, out["drain"]), (0, None))
        code, out = self.run_job()
        self.assertEqual(code, 0, out)

    def test_drain_wait_times_out_with_exit_5_and_lists_the_pending_ids(self):
        job_id = self.start_sleeper()
        self.cli("drain", "start", "--reason", "x")
        code, out = self.cli("drain", "wait", "--timeout", "1")
        self.assertEqual((code, out["pending"]), (5, [job_id]))

    def test_a_skipped_reconcile_is_never_progress(self):
        job_id = self.start_sleeper()
        self.cli("drain", "start", "--reason", "x")
        fd = os.open(self.state / "reconcile.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.kill_dispatcher(job_id)
            os.killpg(self.start_json(job_id)["pgid"], signal.SIGKILL)
            code, out = self.cli("drain", "wait", "--timeout", "1")
        finally:
            os.close(fd)
        self.assertEqual((code, out["state"], out["pending"]), (5, "PENDING", [job_id]))
        code, out = self.cli("drain", "wait", "--timeout", "30")
        self.assertEqual((code, out["state"]), (0, "DRAINED"))

    def test_drain_start_needs_a_reason(self):
        code, out = self.cli("drain", "start")
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
