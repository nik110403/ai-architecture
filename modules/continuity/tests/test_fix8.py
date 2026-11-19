"""R9-U04a fix round 8: start with a pending cancel, live jobs and wait/force-bank, bank_refusal output, stable unresolved wait."""

import contextlib
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import L, alive, dispatch, jobs, reconcile, wait_for  # noqa: E402
from test_fix4 import CANCEL, Round4  # noqa: E402


class LiveRefused(Round4):
    def refuse_live(self):
        """A live running job whose planted result the ledger would refuse; returns (job_id, dispatcher pid, patch)."""
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        real = jobs.bank_in

        def refuse(doc, jid, obs):
            if obs["trust"] != "recovery":
                raise L.ResultRefused("the result cannot be banked (injected)")
            return real(doc, jid, obs)

        return job_id, pid, mock.patch.object(jobs, "bank_in", refuse)


class H1StartPendingCancel(Round4):
    def test_start_that_signals_a_pending_cancel_is_exit_5_changed(self):
        real = jobs.record_dispatcher

        def record_with_cancel(ctx, job_id, pid):
            real(ctx, job_id, pid)
            self.edit(lambda d: d["jobs"][job_id].update(cancel=CANCEL))
            return dict(self.job(job_id)["dispatcher"])

        self.fake("worker", "time.sleep(60)")
        with mock.patch.object(jobs, "record_dispatcher", record_with_cancel), \
                mock.patch.object(jobs, "collect_all", return_value=[]):
            code, out = self.inproc("start", *self.launch_args())
        self.assertEqual((code, out.get("changed")), (5, True), out)
        pid = self.job(out["job_id"])["dispatcher"]["pid"]
        self.assertTrue(wait_for(lambda: not alive(pid)), "the pending cancel never signalled the dispatcher")


class H2H3LiveJob(LiveRefused):
    def test_wait_on_a_live_job_with_a_refusable_result_keeps_waiting(self):
        job_id, pid, patch = self.refuse_live()
        with patch:
            code, out = self.inproc("wait", "--job", job_id, "--timeout", "2")
        self.assertEqual((code, out.get("pending")), (5, True), out)
        self.assertTrue(alive(pid))

    def test_force_bank_refuses_a_live_dispatcher(self):
        job_id, pid, patch = self.refuse_live()
        with patch:
            code, out = self.inproc("reconcile", "--force-bank", job_id, "--reason", "x")
        self.assertEqual(code, 2, out)
        self.assertIn("dispatcher alive", out["error"])
        self.assertLessEqual(len(out["error"]), 400)
        self.assertNotEqual(self.job(job_id)["state"], "banked")
        self.assertTrue(alive(pid))


class H4BankRefusalOutput(Round4):
    def planted(self):
        job_id, _dead = self.launched_dead()
        record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        return job_id

    def test_no_stderr_leaves_stdout_clean(self):
        job_id = self.planted()
        out = io.StringIO()
        reconcile._ASSESS_WARNED.clear()
        with mock.patch.object(jobs, "bank_in", side_effect=KeyError("boom")), \
                contextlib.redirect_stdout(out), mock.patch.object(sys, "stderr", None):
            self.assertIsNone(reconcile.bank_refusal(self.ctx(), job_id))
        self.assertEqual(out.getvalue(), "")

    def test_the_line_is_printed_once_per_job(self):
        job_id = self.planted()
        err = io.StringIO()
        reconcile._ASSESS_WARNED.clear()
        with mock.patch.object(jobs, "bank_in", side_effect=KeyError("boom")), contextlib.redirect_stderr(err):
            for _ in range(3):
                reconcile.bank_refusal(self.ctx(), job_id)
        self.assertEqual(err.getvalue().count("KeyError"), 1)


class H5StableUnresolvedWait(Round4):
    def test_wait_without_timeout_on_an_untouched_unresolved_job_exits_2(self):
        job_id, _dead = self.launched_dead()
        bystander = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, bystander)
        (self.state / "runs" / job_id / "start.json").write_text(
            json.dumps({"job_id": job_id, "state": "running", "pid": bystander.pid}))  # identity unknown: never killed
        started = time.monotonic()
        finished, value = self.in_thread(lambda: self.inproc("wait", "--job", job_id), timeout=15)
        self.assertTrue(finished, "wait looped on a stable unresolved row")
        code, out = value
        self.assertEqual(code, 2, out)
        self.assertIn("reconcile", out["error"])
        self.assertIn("--force-bank", out["error"])
        self.assertLess(time.monotonic() - started, 15)


if __name__ == "__main__":
    unittest.main()
