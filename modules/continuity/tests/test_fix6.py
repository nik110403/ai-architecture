"""R9-U04a fix round 6: a live running job is not "behind the world", the row clause, sweep rows, refused results."""

import contextlib
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import L, alive, continuity, dispatch, jobs, reconcile, wait_for  # noqa: E402
from test_fix4 import Round4  # noqa: E402


@contextlib.contextmanager
def busy_lock():
    @contextlib.contextmanager
    def busy(ctx, wait=0.0):
        yield False

    with mock.patch.object(reconcile, "reconcile_lock", busy):
        yield


class E1LiveRunningIsNotBehind(Round4):
    """Real processes: job A is cancelled and killed, job B is a healthy worker that sleeps."""

    def kill_a_with_b_running(self):
        a = self.cancelled_sleeper()
        self.fake("worker", "time.sleep(4)\nprint('done')")
        code, out = self.start()
        self.assertEqual(code, 0, out)
        b = out["job_id"]
        self.assertTrue(wait_for(lambda: self.start_json(b).get("state") == "running"), "B never ran")
        return a, b

    def test_drain_wait_keeps_waiting_after_an_earlier_kill_was_banked(self):
        a, b = self.kill_a_with_b_running()
        code, out = self.inproc("drain", "wait", "--timeout", "60")
        self.assertEqual((code, out.get("state"), out.get("changed")), (0, "DRAINED", None), out)
        self.assertEqual((self.job(a)["state"], self.job(b)["state"]), ("banked", "banked"))

    def test_reconcile_with_a_kill_banked_and_a_live_job_exits_0(self):
        a, b = self.kill_a_with_b_running()
        code, out = self.inproc("reconcile")
        rows = {r["job_id"]: r for r in out["results"]}
        self.assertEqual((code, out.get("changed")), (0, None), out)
        self.assertEqual((rows[a]["status"], rows[b]["status"]), ("banked", "running"))
        self.assertNotIn("changed", rows[b])

    def test_wait_for_the_live_job_exits_0_after_the_other_was_killed_and_banked(self):
        a, b = self.kill_a_with_b_running()
        self.assertEqual(self.inproc("reconcile")[0], 0)
        code, out = self.inproc("wait", "--job", b, "--timeout", "60")
        self.assertEqual((code, out["state"], out["outcome"]), (0, "banked", "succeeded"), out)
        self.assertEqual(self.job(a)["state"], "banked")


class E2RowClause(Round4):
    def test_a_row_left_open_after_cancels_own_sigterm_is_changed(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        real, calls = reconcile.dispatcher_verdict, []

        def verdict(ctx, job):
            calls.append(1)
            return real(ctx, job) if len(calls) == 1 else ("unknown", "injected")  # after the SIGTERM

        with mock.patch.object(reconcile, "dispatcher_verdict", verdict):
            code, out = self.inproc("cancel", "--job", job_id)
        row = out["reconcile"]
        self.assertIn("dispatcher identity unknown", row["action"], out)
        self.assertEqual((code, row.get("changed"), out.get("changed")), (5, True, True), out)
        self.assertTrue(wait_for(lambda: not alive(pid)))


class RunExit5(Round4):
    def finish(self, row=None):
        job_id, dead = self.launched_dead()
        reconcile._ACTED[0] = True  # a SIGCONT/SIGTERM went out while `run` waited
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(jobs, "observe_result", return_value=(None, "absent", None)))
        if row is None:
            stack.enter_context(busy_lock())
        else:
            stack.enter_context(mock.patch.object(reconcile, "gone_rows", return_value=row))
        with stack:
            return jobs.finish_run(self.ctx(), job_id, dead, 0)

    def test_run_exit_5_on_a_busy_lock_says_changed_after_its_own_signal(self):
        code, out = self.finish()
        self.assertEqual((code, out["changed"]), (5, True), out)

    def test_run_exit_5_with_a_reconcile_row_says_changed_after_its_own_signal(self):
        code, out = self.finish(reconcile.report("j", "unresolved", "x"))
        self.assertEqual((code, out["changed"]), (5, True), out)

    def test_run_exit_5_without_an_act_says_not_changed(self):
        job_id, dead = self.launched_dead()
        with mock.patch.object(jobs, "observe_result", return_value=(None, "absent", None)), busy_lock():
            code, out = jobs.finish_run(self.ctx(), job_id, dead, 0)
        self.assertEqual((code, out["changed"]), (5, False), out)

    def test_enforce_acted_adds_changed_to_a_code_5_payload_with_an_open_row_only(self):
        reconcile._ACTED[0] = True
        row = reconcile.report("j", "unresolved", "x", changed=True)
        self.assertEqual(continuity.enforce_acted(5, {"reconcile": row}), (5, {"reconcile": row, "changed": True}))
        self.assertEqual(continuity.enforce_acted(5, {"reconcile": row, "changed": False})[1]["changed"], False)
        self.assertEqual(continuity.enforce_acted(5, {"pending": True}), (5, {"pending": True}))
        running = reconcile.report("j", "running", "x")
        self.assertEqual(continuity.enforce_acted(0, {"results": [running]}), (0, {"results": [running]}))
        untouched = reconcile.report("j", "unresolved", "x")  # negative control: acted, row untouched and unmarked
        self.assertEqual(continuity.enforce_acted(0, {"results": [untouched]}), (0, {"results": [untouched]}))


