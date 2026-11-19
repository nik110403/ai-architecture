"""R9-U04a fix rounds 1 and 2: tests for the review findings (S1-S3, the SEV3s, the mutants D/L/N, A1-A3 and the round-2 SEV3s)."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import CLI, L, alive, dispatch, git_env, jobs, reconcile, wait_for  # noqa: E402
from test_jobs import SLEEP_WITH_CHILDREN, Seams  # noqa: E402
from test_reconcile import FAKE_GIT  # noqa: E402

WORKER_PAUSE = "time.sleep(1.5)\nprint('done')"


class Lookalikes(Seams):
    """Processes whose argv looks like a dispatcher for a job."""

    def launching(self, cancel=False):
        job_id = jobs.admit(self.ctx(), self.opts())
        self.make_submitter_dead(job_id)
        if cancel:
            self.edit(lambda d: d["jobs"][job_id].update(
                cancel={"requested_at": L.now(), "by": {"harness": "cli", "session_id": None}}))
        return job_id

    def lookalike(self, job_id, new_session=True, argv=None):
        argv = argv or [str(jobs.DISPATCH), "run", "--job-id", job_id, "--run-dir", str(self.state / "runs")]
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv],
                                start_new_session=new_session)
        self.addCleanup(self.reap_proc, proc)
        self.assertTrue(wait_for(lambda: reconcile.cmdline(proc.pid) and len(reconcile.cmdline(proc.pid)) > 3))
        return proc

    def drifting_ticks(self, pid, after_calls):
        """stat_fields whose start ticks change from call number `after_calls`+1 on, for `pid` only (a recycled pid)."""
        real, seen = reconcile.stat_fields, {"n": 0}

        def stat_fields(other):
            fields = real(other)
            if fields and other == pid:
                seen["n"] += 1
                if seen["n"] > after_calls:
                    fields = dict(fields, ticks=fields["ticks"] + 1000)
            return fields
        return mock.patch.object(reconcile, "stat_fields", stat_fields)


class AdoptionIdentity(Lookalikes):
    """S1: adoption and cancel never act on a pid that is not provably the recorded process."""

    def test_a_recycled_pid_is_neither_adopted_nor_signalled(self):
        job_id = self.launching(cancel=True)
        proc = self.lookalike(job_id)
        for after_calls in (1, 2):  # ticks differ between the two reads of the match / inside the transaction
            with self.drifting_ticks(proc.pid, after_calls):
                self.assertIsNone(reconcile.adopt_launching(self.ctx(), job_id), after_calls)
            time.sleep(0.3)
            self.assertIsNone(proc.poll(), f"a SIGTERM reached a process that was not the dispatcher ({after_calls})")
            job = self.job(job_id)
            self.assertEqual((job["state"], job["dispatcher"]), ("launching", None))

    def test_the_stored_record_is_the_one_captured_at_the_match_and_the_cancel_signals_it(self):
        job_id = self.launching(cancel=True)
        proc = self.lookalike(job_id)
        ticks = dispatch.proc_start_ticks(proc.pid)
        self.assertEqual(reconcile.adopt_launching(self.ctx(), job_id), proc.pid)
        disp = self.job(job_id)["dispatcher"]
        self.assertEqual((disp["pid"], disp["start_ticks"]), (proc.pid, ticks))
        self.assertEqual(self.job(job_id)["state"], "launched")
        proc.wait(timeout=20)
        self.assertEqual(proc.returncode, -signal.SIGTERM)

    def test_a_fork_child_before_exec_is_not_adopted(self):
        job_id = self.launching(cancel=True)
        proc = self.lookalike(job_id, new_session=False)  # same argv, but pid != pgid
        self.assertIsNone(reconcile.adopt_launching(self.ctx(), job_id))
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.job(job_id)["state"], "launching")

    def test_the_argv_match_wants_the_dispatcher_path_followed_by_run(self):
        job_id = self.launching(cancel=True)
        runs = str(self.state / "runs")
        for argv in (["--job-id", job_id, "--run-dir", runs],
                     [str(jobs.DISPATCH), "plan", "--job-id", job_id, "--run-dir", runs],
                     ["run", str(jobs.DISPATCH), "--job-id", job_id, "--run-dir", runs]):
            proc = self.lookalike(job_id, argv=argv)
            self.assertEqual(reconcile.find_dispatcher_processes(self.ctx(), job_id), [], argv)
            self.assertIsNone(reconcile.adopt_launching(self.ctx(), job_id))
            self.assertIsNone(proc.poll())
        self.assertEqual(self.job(job_id)["state"], "launching")


class RunAfterTheBank(Seams):
    """S2: once `run` has banked, nothing can turn its exit into 3 or 4."""

    def test_run_exits_with_the_banked_code_when_the_ledger_is_busy_after_the_bank(self):
        real_write, real_acquire, after = L.Ledger._write, L.Ledger._acquire, {"bank": False, "seen": 0}

        def write(ledger, doc):
            real_write(ledger, doc)
            banked = sum(job["state"] == "banked" for job in doc["jobs"].values())
            if banked > after["seen"]:
                after["bank"], after["seen"] = True, banked

        def acquire(ledger, mode):  # the lock is held by someone else from the bank commit on
            if after["bank"]:
                raise L.LedgerBusy("ledger busy: lock not obtained")
            return real_acquire(ledger, mode)

        for body, want in (("print('done')", 0), ("sys.exit(3)", 1)):
            after["bank"] = False
            self.fake("worker", body)
            with mock.patch.object(L.Ledger, "_write", write), mock.patch.object(L.Ledger, "_acquire", acquire):
                code, out = self.inproc("run", *self.launch_args())
            self.assertEqual(code, want, out)
            self.assertEqual(out["state"], "banked")
            self.assertEqual(self.job(out["job_id"])["state"], "banked")


class RunSeesTheDispatcherDie(Seams):
    """S3: no valid result and no valid envelope: the reconciler's rows decide, not a synthesised result_missing."""

    def run_async(self, *extra):
        proc = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "run", "--project", str(self.project), *self.launch_args(), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        self.addCleanup(self.reap_proc, proc)
        return proc

    def running_job(self):
        def find():
            for job_id, job in self.doc()["jobs"].items():
                if job["dispatcher"] and self.start_json(job_id).get("state") == "running" and self.kids():
                    return job_id
        return wait_for(find)

    def test_a_killed_dispatcher_gets_its_worker_killed_before_run_banks(self):
        self.fake("worker", SLEEP_WITH_CHILDREN)
        proc = self.run_async()
        job_id = self.running_job()
        self.assertTrue(job_id)
        kid, escaped = self.kids()
        worker = self.start_json(job_id)["pid"]
        self.kill_dispatcher(job_id)
        self.assertTrue(self.banked(job_id, 60))
        for pid in (worker, kid, escaped):  # the bank may only come after the kills
            self.assertFalse(alive(pid), f"{pid} was still alive when run banked")
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1, (out, err))
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"], result["source"], result["trust"]),
                         ("failed", "orphaned", "reconcile", "recovery"))
        code, drained = self.cli("drain", "wait", "--timeout", "10")
        self.assertEqual((code, drained["state"]), (0, "DRAINED"))

    def test_with_the_reconcile_lock_busy_run_leaves_the_job_and_exits_5(self):
        self.fake("worker", SLEEP_WITH_CHILDREN)
        proc = self.run_async()
        job_id = self.running_job()
        worker = self.start_json(job_id)["pid"]
        fd = os.open(self.state / "reconcile.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.kill_dispatcher(job_id)
            out, err = proc.communicate(timeout=60)
        finally:
            os.close(fd)
        self.assertEqual(proc.returncode, 5, (out, err))
        self.assertTrue(json.loads(out)["pending"])
        self.assertEqual(self.job(job_id)["state"], "launched")
        self.assertTrue(alive(worker), "run must not kill anything without the reconcile lock")
        code, got = self.cli("reconcile")
        self.assertEqual(code, 0, got)
        self.assertEqual(self.job(job_id)["result"]["cause"], "orphaned")
        self.assertFalse(alive(worker))


class StartingWorktree(Seams):
    """A write job whose dispatcher dies while `git worktree add` is in flight (helpers shared by the rule-W tests)."""

    def starting(self, how="start", job_id="wj"):
        (self.bin / "git").write_text(FAKE_GIT)
        (self.bin / "git").chmod(0o755)
        repo = self.repo()
        cfg = self.config(permit=["builder"], allow_write=True, worktree_root=str(self.dir / "wt"))
        args = ["--config", cfg, "--role", "builder", "--intent", "x", "--workdir", str(repo), "--job-id", job_id]
        run = None
        if how == "start":
            code, out = self.cli("start", *args)
            self.assertEqual(code, 0, out)
        else:
            run = subprocess.Popen([sys.executable, "-B", str(CLI), "run", "--project", str(self.project), *args],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
            self.addCleanup(self.reap_proc, run)
            self.assertTrue(wait_for(lambda: job_id in self.doc()["jobs"] and self.job(job_id)["dispatcher"]))
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "starting"
                                 and (self.bin / "added").exists()), "worktree add never finished")
        plan = self.job(job_id)["worktree_plan"]
        self.kill_dispatcher(job_id)
        return repo, plan, run

    def set_start(self, job_id, **changes):
        path = self.state / "runs" / job_id / "start.json"
        start = json.loads(path.read_text())
        start.update(changes)
        path.write_text(json.dumps(start))


