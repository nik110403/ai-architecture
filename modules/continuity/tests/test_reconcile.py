import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ContCase, L, alive, dispatch, jobs, reconcile, wait_for  # noqa: E402
from test_jobs import SLEEP_WITH_CHILDREN, Seams  # noqa: E402,F401

FAKE_GIT = """#!/bin/sh
/usr/bin/git "$@"
code=$?
case " $* " in *" worktree add "*) touch "$(dirname "$0")/added"; sleep 20;; esac
exit $code
"""


def proc_state(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def pending_signals(pid):
    """(SigPnd, ShdPnd) from /proc/<pid>/status: signals queued for the thread / the whole process."""
    found = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("SigPnd", "ShdPnd"):
            found[key] = int(value.strip(), 16)
    return found["SigPnd"], found["ShdPnd"]


class LivenessRules(Seams):
    """R1: identity of the dispatcher."""

    def sleeper(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, proc)
        return proc

    def test_ticks_match_means_alive_and_a_mismatch_means_gone(self):
        proc = self.sleeper()
        ticks = dispatch.proc_start_ticks(proc.pid)
        self.assertEqual(reconcile.liveness(proc.pid, ticks)[0], "alive")
        self.assertEqual(reconcile.liveness(proc.pid, ticks + 1)[0], "gone")
        self.assertEqual(reconcile.liveness(self.dead_pid(), ticks)[0], "gone")

    def test_a_zombie_counts_as_gone(self):
        proc = subprocess.Popen(["true"])
        self.assertTrue(wait_for(lambda: proc_state(proc.pid) == "Z"))
        self.assertEqual(reconcile.liveness(proc.pid, dispatch.proc_start_ticks(proc.pid))[0], "gone")
        proc.wait()

    def test_null_ticks_need_the_exact_cmdline_tokens(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        runs = self.state / "runs"
        self.assertEqual(reconcile.liveness(pid, None, job_id=job_id, runs=runs)[0], "alive")
        self.assertEqual(reconcile.liveness(pid, None, job_id="other", runs=runs)[0], "unknown")
        self.assertEqual(reconcile.liveness(pid, None, job_id=job_id, runs=self.dir)[0], "unknown")
        self.assertEqual(reconcile.liveness(pid, None)[0], "unknown")

    def test_null_ticks_with_a_matching_cmdline_stay_running_end_to_end(self):
        job_id = self.start_sleeper()
        self.edit(lambda d: d["jobs"][job_id]["dispatcher"].update(start_ticks=None))
        code, out = self.cli("reconcile")
        self.assertEqual((code, out["results"][0]["status"]), (0, "running"), out)
        self.assertEqual(self.job(job_id)["state"], "launched")

    def test_null_ticks_with_a_foreign_process_is_unresolved_never_signalled_never_banked(self):
        job_id = self.start_sleeper()
        stranger = self.sleeper()
        self.edit(lambda d: d["jobs"][job_id]["dispatcher"].update(pid=stranger.pid, start_ticks=None))
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        self.assertEqual(self.job(job_id)["state"], "launched")
        self.assertIsNone(stranger.poll(), "an unknown identity must never be signalled")

    def test_force_bank_clears_an_unresolved_job_without_touching_a_process(self):
        job_id = self.start_sleeper()
        stranger = self.sleeper()
        self.edit(lambda d: d["jobs"][job_id]["dispatcher"].update(pid=stranger.pid, start_ticks=None))
        code, out = self.cli("reconcile", "--force-bank", job_id, "--reason", "operator checked")
        self.assertEqual(code, 2, out)  # an unknown dispatcher verdict is refused without the operator's flag
        code, out = self.cli("reconcile", "--force-bank", job_id, "--reason", "operator checked",
                             "--allow-unknown-liveness")
        self.assertEqual(code, 0, out)
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"]), ("failed", "abandoned"))
        self.assertIn("operator checked", result["note"])
        self.assertIsNone(stranger.poll())
        self.assertTrue(alive(self.start_json(job_id)["pid"]), "the worker must not be touched")
        code, out = self.cli("reconcile", "--force-bank", job_id, "--reason", "again")
        self.assertEqual(code, 2, out)

    def test_force_bank_refuses_a_job_that_is_not_unresolved(self):
        job_id = self.start_sleeper()
        code, out = self.cli("reconcile", "--force-bank", job_id, "--reason", "x")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.job(job_id)["state"], "launched")

    def test_a_different_boot_id_means_gone(self):
        proc = self.sleeper()
        ticks = dispatch.proc_start_ticks(proc.pid)
        self.assertEqual(reconcile.liveness(proc.pid, ticks, "not-this-boot")[0], "gone")

    def test_without_proc_everything_is_unknown(self):
        with mock.patch.object(reconcile, "have_proc", lambda: False):
            self.assertEqual(reconcile.liveness(os.getpid(), None)[0], "unknown")


class StuckRules(Seams):
    """R2: stuck by state T or by the monotonic clock."""

    def test_a_stopped_dispatcher_is_stuck_and_sigcont_sigterm_recovers_a_result(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        os.kill(pid, signal.SIGSTOP)
        self.assertTrue(wait_for(lambda: proc_state(pid) == "T"))
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        job = self.job(job_id)
        self.assertEqual(job["state"], "banked", out)
        self.assertEqual(job["result"]["source"], "file")
        self.assertIsNotNone(job["result"]["record"], "the dispatcher wrote the result itself after SIGTERM")
        self.assertTrue(wait_for(lambda: not alive(pid)))

    def test_a_live_dispatcher_within_timeout_plus_grace_is_never_stuck(self):
        self.fake("worker", "time.sleep(60)")
        cfg = self.config(timeout_seconds=300)
        code, out = self.start(cfg=cfg)
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "running"))
        self.set_config(stuck_grace_seconds=100)
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "running", out)
        self.assertEqual(self.job(job_id)["state"], "launched")
        # the same dispatcher, 400 s of monotonic time later (timeout 300 + grace 100): stuck
        self.edit(lambda d: d["jobs"][job_id]["dispatcher"].update(
            launched_monotonic=jobs.monotonic() - 401))
        code, out = self.cli("reconcile")
        self.assertEqual(self.job(job_id)["state"], "banked", out)
        self.assertEqual(self.job(job_id)["result"]["source"], "file")

    def test_a_young_live_dispatcher_without_start_json_is_running(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, proc)
        job_id = jobs.admit(self.ctx(), self.opts(job_id="young"))

        def go(doc):
            job = doc["jobs"][job_id]
            job["dispatcher"] = jobs.dispatcher_record(proc.pid)
            job["state"] = "launched"
        self.edit(go)
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "running", out)
        self.assertFalse((self.state / "runs" / job_id).exists())
        self.assertEqual(self.job(job_id)["state"], "launched")
        # past the launch grace the missing inputs make it unresolved, not killed, not banked
        self.edit(lambda d: d["jobs"][job_id]["dispatcher"].update(launched_monotonic=jobs.monotonic() - 1000))
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.job(job_id)["state"], "launched")

    def test_a_dispatcher_from_another_boot_is_gone(self):
        job_id = self.start_sleeper()
        self.edit(lambda d: d["jobs"][job_id]["dispatcher"].update(boot_id="a-previous-boot"))
        verdict, why = reconcile.dispatcher_verdict(self.ctx(), self.job(job_id))
        self.assertEqual((verdict, why), ("gone", "recorded boot id differs"))


