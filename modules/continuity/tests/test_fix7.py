"""R9-U04a fix round 7: one "behind" predicate, a verified-kill sweep that timed out, untouched jobs, a waited refusal."""

import contextlib
import io
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

from helpers import L, alive, dispatch, jobs, reconcile, wait_for  # noqa: E402
from test_fix4 import Round4  # noqa: E402


class G1CancelRunningChanged(Round4):
    def test_cancel_after_its_own_sigterm_with_a_running_changed_row_is_exit_5(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        (self.state / "config.json").write_text(json.dumps({"cancel_wait_seconds": 0.5}))
        real, real_signal = reconcile.dispatcher_verdict, reconcile.signal_dispatcher

        def signal_then_garble(*args, **kw):  # the SIGTERM is real; the dying dispatcher's result is then replaced
            result = real_signal(*args, **kw)
            time.sleep(1)
            (self.state / "runs" / job_id / "result.json").write_text("not json at all")
            return result

        def verdict(ctx, job):  # the dispatcher survives the SIGTERM (the signal itself was real)
            return ("alive", "") if reconcile.acted() else real(ctx, job)

        with mock.patch.object(reconcile, "dispatcher_verdict", verdict), \
                mock.patch.object(reconcile, "signal_dispatcher", signal_then_garble), \
                mock.patch.object(jobs, "collect_all", return_value=[]):
            code, out = self.inproc("cancel", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertEqual(out["reconcile"]["status"], "running", out)
        self.assertTrue(wait_for(lambda: not alive(pid)), "the SIGTERM never reached the dispatcher")

    def test_behind_row_is_one_predicate(self):  # control for the helper both callers share
        self.assertTrue(reconcile.behind_row({"status": "running", "changed": True}))
        self.assertTrue(reconcile.behind_row({"status": "unresolved", "changed": True}))
        self.assertFalse(reconcile.behind_row({"status": "unresolved"}))
        self.assertFalse(reconcile.behind_row({"status": "running"}))
        self.assertFalse(reconcile.behind_row({"status": "banked", "changed": True}))


class G2VerifiedKillSweep(Round4):
    def test_a_timed_out_sweep_after_the_worker_kill_is_unresolved_and_sweeps_again(self):
        self.fake("worker", "import subprocess\n"
                  "p = subprocess.Popen(['sleep', '300'], start_new_session=True)\n"
                  "open(os.path.join(here, 'child'), 'w').write(str(p.pid))\n"
                  "time.sleep(60)")
        code, out = self.start()
        self.assertEqual(code, 0, out)
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: self.start_json(job_id).get("state") == "running"
                                 and (self.bin / "child").exists()), "worker never ran")
        child = int((self.bin / "child").read_text())
        self.addCleanup(lambda: alive(child) and os.kill(child, signal.SIGKILL))
        os.kill(self.job(job_id)["dispatcher"]["pid"], signal.SIGKILL)
        self.assertTrue(wait_for(lambda: reconcile.dispatcher_verdict(self.ctx(), self.job(job_id))[0] == "gone"))
        with mock.patch.object(reconcile, "SWEEP_CODE", "import time\ntime.sleep(30)\n"), \
                mock.patch.object(reconcile, "SWEEP_TIMEOUT", 0.5):
            code, out = self.inproc("reconcile")
        row = out["results"][0]
        self.assertEqual((code, row["status"], row.get("changed"), out.get("changed")), (5, "unresolved", True, True), out)
        self.assertIn("worker killed; marker sweep did not finish", row["action"])
        self.assertEqual(self.job(job_id)["state"], "launched")
        self.assertTrue(alive(child), "the marked child must still be alive (recorded, not hidden)")
        real, calls = reconcile.sweep, []

        def counting(marker):
            calls.append(marker)
            return real(marker)

        with mock.patch.object(reconcile, "sweep", counting):
            code, out = self.inproc("reconcile")
        self.assertTrue(calls, "the second reconcile did not sweep again")
        self.assertEqual((code, out["results"][0]["status"]), (0, "banked"), out)
        self.assertTrue(wait_for(lambda: not alive(child)))


class G3UntouchedJobsAreNotBehind(Round4):
    def untouched_unresolved(self):
        job_id, _dead = self.launched_dead()
        bystander = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.reap_proc, bystander)
        (self.state / "runs" / job_id / "start.json").write_text(
            json.dumps({"job_id": job_id, "state": "running", "pid": bystander.pid}))  # identity unknown: never killed
        return job_id

    def test_reconcile_with_a_kill_banked_and_an_untouched_unresolved_job_exits_0(self):
        a = self.cancelled_sleeper()
        c = self.untouched_unresolved()
        code, out = self.inproc("reconcile")
        rows = {r["job_id"]: r for r in out["results"]}
        self.assertEqual((code, out.get("changed")), (0, None), out)
        self.assertEqual((rows[a]["status"], rows[c]["status"]), ("banked", "unresolved"))
        self.assertNotIn("changed", rows[c])

    def test_drain_wait_does_not_abort_early_on_an_untouched_unresolved_job(self):
        self.cancelled_sleeper()
        c = self.untouched_unresolved()
        started = time.monotonic()
        code, out = self.inproc("drain", "wait", "--timeout", "3")
        self.assertEqual((code, out.get("state"), out.get("changed")), (5, "PENDING", None), out)
        self.assertGreaterEqual(time.monotonic() - started, 2.5)
        self.assertEqual(out["pending"], [c])


class G4WaitOnARefusedResult(Round4):
    def test_wait_without_timeout_stops_with_exit_2_naming_force_bank(self):
        job_id, _dead = self.launched_dead()
        record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        real = jobs.bank_in

        def refuse(doc, jid, obs):
            if obs["trust"] != "recovery":
                raise L.ResultRefused("the result cannot be banked (injected)")
            return real(doc, jid, obs)

        with mock.patch.object(jobs, "bank_in", refuse):
            finished, value = self.in_thread(lambda: self.inproc("wait", "--job", job_id), timeout=15)
        self.assertTrue(finished, "wait looped on a refused result")
        code, out = value
        self.assertEqual(code, 2, out)
        self.assertIn("--force-bank", out["error"])
        self.assertLessEqual(len(out["error"]), 400)


class G5BankRefusalReason(Round4):
    def test_an_unexpected_exception_stays_fail_closed_and_names_its_class(self):
        job_id, _dead = self.launched_dead()
        record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        err = io.StringIO()
        with mock.patch.object(jobs, "bank_in", side_effect=KeyError("boom")), contextlib.redirect_stderr(err):
            self.assertIsNone(reconcile.bank_refusal(self.ctx(), job_id))
        self.assertIn("KeyError", err.getvalue())
        self.assertLess(len(err.getvalue()), 300)


if __name__ == "__main__":
    unittest.main()