class RunRuleW(StartingWorktree):
    def test_run_whose_dispatcher_died_in_starting_applies_rule_w(self):
        repo, plan, run = self.starting(how="run")
        out, err = run.communicate(timeout=120)
        self.assertEqual(run.returncode, 1, (out, err))
        result = self.job("wj")["result"]
        self.assertEqual((result["cause"], result["source"]), ("not_launched", "reconcile"))
        self.assertFalse(result["worktree"]["kept"], result)
        self.assertFalse(Path(plan["path"]).exists())


class WorktreeRules(StartingWorktree):
    def keeps_with(self, bad):
        repo, plan, _ = self.starting()
        self.set_start("wj", worktree=bad)
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, (bad, out))
        result = self.job("wj")["result"]
        self.assertTrue(result["worktree"]["kept"], (bad, result))
        self.assertTrue(Path(plan["path"]).exists())

    def test_a_string_worktree_entry_in_start_json_keeps_the_worktree(self):
        self.keeps_with("oops")

    def test_a_list_worktree_entry_in_start_json_keeps_the_worktree(self):
        self.keeps_with(["a", "b"])

    def test_a_worktree_entry_with_non_string_keys_keeps_the_worktree(self):
        self.keeps_with({"path": 1, "branch": 2})

    def test_rule_w_status_ignores_a_worker_planted_fsmonitor_hook(self):
        repo, plan, _ = self.starting()
        marker = self.dir / "fsmonitor-ran"
        hook = self.dir / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        self.assertFalse(marker.exists(), "git status ran the worker's fsmonitor hook")
        self.assertFalse(self.job("wj")["result"]["worktree"]["kept"])

    def test_an_ignored_build_output_keeps_the_worktree(self):
        repo, plan, _ = self.starting()
        with open(repo / ".git" / "info" / "exclude", "a", encoding="utf-8") as handle:
            handle.write("*.o\n")
        (Path(plan["path"]) / "build.o").write_text("object code")
        self.cli("reconcile")
        result = self.job("wj")["result"]
        self.assertTrue(result["worktree"]["kept"], result)
        self.assertTrue((Path(plan["path"]) / "build.o").exists())

    def test_rule_w_does_not_run_after_a_sweep_that_timed_out(self):
        repo, plan, _ = self.starting()
        timed_out = {"killed": 0, "unknown": "unknown: timed out", "note": reconcile.SWEEP_NOTE}
        with mock.patch.object(reconcile, "sweep", return_value=timed_out), \
                mock.patch.object(reconcile, "rule_w") as rule_w:
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out["results"][0].get("changed")), (5, True), out)  # a timed-out sweep may have killed
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        rule_w.assert_not_called()
        self.assertEqual(self.job("wj")["state"], "launched")
        self.assertTrue(Path(plan["path"]).exists())

    def test_a_bank_failure_after_a_removal_is_exit_5_and_the_retry_keeps_the_removal_record(self):
        repo, plan, _ = self.starting()
        real, calls = jobs.bank, []

        def flaky(ledger, job_id, obs):
            calls.append(job_id)
            if len(calls) == 1:
                raise L.LedgerBusy("ledger busy: injected")
            return real(ledger, job_id, obs)

        with mock.patch.object(jobs, "bank", flaky):
            code, out = self.inproc("reconcile")
        self.assertEqual(code, 5, out)
        self.assertTrue(out["changed"])
        self.assertFalse(Path(plan["path"]).exists(), "the worktree was removed")
        self.assertEqual(self.job("wj")["state"], "launched")
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        worktree = self.job("wj")["result"]["worktree"]
        self.assertIsNotNone(worktree, "the retry lost the removal record")
        self.assertEqual((worktree["kept"], worktree["path"]), (False, plan["path"]))