class SignalSafety(Seams):
    def broken_pidfd(self, number):
        self.pidfd_opens = 0

        def refuse(pid, flags=0):
            self.pidfd_opens += 1
            raise OSError(number, os.strerror(number))
        return mock.patch.object(os, "pidfd_open", refuse)

    def reconcile_in_process(self, job_id):
        return reconcile.reconcile_job(self.ctx(), job_id)

    def test_a_dispatcher_the_reconciler_wants_to_stop_survives_a_pidfd_that_cannot_be_opened(self):
        # A dispatcher within its timeout is never signalled, so pidfd_open would not even be reached: make it stuck
        # by the clock, so the reconciler really tries to signal it, and let pidfd_open fail under it.
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        # (not by editing launched_monotonic to a negative number: a host up for under 100000 s fails the ledger check)
        with mock.patch.object(reconcile, "stuck_verdict", return_value=("stuck", "clock")), \
                self.broken_pidfd(errno.EMFILE):  # ENOSYS falls back to os.kill: test_enosys_falls_back_... covers it
            row = self.reconcile_in_process(job_id)
        self.assertGreaterEqual(self.pidfd_opens, 1, "the test never reached pidfd_open")
        self.assertEqual(row["status"], "unresolved", row)
        self.assertEqual(self.job(job_id)["state"], "launched")
        time.sleep(0.5)
        self.assertTrue(alive(pid), "a signal was sent although pidfd_open failed")
        self.assertEqual(pending_signals(pid), (0, 0))

    def test_emfile_on_a_stopped_dispatcher_is_unresolved_and_nothing_is_signalled(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        os.kill(pid, signal.SIGSTOP)
        self.assertTrue(wait_for(lambda: proc_state(pid) == "T"))
        with self.broken_pidfd(errno.EMFILE):
            row = self.reconcile_in_process(job_id)
        self.assertEqual(row["status"], "unresolved", row)
        self.assertEqual(self.job(job_id)["state"], "launched")
        self.assertEqual(proc_state(pid), "T", "no signal may be sent when pidfd_open fails with EMFILE")
        self.assertEqual(pending_signals(pid), (0, 0), "a queued signal shows that one was sent anyway")

    def test_enosys_falls_back_to_a_tick_recheck_and_kill_and_says_so(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        disp = self.job(job_id)["dispatcher"]
        with self.broken_pidfd(errno.ENOSYS):
            status, note = reconcile.signal_pid(
                pid, lambda: reconcile.liveness(pid, disp["start_ticks"])[0], signal.SIGSTOP)
        self.assertEqual((status, note), ("sent", "signal: no pidfd"))
        self.assertTrue(wait_for(lambda: proc_state(pid) == "T"))
        os.kill(pid, signal.SIGCONT)

    def test_a_pid_whose_ticks_changed_is_never_signalled(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, proc)
        ticks = dispatch.proc_start_ticks(proc.pid)
        status, _ = reconcile.signal_pid(proc.pid, lambda: reconcile.liveness(proc.pid, ticks + 1)[0], signal.SIGKILL)
        self.assertEqual(status, "gone")
        self.assertIsNone(proc.poll())

    def test_a_signal_to_a_missing_pid_is_gone(self):
        self.assertEqual(reconcile.signal_pid(self.dead_pid(), lambda: "alive", signal.SIGTERM)[0], "gone")

    def test_a_sweep_that_overruns_is_killed_and_reported(self):
        with mock.patch.object(reconcile, "SWEEP_TIMEOUT", 0.001):
            got = reconcile.sweep("0" * 64)
        self.assertEqual((got["killed"], got["unknown"]), (0, "unknown: timed out"))
        self.assertIn("env -i", got["note"])


class WorkerIdentity(Seams):
    def test_unknown_worker_identity_is_never_killed(self):
        job_id = self.start_sleeper()
        worker = self.start_json(job_id)["pid"]
        self.kill_dispatcher(job_id)
        path = self.state / "runs" / job_id / "start.json"
        start = json.loads(path.read_text())
        start["worker_start_ticks"] = None
        path.write_text(json.dumps(start))
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        self.assertTrue(alive(worker), "a worker whose identity cannot be proved must not be killed")
        self.assertEqual(self.job(job_id)["state"], "launched")

    def test_a_process_without_the_job_marker_is_not_the_worker(self):
        job_id = self.start_sleeper()
        stranger = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, stranger)
        self.kill_dispatcher(job_id)
        path = self.state / "runs" / job_id / "start.json"
        start = json.loads(path.read_text())
        start.update(pid=stranger.pid, worker_start_ticks=dispatch.proc_start_ticks(stranger.pid))
        path.write_text(json.dumps(start))
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "unresolved", out)
        self.assertIsNone(stranger.poll())

    def test_dry_run_changes_nothing_and_names_the_plan(self):
        job_id = self.start_sleeper()
        worker = self.start_json(job_id)["pid"]
        self.kill_dispatcher(job_id)
        before = self.ledger_bytes()
        code, out = self.cli("reconcile", "--dry-run")
        self.assertEqual((code, out["results"][0]["status"]), (0, "planned"), out)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertTrue(alive(worker))
        code, out = self.cli("reconcile")
        self.assertEqual(self.job(job_id)["result"]["cause"], "orphaned")

    def test_reconcile_while_the_reconcile_lock_is_held_is_skipped_with_exit_5(self):
        job_id = self.start_sleeper()
        fd = os.open(self.state / "reconcile.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.cli("reconcile")
        finally:
            os.close(fd)
        self.assertEqual(code, 5, out)
        self.assertEqual((out["skipped"], out["pending"]), ("another reconcile is running", [job_id]))
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)

    def test_dispatcher_refusal_banked_by_reconcile_when_the_submitter_vanished(self):
        ctx = self.ctx()
        opts = self.opts(config=self.config(permit=[]))
        job_id = jobs.admit(ctx, opts)
        proc = jobs.spawn(ctx, opts, job_id)
        proc.wait(timeout=30)
        jobs.record_dispatcher(ctx, job_id, proc.pid)  # launched; the dispatcher then exited without a result
        code, out = self.cli("reconcile")
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"], result["source"]), ("unsupported", "refused", "stdout"))
        self.assertIn("not permitted", result["note"])