class RunningBranchSweep(Round4):
    def test_a_timed_out_sweep_in_the_running_branch_is_unresolved_changed_and_sweeps_again_later(self):
        job_id, _dead = self.launched_dead()
        (self.state / "runs" / job_id / "start.json").write_text(
            json.dumps({"job_id": job_id, "state": "running", "pid": self.dead_pid()}))
        with mock.patch.object(reconcile, "SWEEP_CODE", "import time\ntime.sleep(30)\n"), \
                mock.patch.object(reconcile, "SWEEP_TIMEOUT", 0.5):
            code, out = self.inproc("reconcile")
        row = out["results"][0]
        self.assertEqual((code, row["status"], row.get("changed"), out.get("changed")), (5, "unresolved", True, True), out)
        self.assertIn("did not finish", row["action"])
        self.assertEqual(self.job(job_id)["state"], "launched")
        code, out = self.inproc("reconcile")  # the real sweep this time: now it banks
        self.assertEqual((code, out["results"][0]["status"]), (0, "banked"), out)


class RefusedResult(Round4):
    def two_jobs_one_refused(self):
        ids = sorted(self.launched_dead()[0] for _ in range(2))
        for job_id in ids:
            record = dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
            self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        real = jobs.bank_in

        def refuse(doc, job_id, obs):
            if job_id == ids[0] and obs["trust"] != "recovery":  # the worker's result is refused, not an operator row
                raise L.ResultRefused(f"job {job_id}: the result cannot be banked (injected)")
            return real(doc, job_id, obs)

        return ids, mock.patch.object(jobs, "bank_in", refuse)

    def test_a_refused_result_does_not_poison_status_collect_and_wait_for_the_others(self):
        ids, patch = self.two_jobs_one_refused()
        with patch:
            for argv in (("status",), ("collect",), ("wait", "--job", ids[1], "--timeout", "5")):
                code, out = self.inproc(*argv)
                self.assertIn(code, (0, 1), (argv, out))
        self.assertEqual((self.job(ids[0])["state"], self.job(ids[1])["state"]), ("launched", "banked"))

    def test_force_bank_closes_a_job_whose_result_is_refused_as_invalid_result(self):
        ids, patch = self.two_jobs_one_refused()
        with patch:
            code, out = self.inproc("reconcile", "--force-bank", ids[0], "--reason", "r" * 900)
        self.assertEqual(code, 0, out)
        result = self.job(ids[0])["result"]
        self.assertEqual((result["outcome"], result["cause"]), ("failed", "invalid_result"))
        self.assertLess(len(result["note"]), 600)
        self.assertEqual(self.job(ids[1])["state"], "launched")

    def test_force_bank_still_refuses_a_job_whose_result_would_bank_fine(self):
        ids, patch = self.two_jobs_one_refused()
        with patch:
            code, out = self.inproc("reconcile", "--force-bank", ids[1], "--reason", "x")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.job(ids[1])["state"], "launched")  # nothing was banked by the check

    def test_a_result_refused_after_cancels_own_signal_is_exit_5_changed(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        with mock.patch.object(jobs, "collect_all", side_effect=L.ResultRefused("the result cannot be banked")):
            code, out = self.inproc("cancel", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertTrue(wait_for(lambda: not alive(pid)))

    def test_a_refusal_with_nothing_acted_stays_exit_2(self):
        with mock.patch.object(continuity, "dispatch_command", side_effect=L.ResultRefused("x")):
            code, out = self.inproc("status")
        self.assertEqual((code, "changed" in out), (2, False), out)


class EnvelopeGuard(Round4):
    def test_a_raising_envelope_is_an_invalid_observation_with_a_capped_reason(self):
        job_id, _dead = self.launched_dead()
        (self.state / "spool").mkdir(exist_ok=True)
        (self.state / "spool" / f"{job_id}.out").write_text(json.dumps({"result": {}, "dispatch": {}}))
        with mock.patch.object(jobs, "observation_from_envelope", side_effect=UnicodeEncodeError("utf-8", "", 0, 1, "x")), \
                mock.patch.object(jobs, "refusal_observation", return_value=None):
            obs, state, info = jobs.observe_result(self.ctx(), self.job(job_id), "file")
            code, out = self.inproc("reconcile")
        self.assertEqual((obs, state), (None, "invalid"))
        self.assertRegex(info["sha256"], r"^[0-9a-f]{64}$")
        self.assertLessEqual(len(info["reason"]), 500)
        self.assertIn("UnicodeEncodeError", info["reason"])
        self.assertEqual(self.job(job_id)["result"]["cause"], "invalid_result")


if __name__ == "__main__":
    unittest.main()