class KillsThenBankFails(Seams):
    def test_a_bank_failure_after_the_worker_was_killed_is_exit_5_not_3(self):
        job_id = self.start_sleeper()
        worker = self.start_json(job_id)["pid"]
        self.kill_dispatcher(job_id)
        real, calls = jobs.bank, []

        def flaky(ledger, jid, obs):
            calls.append(jid)
            if len(calls) == 1:
                raise L.LedgerBusy("ledger busy: injected")
            return real(ledger, jid, obs)

        with mock.patch.object(jobs, "bank", flaky):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out["changed"]), (5, True), out)
        self.assertTrue(wait_for(lambda: not alive(worker)))
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.job(job_id)["state"], "banked")

    def test_with_nothing_done_a_busy_ledger_stays_exit_3(self):
        job_id = jobs.admit(self.ctx(), self.opts())
        self.make_submitter_dead(job_id)
        self.edit(lambda d: d["jobs"][job_id].update(created_at="2020-01-01T00:00:00Z"))
        with mock.patch.object(jobs, "bank", side_effect=L.LedgerBusy("ledger busy: injected")):
            code, out = self.inproc("reconcile")
        self.assertEqual(code, 3, out)


class SmallerFindings(Seams):
    def test_group_kill_never_trusts_a_pgid_from_start_json(self):
        job_id = self.start_sleeper()
        worker = self.start_json(job_id)["pid"]
        stranger = subprocess.Popen(["sleep", "60"], start_new_session=True)  # newer than the worker, same uid
        self.addCleanup(self.reap_proc, stranger)
        self.kill_dispatcher(job_id)
        path = self.state / "runs" / job_id / "start.json"
        start = json.loads(path.read_text())
        start["pgid"] = stranger.pid
        path.write_text(json.dumps(start))
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        self.assertTrue(wait_for(lambda: not alive(worker)))
        self.assertIsNone(stranger.poll(), "a pgid from start.json named another process group")
        self.assertEqual(reconcile.kill_group_members(dict(start, pgid=stranger.pid)), 0)

    def test_a_non_integer_dispatcher_start_ticks_is_unknown_not_gone(self):
        job_id = self.launching_without_process()
        dead = self.dead_pid()
        runs = self.state / "runs" / job_id
        runs.mkdir()
        (runs / "start.json").write_text(json.dumps(
            {"job_id": job_id, "state": "starting", "dispatcher_pid": dead, "dispatcher_start_ticks": "abc"}))
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        self.assertEqual(self.job(job_id)["state"], "launching")
        self.assertEqual(reconcile.liveness(os.getpid(), "abc")[0], "unknown")

    def launching_without_process(self):
        job_id = jobs.admit(self.ctx(), self.opts())
        self.make_submitter_dead(job_id)
        return job_id

    def test_a_planted_symlink_at_the_temporary_name_is_not_followed(self):
        victim = self.dir / "victim.txt"
        victim.write_text("precious")
        ctx = self.ctx()
        job_id = "planted"
        os.symlink(victim, ctx.spool / f"{job_id}.prompt.{os.getpid()}.tmp")
        self.assertEqual(jobs.admit(ctx, self.opts(job_id=job_id, intent="hello")), job_id)
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual((ctx.spool / f"{job_id}.prompt").read_text(), "hello")
        stored = ctx.spool / "e1.external.txt"
        os.symlink(victim, ctx.spool / f"e1.external.txt.{os.getpid()}.tmp")
        jobs.write_private(stored, "text")
        self.assertEqual(victim.read_text(), "precious")

    def test_the_banked_worktree_comes_from_the_ledger_plan_and_a_mismatch_is_noted(self):
        repo = self.repo()
        cfg = self.config(permit=["builder"], allow_write=True, worktree_root=str(self.dir / "wt"))
        code, out = self.cli("run", "--config", cfg, "--role", "builder", "--intent", "x", "--workdir", str(repo))
        self.assertEqual(code, 0, out)
        job = self.job(out["job_id"])
        envelope = json.loads((self.state / "runs" / job["job_id"] / "result.json").read_text())
        envelope["dispatch"]["worktree"] = {"path": "/etc", "branch": "elsewhere"}
        obs = jobs.observation_from_envelope(job, envelope, "0" * 64, "file")
        self.assertEqual((obs["worktree"]["path"], obs["worktree"]["branch"]),
                         (job["worktree_plan"]["path"], job["worktree_plan"]["branch"]))
        self.assertIn("differs from the plan", obs["note"])
        envelope["dispatch"]["worktree"] = {"path": job["worktree_plan"]["path"], "branch": job["worktree_plan"]["branch"]}
        self.assertNotIn("differs", jobs.observation_from_envelope(job, envelope, "0" * 64, "file")["note"] or "")

    def test_an_exception_for_one_job_leaves_it_unresolved_and_the_others_continue(self):
        first = self.start_sleeper()
        self.assertTrue(wait_for((self.bin / "started").exists), "the first worker never read its script")
        self.fake("worker", "print('done')")
        code, out = self.start()
        second = out["job_id"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / second / "result.json").exists()))
        self.kill_dispatcher(first)
        os.killpg(self.start_json(first)["pgid"], signal.SIGKILL)
        real = reconcile.gone_rows

        def explode(ctx, job, dry, acted=False):
            if job["job_id"] == first:
                raise AttributeError("'str' object has no attribute 'get'")
            return real(ctx, job, dry, acted)

        with mock.patch.object(reconcile, "gone_rows", explode):
            code, out = self.inproc("reconcile")
            self.assertEqual(code, 0, out)
            rows = {row["job_id"]: row for row in out["results"]}
            self.assertEqual(rows[first]["status"], "unresolved", out)
            self.assertEqual(rows[second]["status"], "banked", out)
            self.assertEqual(out["pending"], [first])
            code, out = self.inproc("reconcile", "--force-bank", first, "--reason", "operator")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.job(first)["result"]["cause"], "abandoned")

    def test_admit_keeps_the_prompt_when_the_commit_happened_but_its_durability_is_unknown(self):
        ctx = self.ctx()
        with mock.patch.object(L, "fsync_dir", side_effect=OSError("injected")):
            with self.assertRaises(L.LedgerDurabilityUnknown):
                jobs.admit(ctx, self.opts(job_id="kept-1"))
        self.assertIn("kept-1", self.doc()["jobs"])
        self.assertTrue((ctx.spool / "kept-1.prompt").exists(), "the committed job lost its prompt")

    def test_admit_removes_the_prompt_when_the_commit_did_not_happen(self):
        ctx = self.ctx()
        (self.state / "ledger.json.tmp").mkdir()
        with self.assertRaises(L.LedgerStorageError):
            jobs.admit(ctx, self.opts(job_id="gone-1"))
        self.assertFalse((ctx.spool / "gone-1.prompt").exists())