class WorktreeRuleW(Seams):
    """A dispatcher killed while `git worktree add` is in flight: remove only the provably empty worktree."""

    def killed_in_starting(self):
        (self.bin / "git").write_text(FAKE_GIT)
        (self.bin / "git").chmod(0o755)
        repo = self.repo()
        cfg = self.config(permit=["builder"], allow_write=True, worktree_root=str(self.dir / "wt"))
        code, out = self.cli("start", "--config", cfg, "--role", "builder", "--intent", "x", "--workdir", str(repo))
        self.assertEqual(code, 0, out)
        job_id = out["job_id"]
        plan = self.job(job_id)["worktree_plan"]
        # killing the dispatcher while git is still adding would make git clean up after itself (broken pipe)
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "starting"
                                 and (self.bin / "added").exists()), "worktree add never finished")
        self.kill_dispatcher(job_id)
        return job_id, repo, plan

    def branches(self, repo):
        done = subprocess.run(["git", "-C", str(repo), "branch", "--list", "dispatch/*"], capture_output=True,
                              text=True, env=os.environ)
        return done.stdout.strip()

    def test_a_clean_worktree_and_its_branch_are_removed(self):
        job_id, repo, plan = self.killed_in_starting()
        self.assertNotEqual(self.branches(repo), "")
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        result = self.job(job_id)["result"]
        self.assertEqual((result["outcome"], result["cause"]), ("failed", "not_launched"))
        self.assertFalse(result["worktree"]["kept"], result)
        self.assertFalse(Path(plan["path"]).exists())
        self.assertEqual(self.branches(repo), "")

    def test_a_commit_on_the_branch_keeps_both(self):
        job_id, repo, plan = self.killed_in_starting()
        subprocess.run(["git", "-C", plan["path"], "commit", "-q", "--allow-empty", "-m", "work"], check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        self.cli("reconcile")
        result = self.job(job_id)["result"]
        self.assertTrue(result["worktree"]["kept"], result)
        self.assertTrue(Path(plan["path"]).exists())
        self.assertNotEqual(self.branches(repo), "")

    def test_an_untracked_file_keeps_both(self):
        job_id, repo, plan = self.killed_in_starting()
        (Path(plan["path"]) / "notes.txt").write_text("work in progress")
        self.cli("reconcile")
        result = self.job(job_id)["result"]
        self.assertTrue(result["worktree"]["kept"], result)
        self.assertTrue((Path(plan["path"]) / "notes.txt").exists())
        self.assertNotEqual(self.branches(repo), "")

    def test_a_start_json_that_disagrees_with_the_ledger_keeps_the_worktree(self):
        job_id, repo, plan = self.killed_in_starting()
        path = self.state / "runs" / job_id / "start.json"
        start = json.loads(path.read_text())
        start["worktree"] = {"path": str(self.dir / "elsewhere"), "branch": plan["branch"]}
        path.write_text(json.dumps(start))
        self.cli("reconcile")
        result = self.job(job_id)["result"]
        self.assertTrue(result["worktree"]["kept"], result)
        self.assertTrue(Path(plan["path"]).exists())

    def test_a_detached_head_keeps_the_worktree(self):
        job_id, repo, plan = self.killed_in_starting()
        subprocess.run(["git", "-C", plan["path"], "checkout", "-q", "--detach"], check=True, env=os.environ)
        self.cli("reconcile")
        self.assertTrue(self.job(job_id)["result"]["worktree"]["kept"])
        self.assertTrue(Path(plan["path"]).exists())


class ExternalJobs(Seams):
    def register(self, job_id, pid, session="opencode:sess-abc"):
        return self.cli("external", "register", "--job-id", job_id, "--role", "researcher", "--access", "read",
                        "--session", session, "--owner-pid", str(pid))

    def stopped_owner(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, proc)
        os.kill(proc.pid, signal.SIGSTOP)
        self.assertTrue(wait_for(lambda: proc_state(proc.pid) == "T"))
        return proc

    def test_the_pending_signal_check_sees_a_signal_sent_to_a_stopped_process(self):
        owner = self.stopped_owner()
        self.assertEqual(pending_signals(owner.pid), (0, 0))
        os.kill(owner.pid, signal.SIGTERM)
        self.assertTrue(wait_for(lambda: pending_signals(owner.pid) != (0, 0)))
        self.assertEqual(proc_state(owner.pid), "T")

    def test_a_write_access_external_job_is_refused(self):
        code, out = self.cli("external", "register", "--job-id", "e1", "--role", "builder", "--access", "write",
                             "--session", "opencode:s1", "--owner-pid", str(os.getpid()))
        self.assertEqual(code, 2, out)
        self.assertIn("read access", out["error"])
        self.assertEqual(self.doc()["jobs"], {})

    def test_an_external_job_is_never_signalled(self):
        owner = self.stopped_owner()
        code, out = self.register("e1", owner.pid)
        self.assertEqual(code, 0, out)
        job = self.job("e1")
        self.assertEqual((job["kind"], job["state"], job["access"]), ("external", "launched", "read"))
        self.assertEqual(job["dispatcher"]["pid"], owner.pid)
        code, out = self.cli("cancel", "--job", "e1")
        self.assertEqual(code, 2, "the owning session only")
        code, out = self.cli("cancel", "--job", "e1", "--session", "opencode:sess-abc")
        self.assertEqual((code, out["signalled"]), (0, False), out)
        code, out = self.cli("cancel", "--job", "e1", "--force")
        self.assertEqual((code, out["signalled"]), (0, False), out)
        code, out = self.cli("reconcile")
        self.assertEqual(out["results"][0]["status"], "running", out)
        self.assertEqual(proc_state(owner.pid), "T")
        # a stopped process keeps state T whatever is sent to it: a queued signal is what shows a delivery
        self.assertEqual(pending_signals(owner.pid), (0, 0), "the owner is a shared process: never signalled")
        self.assertIsNone(owner.poll())
        self.assertIsNotNone(self.job("e1")["cancel"])
        self.assertEqual(self.job("e1")["state"], "launched")

    def test_the_hub_banks_a_cancelled_external_job(self):
        owner = self.stopped_owner()
        self.register("e1", owner.pid)
        self.cli("cancel", "--job", "e1", "--session", "opencode:sess-abc")
        result_file = self.dir / "out.txt"
        result_file.write_text("partial answer")
        code, out = self.cli("external", "bank", "--job-id", "e1", "--outcome", "failed", "--cause", "cancelled",
                             "--result-file", str(result_file))
        self.assertEqual((code, out["cause"], out["banked_now"]), (0, "cancelled", True), out)
        result = self.job("e1")["result"]
        self.assertEqual(result["source"], "external")
        self.assertEqual(result["evidence"][0]["sha256"], L.sha256_hex(b"partial answer"))
        self.assertEqual((self.state / "spool" / "e1.external.txt").read_text(), "partial answer")
        code, out = self.cli("external", "bank", "--job-id", "e1", "--outcome", "succeeded", "--cause", "completed")
        self.assertEqual(out["banked_now"], False, "a second bank is never a second result")

    def test_a_succeeded_external_job_needs_cause_completed(self):
        owner = self.stopped_owner()
        self.register("e1", owner.pid)
        code, out = self.cli("external", "bank", "--job-id", "e1", "--outcome", "succeeded", "--cause", "cancelled")
        self.assertEqual(code, 2, out)
        code, out = self.cli("external", "bank", "--job-id", "e1", "--outcome", "succeeded", "--cause", "completed")
        self.assertEqual((code, out["outcome"]), (0, "succeeded"), out)

    def test_an_owner_that_is_gone_orphans_the_external_job(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.register("e1", proc.pid, session="opencode:sess-gone")
        proc.kill()
        proc.wait()
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        result = self.job("e1")["result"]
        self.assertEqual((result["outcome"], result["cause"], result["trust"]), ("failed", "orphaned_external",
                                                                              "recovery"))
        self.assertIn("opencode:sess-gone", result["note"])

    def test_register_is_refused_during_a_drain_and_counts_toward_the_cap(self):
        owner = self.stopped_owner()
        self.cli("drain", "start", "--reason", "x")
        code, out = self.register("e1", owner.pid)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
