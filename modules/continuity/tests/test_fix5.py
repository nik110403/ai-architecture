"""R9-U04a fix round 5: cancel's own signal is never hidden, the acted rule at the exit, bank-time refusals, byte caps."""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import L, alive, continuity, dispatch, jobs, reconcile  # noqa: E402
from test_fix4 import Round4  # noqa: E402


class D1CancelsOwnSignal(Round4):
    def test_cancel_whose_job_then_errors_is_exit_5_changed_not_exit_0(self):
        job_id = self.start_sleeper()
        pid = self.job(job_id)["dispatcher"]["pid"]
        with mock.patch.object(jobs, "collect_all", return_value=[]), \
                mock.patch.object(reconcile, "launched_rows", side_effect=RuntimeError("boom")):
            code, out = self.inproc("cancel", "--job", job_id)
        self.assertEqual((code, out.get("changed")), (5, True), out)
        self.assertFalse(alive(pid))

    def test_the_exit_enforces_the_rule_for_a_command_that_acted_and_left_a_row_unresolved(self):
        reconcile.reset_acted()
        reconcile._ACTED[0] = True
        row = reconcile.report("j", "unresolved", "x", changed=True)  # a row of a job this command acted on
        self.assertEqual(continuity.enforce_acted(0, {"reconcile": row}), (5, {"reconcile": row, "changed": True}))
        self.assertEqual(continuity.enforce_acted(0, {"job_id": "j"}), (0, {"job_id": "j"}))  # banked cleanly: 0
        untouched = reconcile.report("j", "unresolved", "x")  # negative control: acted, but this row was never touched
        self.assertEqual(continuity.enforce_acted(0, {"reconcile": untouched}), (0, {"reconcile": untouched}))
        self.assertEqual(continuity.enforce_acted(7, {"reconcile": row})[0], 7)
        reconcile.reset_acted()
        self.assertEqual(continuity.enforce_acted(0, {"reconcile": row})[0], 0)  # nothing acted: unchanged


class F2ReconcileAll(Round4):
    def test_a_kill_in_one_job_and_an_error_in_an_untouched_other_exits_0(self):
        ids = sorted(self.launched_dead()[0] for _ in range(2))

        def fake(ctx, job_id, *args, **kw):
            if job_id == ids[0]:
                reconcile._ACTED[0] = True
                return reconcile.report(job_id, "banked", "killed and banked")  # no `changed` on the row
            raise RuntimeError("boom")

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        rows = {r["job_id"]: r for r in out["results"]}
        self.assertIn("use --force-bank", rows[ids[1]]["action"], out)
        self.assertNotIn("changed", rows[ids[1]])
        self.assertEqual((code, out.get("changed")), (0, None), out)  # fix 7: an untouched unresolved job is not behind

    def test_a_kill_with_everything_banked_stays_exit_0(self):
        job_id, _dead = self.launched_dead()

        def fake(ctx, jid, *args, **kw):
            reconcile._ACTED[0] = True
            return reconcile.report(jid, "banked", "killed and banked")

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        self.assertEqual((code, out.get("changed")), (0, None), out)


class SweepFailedRow(Round4):
    def test_a_starting_job_whose_sweep_times_out_is_exit_5_and_the_row_is_changed(self):
        job_id, _dead = self.launched_dead()
        (self.state / "runs" / job_id / "start.json").write_text(json.dumps({"job_id": job_id, "state": "starting"}))
        with mock.patch.object(reconcile, "SWEEP_CODE", "import time\ntime.sleep(30)\n"), \
                mock.patch.object(reconcile, "SWEEP_TIMEOUT", 0.5):
            code, out = self.inproc("reconcile")
        row = out["results"][0]
        self.assertEqual((code, row["status"], row.get("changed"), out.get("changed")), (5, "unresolved", True, True), out)
        self.assertIn("did not finish", row["action"])


class RunPostLaunch(Round4):
    def run_with_failing_finish(self, act):
        self.fake("worker", "print('ok')")

        def finish(*args, **kw):
            reconcile._ACTED[0] = act
            raise L.LedgerBusy("ledger busy: injected")

        with mock.patch.object(jobs, "finish_run", finish):
            return self.inproc("run", *self.launch_args())

    def test_run_exit_6_says_changed_when_something_was_killed(self):
        code, out = self.run_with_failing_finish(True)
        self.assertEqual((code, out["changed"]), (6, True), out)

    def test_run_exit_6_says_not_changed_otherwise(self):
        code, out = self.run_with_failing_finish(False)
        self.assertEqual((code, out["changed"]), (6, False), out)