class CancelRules(Seams):
    def test_cancel_waits_for_the_reconcile_lock_and_exits_5_when_it_stays_busy(self):
        self.set_config(lock_wait_seconds=0.3)
        job_id = jobs.admit(self.ctx(), self.opts())
        self.make_submitter_dead(job_id)
        fd = os.open(self.state / "reconcile.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.cli("cancel", "--job", job_id)
        finally:
            os.close(fd)
        self.assertEqual((code, out["cancel"], out["pending"]), (5, "requested", True), out)
        self.assertEqual(self.job(job_id)["state"], "launching")
        self.assertIsNotNone(self.job(job_id)["cancel"])
        code, out = self.cli("cancel", "--job", job_id)
        self.assertEqual((code, out["cause"]), (0, "cancelled"), out)

    def test_cancel_force_during_a_failed_step_three_banks_cancelled(self):
        self.fake("worker", "time.sleep(60)")
        allow, result = threading.Event(), {}
        real = jobs.record_dispatcher

        def gated(ctx, job_id, pid):
            if not allow.is_set():
                raise L.LedgerStorageError("injected: step 3 cannot be written")
            return real(ctx, job_id, pid)

        def target():
            result["code"], result["out"] = jobs.run_job(self.ctx(), self.opts())

        with mock.patch.object(jobs, "record_dispatcher", gated):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: self.doc()["jobs"]))
            job_id = next(iter(self.doc()["jobs"]))
            time.sleep(0.7)  # at least one failed retry
            code, out = self.cli("cancel", "--job", job_id, "--force")
            self.assertEqual((code, out["state"]), (0, "launching"), out)
            allow.set()
            thread.join(timeout=40)
            self.assertFalse(thread.is_alive(), "run stayed in launching with a live submitter")
        self.assertEqual((result["code"], result["out"]["outcome"], result["out"]["cause"]), (1, "failed", "cancelled"))
        self.assertEqual(self.job(job_id)["state"], "banked")


