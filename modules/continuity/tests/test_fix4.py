"""R9-U04a fix round 4: the acted rule at the entry point, lone surrogates, no uncapped worker string in the ledger."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import L, alive, continuity, dispatch, git_env, jobs, reconcile, wait_for  # noqa: E402
from test_fix1 import Round3  # noqa: E402

CANCEL = {"requested_at": "2026-01-01T00:00:00Z", "by": {"harness": "cli", "session_id": None}}


class Round4(Round3):
    def busy_after_reconcile_all(self, fail_on_call=None):
        """reconcile_all runs for real; every ledger read after it returned (or its `fail_on_call`-th call) is busy."""
        real_all, real_read, state = reconcile.reconcile_all, L.Ledger.read, {"after": False, "calls": 0}

        def reconcile_all(*args, **kw):
            state["calls"] += 1
            if state["calls"] == fail_on_call:
                raise L.LedgerBusy("ledger busy: injected")
            result = real_all(*args, **kw)
            state["after"] = fail_on_call is None
            return result

        def read(ledger):
            if state["after"]:
                raise L.LedgerBusy("ledger busy: injected")
            return real_read(ledger)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(reconcile, "reconcile_all", reconcile_all))
        stack.enter_context(mock.patch.object(L.Ledger, "read", read))
        return stack

    def cancelled_sleeper(self):
        job_id = self.start_sleeper()
        self.edit(lambda d: d["jobs"][job_id].update(cancel=CANCEL))
        return job_id


class C1ActedAtTheEntryPoint(Round4):
    def test_wait_with_a_recorded_cancel_then_a_busy_read_is_exit_5_changed(self):
        job_id = self.cancelled_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        with self.busy_after_reconcile_all():
            code, out = self.inproc("wait", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)
        # banked from the result.json the dispatcher writes on SIGTERM: it may still be exiting when the command returns
        self.assertTrue(wait_for(lambda: not alive(pid), 10), "dispatcher survived the cancel")

    def test_drain_wait_with_a_recorded_cancel_then_a_busy_read_is_exit_5_changed(self):
        job_id = self.cancelled_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        with self.busy_after_reconcile_all():
            code, out = self.inproc("drain", "wait", "--timeout", "20")
        self.assertEqual((code, out.get("changed")), (5, True), out)
        # banked from the result.json the dispatcher writes on SIGTERM: it may still be exiting when the command returns
        self.assertTrue(wait_for(lambda: not alive(pid), 10), "dispatcher survived the cancel")

    def test_drain_waits_second_pass_failing_busy_is_exit_5_changed(self):
        job_id = self.cancelled_sleeper()
        owner = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, owner)
        code, out = self.cli("external", "register", "--job-id", "ext1", "--role", "researcher", "--access", "read",
                             "--session", "opencode:sess-abc", "--owner-pid", str(owner.pid))
        self.assertEqual(code, 0, out)
        with self.busy_after_reconcile_all(fail_on_call=2):
            code, out = self.inproc("drain", "wait", "--timeout", "20")
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertEqual(self.job(job_id)["state"], "banked")

    def test_every_ledger_failure_after_an_act_is_exit_5_changed_for_any_command(self):
        for error in (L.LedgerBusy("busy"), L.LedgerInvalid("invalid"), L.LedgerStorageError("storage")):
            for command in (["status"], ["collect"], ["drain", "status"]):
                def act_then_fail(args, error=error):
                    reconcile._ACTED[0] = True
                    raise error
                with self.subTest(error=type(error).__name__, command=command), \
                        mock.patch.object(continuity, "dispatch_command", act_then_fail):
                    code, out = self.inproc(*command)
                    self.assertEqual((code, out.get("changed")), (5, True), out)

    def test_without_an_act_the_codes_stay_3_and_4(self):
        for error, want in ((L.LedgerBusy("busy"), 3), (L.LedgerInvalid("invalid"), 4)):
            with mock.patch.object(continuity, "dispatch_command", side_effect=error):
                code, _ = self.inproc("status")
            self.assertEqual(code, want)


class C2LoneSurrogate(Round4):
    def surrogate_job(self, reference="X\udc80Y"):
        job_id, _dead = self.launched_dead()
        record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": reference, "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        return job_id

    def check(self, *argv):
        job_id = self.surrogate_job()
        code, out = self.inproc(*argv, *(["--job", job_id] if argv[0] == "wait" else []))
        self.assertIn(code, (0, 1), out)
        job = self.job(job_id)
        self.assertEqual(job["state"], "banked")
        self.assertIn(job["result"]["cause"], ("completed", "invalid_result"))

    def test_status_survives_a_lone_surrogate(self):
        self.check("status")

    def test_collect_survives_a_lone_surrogate(self):
        self.check("collect")

    def test_wait_survives_a_lone_surrogate(self):
        self.check("wait")

    def test_any_exception_from_the_observation_is_an_invalid_result_not_a_traceback(self):
        job_id, _dead = self.launched_dead()
        self.planted_result(job_id, json.dumps({"result": {}, "dispatch": {}}))
        with mock.patch.object(jobs, "observation_from_envelope", side_effect=UnicodeEncodeError("utf-8", "", 0, 1, "x")):
            state, info = jobs.read_result_file(self.ctx(), self.job(job_id))
        self.assertEqual(state, "invalid")
        self.assertLess(len(info["reason"]), 600)

    def test_the_record_size_is_measured_as_stored(self):
        self.assertEqual(jobs.MAX_RECORD_BYTES, 8 * 1024)
        # 3000 x U+0080 is 6000 bytes of UTF-8 but 18000 bytes in the ledger
        job_id = self.surrogate_job("\u0080" * 3000)
        before = len(self.ledger_bytes())
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.job(job_id)["result"]["cause"], "invalid_result")
        self.assertLess(len(self.ledger_bytes()) - before, 6000)


class C3NoUncappedWorkerText(Round4):
    def test_a_huge_start_json_worktree_is_never_banked_for_a_read_job(self):
        job_id, _dead = self.launched_dead()
        before = len(self.ledger_bytes())
        start = {"job_id": job_id, "state": "starting",
                 "worktree": {"path": "\U0001F600" * 200_000, "branch": "\U0001F600" * 10_000}}
        # raw UTF-8 keeps the file under the 1 MiB read limit; the ledger would store each char as 12 ASCII bytes
        (self.state / "runs" / job_id / "start.json").write_text(json.dumps(start, ensure_ascii=False), encoding="utf-8")
        code, out = self.inproc("reconcile")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.job(job_id)["state"], "banked")
        self.assertLess(len(self.ledger_bytes()) - before, 5000)

    def test_rule_w_keep_uses_only_the_ledger_plan(self):
        job = {"worktree_plan": None, "workdir": None}
        start = {"worktree": {"path": "/x" * 100, "branch": "b"}}
        kept = reconcile.rule_w(job, start)
        self.assertEqual((kept["path"], kept["branch"]), ("?", "?"))

    def test_an_oversize_removal_record_reason_is_capped(self):
        repo = self.repo()
        plan = {"path": str(self.dir / "wt-gone"), "branch": "job-gone"}
        run_dir = self.dir / "run"
        run_dir.mkdir()
        record = dict(plan, kept=False, reason="r" * 60_000)
        (run_dir / reconcile.REMOVAL_FILE).write_text(json.dumps(record), encoding="utf-8")
        with mock.patch.dict(os.environ, git_env(self.dir / "home")):
            got = reconcile.persisted_removal(run_dir, plan, str(repo))
        self.assertIsNotNone(got)
        self.assertLessEqual(len(got["reason"]), 500)

    def result(self, **over):
        obs = jobs.synth("failed", "lost", "note")
        obs.pop("interrupted", None)
        obs.update(banked_at="2026-01-01T00:00:00Z", bank_revision=1)
        obs.update(over)
        return obs

    def test_check_result_accepts_a_normal_result(self):
        wt = {"path": "/p" * 100, "branch": "b", "kept": True, "reason": "r"}
        L.check_result(self.result(worktree=wt), "r")

    def test_check_result_rejects_an_oversize_field(self):
        wt = {"path": "/p", "branch": "b", "kept": True, "reason": "r"}
        cases = {
            "path": dict(wt, path="p" * 5000), "branch": dict(wt, branch="b" * 2000), "reason": dict(wt, reason="r" * 2000)}
        for name, worktree in cases.items():
            with self.subTest(name), self.assertRaises(L.LedgerInvalid):
                L.check_result(self.result(worktree=worktree), "r")
        with self.assertRaises(L.LedgerInvalid):
            L.check_result(self.result(note="n" * 2000), "r")
        with self.assertRaises(L.LedgerInvalid):
            L.check_result(self.result(evidence=[{"kind": "k", "reference": "r" * 5000, "sha256": "0" * 64}]), "r")


class Sev3(Round4):
    def test_cancel_busy_after_a_signal_says_changed_and_not_nothing_was_signalled(self):
        job_id = self.start_sleeper()
        verdicts = iter([("alive", "x")])

        def verdict(ctx, job):
            return next(verdicts, ("gone", "x"))

        def fake_signal(ctx, jid, disp, numbers):
            reconcile._ACTED[0] = True
            return "sent", None

        @contextlib.contextmanager
        def busy(ctx, wait=0.0):
            yield False

        with mock.patch.object(reconcile, "dispatcher_verdict", verdict), \
                mock.patch.object(reconcile, "signal_dispatcher", fake_signal), \
                mock.patch.object(jobs, "collect_all", return_value=[]), \
                mock.patch.object(reconcile, "reconcile_lock", busy):
            code, out = self.inproc("cancel", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertNotIn("nothing was signalled", out["note"])

    def test_cancel_busy_before_any_signal_still_says_nothing_was_signalled(self):
        code, out = jobs._cancel_busy("j")
        self.assertEqual((code, out["changed"]), (5, False))
        self.assertIn("nothing was signalled", out["note"])

    def test_another_jobs_error_keeps_its_own_row_text(self):
        ids = sorted(self.launched_dead()[0] for _ in range(2))

        def fake(ctx, job_id, *args, **kw):
            if job_id == ids[0]:
                reconcile._ACTED[0] = True
                return reconcile.report(job_id, "banked", "acted")  # the act is in the flag, not on the row
            raise RuntimeError("boom")

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        rows = {r["job_id"]: r for r in out["results"]}
        self.assertIn("use --force-bank", rows[ids[1]]["action"], out)
        self.assertNotIn("changed", rows[ids[1]])
        self.assertEqual((code, out.get("changed")), (0, None), out)  # fix 7: the untouched other job is not behind

    def test_a_changed_row_makes_the_whole_run_exit_5_changed(self):
        job_id, _dead = self.launched_dead()

        def fake(ctx, jid, *args, **kw):
            return reconcile.report(jid, "unresolved", "x", changed=True)

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertIn("error", out)

    def test_a_sweep_that_times_out_keeps_its_kills_in_acted(self):
        with mock.patch.object(reconcile, "SWEEP_CODE", "import time\ntime.sleep(30)\n"), \
                mock.patch.object(reconcile, "SWEEP_TIMEOUT", 0.5):
            swept = reconcile.sweep("marker")
        self.assertTrue(reconcile.sweep_failed(swept))
        self.assertTrue(reconcile.acted())


if __name__ == "__main__":
    unittest.main()