class CheckResultRefusal(Round4):
    def test_an_over_long_worktree_root_is_refused_at_admit(self):
        repo = self.repo()
        root = str(self.dir / ("w" * 4100))
        cfg = self.config(permit=["builder"], allow_write=True, worktree_root=root)
        code, out = self.inproc("start", "--config", cfg, "--role", "builder", "--intent", "x", "--workdir", str(repo))
        self.assertEqual(code, 2, out)
        self.assertIn("worktree_root is too long", out["error"])
        self.assertEqual(self.doc()["jobs"], {})

    def test_a_result_check_result_refuses_leaves_that_job_unresolved_and_the_others_go_on(self):
        ids = sorted(self.launched_dead()[0] for _ in range(2))
        bad = jobs.synth("failed", "lost", "n", evidence=[{"kind": "k", "reference": "r" * 5000, "sha256": "0" * 64}])

        def fake(ctx, job_id, *args, **kw):
            obs = bad if job_id == ids[0] else jobs.synth("failed", "lost", "fine")
            return reconcile.banked(ctx, job_id, obs, "x")

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        rows = {r["job_id"]: r for r in out["results"]}
        self.assertEqual((rows[ids[0]]["status"], rows[ids[1]]["status"]), ("unresolved", "banked"), out)
        self.assertIn("cannot be banked", rows[ids[0]]["action"])
        self.assertEqual((self.job(ids[0])["state"], self.job(ids[1])["state"]), ("launched", "banked"))
        self.assertEqual(code, 0, out)  # a healthy ledger: not exit 4

    def test_check_result_refuses_an_oversize_record(self):
        obs = jobs.synth("failed", "lost", "n")
        obs.pop("interrupted", None)
        obs.update(banked_at="2026-01-01T00:00:00Z", bank_revision=1)
        # big but schema-valid: many evidence items of 1 KiB kinds
        big = dispatch.make_result("j", "succeeded", [{"kind": "k" * 1000, "reference": "r" * 4000, "sha256": "0" * 64}
                                                      for _ in range(3)])
        self.assertGreater(L.stored_size(big), L.MAX_RECORD_BYTES)
        with self.assertRaises(L.LedgerInvalid):
            L.check_result(dict(obs, record=big), "r")
        small = dispatch.make_result("j", "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        L.check_result(dict(obs, record=small), "r")


class ByteCounts(Round4):
    def test_the_record_cap_counts_stored_bytes_not_characters(self):
        # 2000 non-BMP characters: 2000 chars, but 12 stored bytes each
        record = dispatch.make_result("j", "succeeded", [{"kind": "k", "reference": "\U0001F600" * 2000,
                                                          "sha256": "0" * 64}])
        self.assertLess(len(json.dumps(record, ensure_ascii=False)), L.MAX_RECORD_BYTES)
        self.assertGreater(L.stored_size(record), L.MAX_RECORD_BYTES)
        job = {"job_id": "j", "access": "read", "worktree_plan": None}
        self.assertIsNone(jobs.observation_from_envelope(job, {"result": record}, "0" * 64, "file"))


class F5RowText(Round4):
    def test_a_job_that_did_not_act_does_not_claim_it_killed_anything(self):
        ids = sorted(self.launched_dead()[0] for _ in range(2))

        def fake(ctx, job_id, *args, **kw):
            if job_id == ids[0]:
                reconcile._ACTED[0] = True
                return reconcile.report(job_id, "banked", "killed and banked")
            raise L.LedgerBusy("ledger busy: injected")

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        row = {r["job_id"]: r for r in out["results"]}[ids[1]]
        self.assertEqual((code, row.get("changed")), (5, True), out)
        self.assertIn("another job in this run acted", row["action"])
        self.assertNotIn("killed or a worktree removed", row["action"])

    def test_a_job_that_acted_itself_keeps_the_own_text(self):
        job_id, _dead = self.launched_dead()

        def fake(ctx, jid, *args, **kw):
            reconcile._ACTED[0] = True
            raise L.LedgerBusy("ledger busy: injected")

        with mock.patch.object(reconcile, "reconcile_job", fake):
            code, out = self.inproc("reconcile")
        self.assertIn("processes were killed or a worktree removed", out["results"][0]["action"])


class StdoutEnvelopeGuard(Round4):
    def test_an_exception_from_the_envelope_observation_is_no_observation_not_a_traceback(self):
        job_id, _dead = self.launched_dead()
        (self.state / "spool").mkdir(exist_ok=True)
        (self.state / "spool" / f"{job_id}.out").write_text(json.dumps({"result": {}, "dispatch": {}}))
        with mock.patch.object(jobs, "observation_from_envelope", side_effect=UnicodeEncodeError("utf-8", "", 0, 1, "x")), \
                mock.patch.object(jobs, "refusal_observation", return_value=None):
            obs, state, _info = jobs.observe_result(self.ctx(), self.job(job_id), "file")
        self.assertIsNone(obs)


if __name__ == "__main__":
    unittest.main()