class SameSourceOrder(Seams):
    """F8: an unwritable result.json banks the same outcome from `run` and from the reconciler."""

    def block_result_json(self, job_id):
        def go():
            path = self.state / "runs" / job_id
            wait_for(lambda: (path / "start.json").exists())
            os.mkdir(path / "result.json")  # the dispatcher's atomic replace now fails; the envelope still reaches stdout
        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread

    def test_run_and_reconcile_bank_the_same_outcome(self):
        self.fake("worker", WORKER_PAUSE)
        blocker = self.block_result_json("via-run")
        code, out = self.run_job("--job-id", "via-run")
        blocker.join()
        self.assertEqual((code, out["outcome"]), (0, "succeeded"), out)
        code, out = self.start("--job-id", "via-start")
        self.assertEqual(code, 0, out)
        self.block_result_json("via-start").join(timeout=30)
        pid = self.job("via-start")["dispatcher"]["pid"]
        self.assertTrue(wait_for(lambda: not alive(pid), 30))
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        keys = ("outcome", "cause", "source", "trust")
        by_run, by_reconcile = self.job("via-run")["result"], self.job("via-start")["result"]
        self.assertEqual({k: by_run[k] for k in keys}, {k: by_reconcile[k] for k in keys})
        self.assertEqual((by_run["outcome"], by_run["source"]), ("succeeded", "stdout"))
        self.assertEqual(by_run["record"]["outcome"], by_reconcile["record"]["outcome"])


class PinnedOrder(Seams):
    """Mutants D, L and N of the verifier seat."""

    def test_liveness_is_decided_before_result_json_is_read(self):
        job_id = self.start_sleeper()
        calls = []
        real_verdict, real_read = reconcile.dispatcher_verdict, jobs.read_result_file

        def verdict(ctx, job):
            calls.append("liveness")
            return real_verdict(ctx, job)

        def read(ctx, job):
            calls.append("result.json")
            return real_read(ctx, job)

        with mock.patch.object(reconcile, "dispatcher_verdict", verdict), mock.patch.object(jobs, "read_result_file", read):
            row = reconcile.reconcile_job(self.ctx(), job_id)
        self.assertEqual(row["status"], "running")
        self.assertEqual(calls[:2], ["liveness", "result.json"])

    def test_gone_rows_rereads_result_json_from_disk_after_the_liveness_decision(self):
        code, out = self.start()
        job_id = out["job_id"]
        pid = self.job(job_id)["dispatcher"]["pid"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "result.json").exists()))
        self.assertTrue(wait_for(lambda: not alive(pid)))
        (self.state / "spool" / f"{job_id}.out").unlink()  # the stdout envelope must not stand in for the file
        real, calls = jobs.read_result_file, []

        def first_looks_absent(ctx, job):
            calls.append(1)
            return ("absent", None) if len(calls) == 1 else real(ctx, job)

        with mock.patch.object(jobs, "read_result_file", first_looks_absent):
            row = reconcile.reconcile_job(self.ctx(), job_id)
        self.assertEqual((row["status"], row["action"]), ("banked", "result.json present"), row)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.job(job_id)["result"]["source"], "file")


# --- fix round 2 ----------------------------------------------------------------------------------------------

class Round2(Seams):
    def launched_dead(self):
        """A `launched` job whose dispatcher is gone, with a run directory and no result."""
        job_id = jobs.admit(self.ctx(), self.opts())
        (self.state / "runs" / job_id).mkdir(mode=0o700, parents=True, exist_ok=True)
        dead = self.dead_pid()
        record = {"pid": dead, "start_ticks": 1, "launched_monotonic": jobs.monotonic(), "boot_id": jobs.boot_id()}
        self.edit(lambda d: d["jobs"][job_id].update(state="launched", dispatcher=record))
        return job_id, dead

    def in_thread(self, fn, timeout=15):
        box = {}
        thread = threading.Thread(target=lambda: box.update(value=fn()), daemon=True)
        thread.start()
        thread.join(timeout)
        return not thread.is_alive(), box.get("value")

    def fifo(self, path):
        os.mkfifo(path)

        def release():  # an opener that can write wakes any reader still blocked in open()
            with open(os.open(path, os.O_RDWR | os.O_NONBLOCK), "wb"):
                pass
        self.addCleanup(release)


class FifoInWorkerFiles(Round2):
    """A1: a FIFO where the dispatcher's files should be never blocks anything, least of all under the ledger lock."""

    def test_a_fifo_at_result_json_does_not_hold_the_ledger_lock(self):
        job_id, dead = self.launched_dead()
        self.fifo(self.state / "runs" / job_id / "result.json")
        done, value = self.in_thread(lambda: jobs.finish_run(self.ctx(), job_id, dead, 0))
        self.assertTrue(done, "finish_run blocked on a FIFO while holding the ledger lock")
        code, out = self.cli("status", timeout=30)  # another process is not locked out
        self.assertEqual(code, 0, out)
        self.assertEqual(self.job(job_id)["state"], "banked")
        self.assertEqual(self.job(job_id)["result"]["cause"], "invalid_result")

    def test_a_fifo_at_rule_w_json_does_not_block_rule_w(self):
        run_dir = self.dir / "rd"
        run_dir.mkdir()
        self.fifo(run_dir / "rule_w.json")
        plan = {"path": str(self.dir / "wt" / "gone"), "branch": "dispatch/x-gone"}
        job = {"worktree_plan": plan, "workdir": str(self.dir)}
        done, _ = self.in_thread(lambda: reconcile.rule_w(job, {"worktree": plan}, run_dir))
        self.assertTrue(done, "rule W blocked on a FIFO")

    def test_sha_file_refuses_a_fifo_instead_of_blocking(self):
        with self.assertRaises(OSError):
            self.fifo(self.dir / "f")
            jobs.sha_file(self.dir / "f")


class ForgedRemovalRecord(Round2):
    def test_a_forged_rule_w_record_is_ignored_while_the_branch_has_commits(self):
        repo = self.repo()
        env = git_env(self.dir / "home")
        path, branch = str(self.dir / "wt-forged"), "dispatch/builder-forged"
        for argv in (["worktree", "add", "-q", "-b", branch, path, "HEAD"],):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, env=env)
        subprocess.run(["git", "-C", path, "commit", "-q", "--allow-empty", "-m", "work"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", path], check=True, env=env)
        run_dir = self.dir / "rd"
        run_dir.mkdir()
        plan = {"path": path, "branch": branch}
        (run_dir / "rule_w.json").write_text(json.dumps(
            dict(plan, kept=False, reason="half-created worktree was clean and empty: removed")))
        job = {"worktree_plan": plan, "workdir": str(repo)}
        got = reconcile.rule_w(job, {"worktree": plan}, run_dir)
        self.assertTrue(got["kept"], got)


class KillThenBankFailsEverywhere(Round2):
    def test_a_stuck_dispatcher_that_was_signalled_then_a_busy_ledger_is_exit_5_changed(self):
        job_id = self.start_sleeper()
        os.kill(self.job(job_id)["dispatcher"]["pid"], signal.SIGSTOP)  # stuck: stopped
        real, calls = jobs.bank, []

        def flaky(ledger, jid, obs):
            calls.append(jid)
            if len(calls) == 1:
                raise L.LedgerBusy("ledger busy: injected")
            return real(ledger, jid, obs)

        with mock.patch.object(jobs, "bank", flaky):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (5, True), out)

    def test_the_bank_outcome_comes_from_the_bank_transaction(self):
        job_id = self.start_sleeper()
        self.kill_dispatcher(job_id)
        real_bank, real_read, after = jobs.bank, L.Ledger.read, {"bank": False}

        def bank(ledger, jid, obs):
            done = real_bank(ledger, jid, obs)
            after["bank"] = True
            return done

        def read(ledger):
            if after["bank"]:
                raise L.LedgerBusy("ledger busy: injected after the bank")
            return real_read(ledger)

        with mock.patch.object(jobs, "bank", bank), mock.patch.object(L.Ledger, "read", read):
            code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["results"][0]["status"], out["results"][0]["outcome"]), ("banked", "failed"), out)

    def test_an_exception_after_a_kill_is_exit_5_changed_not_unresolved(self):
        job_id = self.start_sleeper()
        self.kill_dispatcher(job_id)
        with mock.patch.object(reconcile, "bank_acted", side_effect=RuntimeError("boom")):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (5, True), out)


class FinishRunRound2(Round2):
    def test_the_dispatcher_record_is_taken_before_the_reap(self):
        real_ticks, seen = dispatch.proc_start_ticks, {}

        def ticks(pid):
            value = real_ticks(pid)
            if value is None:
                return 424242  # what a recycled pid would show
            seen[pid] = value
            return value

        with mock.patch.object(dispatch, "proc_start_ticks", ticks), \
                mock.patch.object(jobs, "record_dispatcher", side_effect=L.LedgerBusy("ledger busy: injected")):
            code, out = self.inproc("run", *self.launch_args())
        self.assertEqual(code, 0, out)
        disp = self.job(out["job_id"])["dispatcher"]
        self.assertIsNotNone(seen.get(disp["pid"]), "the record was never read while the child was unreaped")
        self.assertEqual(disp["start_ticks"], seen[disp["pid"]])

    def test_finish_run_waits_for_the_reconcile_lock_a_little(self):
        job_id, dead = self.launched_dead()
        fd = os.open(self.state / "reconcile.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        timer = threading.Timer(1.0, lambda: os.close(fd))
        timer.start()
        self.addCleanup(timer.join)
        code, out = jobs.finish_run(self.ctx(), job_id, dead, 0)
        self.assertNotEqual(code, 5, out)
        self.assertEqual(self.job(job_id)["state"], "banked")

    def test_an_exception_in_gone_rows_is_exit_5_not_a_traceback(self):
        job_id, dead = self.launched_dead()
        with mock.patch.object(reconcile, "gone_rows", side_effect=RuntimeError("boom")):
            code, out = jobs.finish_run(self.ctx(), job_id, dead, 0)
        self.assertEqual(code, 5, out)
        self.assertTrue(out["pending"])
        self.assertEqual(self.job(job_id)["state"], "launched")

    def test_a_pending_cancel_is_honoured_when_step_three_durability_is_unknown(self):
        self.fake("worker", "time.sleep(60)")
        real_spawn, real_record = jobs.spawn, jobs.record_dispatcher

        def spawn(ctx, opts, job_id):
            proc = real_spawn(ctx, opts, job_id)
            self.edit(lambda d: d["jobs"][job_id].update(
                cancel={"requested_at": L.now(), "by": {"harness": "cli", "session_id": None}}))
            return proc

        def record(ctx, job_id, pid):
            real_record(ctx, job_id, pid)
            raise L.LedgerDurabilityUnknown("fsync failed: injected")

        with mock.patch.object(jobs, "spawn", spawn), mock.patch.object(jobs, "record_dispatcher", record):
            code, out = self.inproc("start", *self.launch_args())
        self.assertEqual(code, 7, out)
        pid = self.job(out["job_id"])["dispatcher"]["pid"]
        self.assertTrue(wait_for(lambda: not alive(pid)), "the pending cancel was never signalled")


class AmbiguousDispatcherMatch(Lookalikes):
    PARENT = ("import subprocess, sys, time\n"
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', *sys.argv[1:]], "
              "start_new_session=True)\ntime.sleep(60)")

    def test_a_pre_exec_child_of_the_dispatcher_is_not_mistaken_for_it(self):
        job_id = self.launching()
        argv = [str(jobs.DISPATCH), "run", "--job-id", job_id, "--run-dir", str(self.state / "runs")]
        parent = subprocess.Popen([sys.executable, "-c", self.PARENT, *argv], start_new_session=True)
        self.addCleanup(self.reap_proc, parent)

        def kids():
            return [int(e) for e in os.listdir("/proc") if e.isdigit()
                    and (reconcile.stat_fields(int(e)) or {}).get("ppid") == parent.pid]
        child = wait_for(lambda: kids() and reconcile.cmdline(kids()[0]) and len(reconcile.cmdline(kids()[0])) > 3
                         and kids()[0])
        self.assertTrue(child)
        self.addCleanup(os.kill, child, signal.SIGKILL)
        self.assertEqual([pid for pid, _ in reconcile.find_dispatcher_processes(self.ctx(), job_id)], [parent.pid])
        self.assertEqual(reconcile.adopt_launching(self.ctx(), job_id), parent.pid)
        self.assertEqual(self.job(job_id)["dispatcher"]["pid"], parent.pid)

    def test_two_unrelated_matches_are_unresolved_and_nothing_is_signalled(self):
        job_id = self.launching(cancel=True)
        first, second = self.lookalike(job_id), self.lookalike(job_id)
        self.assertEqual(len(reconcile.find_dispatcher_processes(self.ctx(), job_id)), 2)
        self.assertIsNone(reconcile.adopt_launching(self.ctx(), job_id))
        code, out = self.inproc("reconcile")
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        time.sleep(0.3)
        self.assertEqual((first.poll(), second.poll()), (None, None))
        self.assertEqual(self.job(job_id)["state"], "launching")


# --- fix round 3 ----------------------------------------------------------------------------------------------

class Round3(Round2, Lookalikes):
    def banking_busy_once_acted(self):
        """Ledger reads fail as busy from the first signal on (the world has changed by then)."""
        real = L.Ledger.read

        def read(ledger):
            if reconcile.acted():
                raise L.LedgerBusy("ledger busy: injected after a signal")
            return real(ledger)
        return mock.patch.object(L.Ledger, "read", read)

    def planted_result(self, job_id, text):
        (self.state / "runs" / job_id).mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state / "runs" / job_id / "result.json").write_text(text, encoding="utf-8")


class ActedIsGlobal(Round3):
    def test_a_busy_read_after_the_adoption_sigterm_is_exit_5_not_3(self):
        job_id = self.launching(cancel=True)
        proc = self.lookalike(job_id)
        with self.banking_busy_once_acted():
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertTrue(wait_for(lambda: proc.poll() is not None), "the pending cancel never signalled the adopted process")

    def test_a_busy_bank_after_the_adopted_dispatcher_was_signalled_is_exit_5_not_3(self):
        job_id = self.launching(cancel=True)
        proc = self.lookalike(job_id)
        with mock.patch.object(jobs, "bank", side_effect=L.LedgerBusy("ledger busy: injected")):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertTrue(wait_for(lambda: proc.poll() is not None))

    def test_cancel_then_a_busy_read_is_exit_5_not_3(self):
        job_id = self.start_sleeper()
        with self.banking_busy_once_acted():
            code, out = self.inproc("cancel", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertTrue(wait_for(lambda: not alive(self.job(job_id)["dispatcher"]["pid"])))

    def test_cancel_then_a_failing_settle_is_exit_5_not_4(self):
        job_id = self.start_sleeper()
        with mock.patch.object(reconcile, "settle_job", side_effect=L.LedgerInvalid("injected")), \
                mock.patch.object(jobs, "collect_all", return_value=[]):
            code, out = self.inproc("cancel", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)


class OversizedWorkerText(Round3):
    def test_two_4mb_duplicate_key_result_files_do_not_brick_the_ledger(self):
        key = "\x7f" * 2_000_000
        text = '{"%s":1,"%s":2}' % (key, key)
        for _ in range(2):
            job_id, _dead = self.launched_dead()
            self.planted_result(job_id, text)
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        code, out = self.inproc("status")
        self.assertEqual(code, 0, out)
        self.assertLess(len(self.ledger_bytes()), 200_000)
        for job in self.doc()["jobs"].values():
            self.assertEqual(job["result"]["cause"], "invalid_result")
            self.assertLess(len(job["result"]["note"]), 700)
            self.assertIn("sha256", job["result"]["note"])

    def test_an_envelope_with_too_many_evidence_entries_is_invalid_result(self):
        job_id, _dead = self.launched_dead()
        evidence = [{"kind": "k", "reference": "r%d" % i, "sha256": "0" * 64} for i in range(jobs.MAX_EVIDENCE + 1)]
        record = dispatch.make_result(job_id, "succeeded", evidence)
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        result = self.job(job_id)["result"]
        self.assertEqual(result["cause"], "invalid_result")
        self.assertIsNone(result["record"])
        self.assertEqual(result["evidence"], [])
        self.assertRegex(result["result_sha256"], r"^[0-9a-f]{64}$")

    def test_a_valid_envelope_stores_its_evidence_once(self):
        job_id, _dead = self.launched_dead()
        record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        self.inproc("reconcile")
        result = self.job(job_id)["result"]
        self.assertEqual((result["cause"], len(result["record"]["evidence"]), result["evidence"]), ("completed", 1, []))

    def test_a_write_over_the_ceiling_raises_and_leaves_the_old_ledger_intact(self):
        before = self.ledger_bytes()

        def grow(doc):
            doc["drain"] = {"since": L.now(), "reason": "x" * 5000}
        with mock.patch.object(L, "MAX_BYTES", len(before) + 1000):
            with self.assertRaises(L.LedgerStorageError):
                self.edit(grow)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.state / "ledger.json.tmp").exists())
        self.assertEqual(self.doc()["revision"], json.loads(before)["revision"])

    def test_cap_text_keeps_a_hash_of_what_it_cut(self):
        text = "a" * 5000
        capped = L.cap_text(text)
        self.assertLessEqual(len(capped), 500)
        self.assertIn(L.sha256_hex(text.encode()), capped)
        self.assertEqual(L.cap_text("short"), "short")

    def test_an_unresolved_row_after_a_sigterm_says_changed_and_carries_the_notes(self):
        job_id = self.start_sleeper()
        os.kill(self.job(job_id)["dispatcher"]["pid"], signal.SIGSTOP)
        replies = iter([("sent", "note one"), ("unresolved", "note two")])

        def fake_signal(ctx, jid, disp, numbers):
            reconcile._ACTED[0] = True
            return next(replies)

        with mock.patch.object(reconcile, "signal_dispatcher", fake_signal), \
                mock.patch.object(reconcile, "STUCK_TERM_WAIT", 0.2):
            code, out = self.inproc("reconcile")
        row = out["results"][0]
        self.assertEqual((row["status"], row["changed"], row["notes"]), ("unresolved", True, ["note one", "note two"]), out)


class ReconcileAllContinues(Round3):
    def test_a_side_effects_failure_for_one_job_does_not_stop_the_next(self):
        ids = sorted(self.launched_dead()[0] for _ in range(2))
        real = reconcile.reconcile_job

        def first_fails(ctx, job_id, *args, **kw):
            if job_id == ids[0]:
                raise reconcile.SideEffectsDone("injected")
            return real(ctx, job_id, *args, **kw)

        with mock.patch.object(reconcile, "reconcile_job", first_fails):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (5, True), out)
        rows = {r["job_id"]: r for r in out["results"]}
        self.assertEqual((rows[ids[0]]["status"], rows[ids[0]]["changed"]), ("unresolved", True), out)
        self.assertEqual(rows[ids[1]]["status"], "banked", out)
        self.assertEqual(self.job(ids[1])["state"], "banked")
        self.assertEqual(out["pending"], [ids[0]])


class PendingCancel(Round3):
    def test_a_plain_reconcile_honours_a_pending_cancel_of_a_live_dispatcher(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        self.edit(lambda d: d["jobs"][job_id].update(
            cancel={"requested_at": L.now(), "by": {"harness": "cli", "session_id": None}}))
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        self.assertTrue(wait_for(lambda: not alive(pid)), "the recorded cancel was not honoured")
        self.assertEqual(self.job(job_id)["state"], "banked")

    def test_run_retries_the_pending_cancel_until_the_lookup_succeeds(self):
        self.fake("worker", "time.sleep(60)")
        real_spawn, real_record, real_honour = jobs.spawn, jobs.record_dispatcher, jobs.honour_cancel
        calls = []

        def spawn(ctx, opts, job_id):
            proc = real_spawn(ctx, opts, job_id)
            self.edit(lambda d: d["jobs"][job_id].update(
                cancel={"requested_at": L.now(), "by": {"harness": "cli", "session_id": None}}))
            return proc

        def record(ctx, job_id, pid):
            real_record(ctx, job_id, pid)
            raise L.LedgerDurabilityUnknown("fsync failed: injected")

        def honour(ctx, job_id):
            calls.append(job_id)
            if len(calls) == 1:
                with mock.patch.object(L.Ledger, "read", side_effect=L.LedgerBusy("ledger busy: injected")):
                    return real_honour(ctx, job_id)
            return real_honour(ctx, job_id)

        with mock.patch.object(jobs, "spawn", spawn), mock.patch.object(jobs, "record_dispatcher", record), \
                mock.patch.object(jobs, "honour_cancel", honour):
            done, value = self.in_thread(lambda: self.inproc("run", *self.launch_args()), timeout=20)
        self.assertTrue(done, "run never honoured the cancel whose first lookup failed")
        self.assertGreaterEqual(len(calls), 2)


class BoundedHash(Round3):
    def test_sha_file_refuses_a_file_over_the_cap_quickly(self):
        big = self.dir / "big"
        with open(big, "wb") as handle:
            handle.truncate(1 << 26)  # sparse 64 MiB
        start = time.monotonic()
        with self.assertRaises(OSError):
            jobs.sha_file(big)
        self.assertLess(time.monotonic() - start, 2)

    def test_a_lost_job_with_an_oversize_stdout_is_banked_with_the_reason_and_no_hash(self):
        self.fake("worker", "print('partial', flush=True)\ntime.sleep(60)")
        code, out = self.start()
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "running"))
        self.kill_dispatcher(job_id)
        os.killpg(self.start_json(job_id)["pgid"], signal.SIGKILL)
        self.assertTrue(wait_for(lambda: not alive(self.start_json(job_id)["pid"])))
        with open(self.state / "runs" / job_id / "stdout.txt", "wb") as handle:
            handle.truncate(8 << 20)  # sparse, over the 1 MiB cap
        start = time.monotonic()
        code, out = self.inproc("reconcile")
        self.assertLess(time.monotonic() - start, 30)
        result = self.job(job_id)["result"]
        self.assertEqual((result["cause"], result["evidence"]), ("lost", []))
        self.assertIn("not hashed (file too large)", result["note"])


if __name__ == "__main__":
    unittest.main()
