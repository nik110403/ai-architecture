"""R9-U04-F2: the frozen SEV3 follow-ups of U04a-F and U04b (force-bank liveness, consume after print, Next budget,
usage staleness, evidence refs, null-budget pool)."""

import fcntl
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import CONT, ContCase, L, alive, reconcile  # noqa: E402
from modules.continuity import receipts, resume  # noqa: E402
import test_fix8  # noqa: E402  (module imports only: a class imported by name would rerun its tests here)
import test_hooks  # noqa: E402
from test_receipts import CpMixin  # noqa: E402  (a mixin: no tests of its own)

import test_fix4  # noqa: E402
import test_jobs  # noqa: E402

USAGE, assistant = test_hooks.USAGE, test_hooks.assistant

START = CONT / "hooks" / "session_start.py"


class Base(CpMixin, ContCase):
    def setUp(self):
        super().setUp()
        self.write("ev.txt", "evidence v1")
        self.usage(500)

    def setup_config(self, **cfg):
        (self.state / "config.json").write_text(json.dumps(cfg))


class ForceBankLiveness(test_fix8.LiveRefused):
    def test_launching_job_with_a_live_dispatcher_process_is_not_force_banked(self):
        job_id, pid, patch = self.refuse_live()
        self.edit(lambda d: d["jobs"][job_id].update(state="launching", dispatcher=None))
        with patch:
            code, out = self.inproc("reconcile", "--force-bank", job_id, "--reason", "x")
        self.assertEqual(code, 2, out)
        self.assertNotEqual(self.job(job_id)["state"], "banked")
        self.assertTrue(alive(pid))

    def test_unknown_liveness_is_not_force_banked(self):
        job_id, pid, patch = self.refuse_live()
        with patch, mock.patch.object(reconcile, "liveness", return_value=("unknown", "injected")):
            code, out = self.inproc("reconcile", "--force-bank", job_id, "--reason", "x")
        self.assertEqual(code, 2, out)
        self.assertNotEqual(self.job(job_id)["state"], "banked")
        self.assertTrue(alive(pid))


class OrphanedWording(test_jobs.Seams):
    def test_an_unknown_recheck_neither_claims_a_kill_nor_a_dead_worker(self):
        job_id = self.start_sleeper()
        self.kill_dispatcher(job_id)
        with mock.patch.object(reconcile, "worker_verdict", side_effect=[("verified", "ok"), ("unknown", "x")]):
            code, out = self.inproc("reconcile")
        row = out["results"][0]
        self.assertEqual(row["status"], "banked", out)
        note = self.job(job_id)["result"]["note"]
        self.assertNotIn("already gone", note)
        self.assertNotIn("killed", row["action"])
        self.assertIn("identity unknown", note)


class BankRefusalStderr(test_fix4.Round4):
    def test_a_closed_or_broken_stderr_is_not_a_traceback(self):
        job_id, _dead = self.launched_dead()
        record = reconcile.dispatch.make_result(job_id, "succeeded", [{"kind": "k", "reference": "r", "sha256": "0" * 64}])
        self.planted_result(job_id, json.dumps({"result": record, "dispatch": {}}))
        closed = io.StringIO()
        closed.close()

        class Broken(io.StringIO):
            def write(self, text):
                raise BrokenPipeError("gone")

        for stream in (closed, Broken()):
            reconcile._ASSESS_WARNED.clear()
            with mock.patch.object(reconcile.jobs, "bank_in", side_effect=KeyError("boom")), \
                    mock.patch.object(sys, "stderr", stream):
                self.assertIsNone(reconcile.bank_refusal(self.ctx(), job_id))


class ConsumeAfterPrint(test_hooks.HookCase):
    def setUp(self):
        super().setUp()
        self.write("ev.txt")
        self.cp(nxt="hook next step", session=None)

    def run_in_process(self, out):
        spec = importlib.util.spec_from_file_location("start_hook_under_test", START)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.start_payload()))), \
                mock.patch.object(sys, "stdout", out):
            module.main()

    def test_a_failed_print_leaves_the_next_unconsumed(self):
        class Broken(io.StringIO):
            def write(self, text):
                raise BrokenPipeError("closed")

            def flush(self):
                raise BrokenPipeError("closed")

        self.run_in_process(Broken())
        self.assertIsNone(self.consumed_by())
        good = io.StringIO()
        self.run_in_process(good)
        self.assertIn("hook next step", good.getvalue())
        self.assertEqual(self.consumed_by(), "claude-code:sid1")


class CommitConsumedRevisionPin(Base):
    def consume(self, key, **kw):
        return resume.consume(self.ctx(), key, str(self.project), "startup", **kw)[1]

    def test_a_newer_checkpoint_is_not_marked_by_an_older_print(self):
        self.cp(nxt="first next", session=None)
        out = self.consume("claude-code:S", defer=True)
        rev1 = out["checkpoint_revision"]
        self.assertTrue(out["mark_pending"])
        self.cp(nxt="second next", session=None)
        self.assertFalse(resume.commit_consumed(self.ctx(), "claude-code:S", rev1))
        self.assertIsNone(self.doc()["checkpoints"][-1]["resume"]["consumed_by"])
        later = self.consume("claude-code:T")
        self.assertTrue(later["delivered"])
        self.assertEqual(later["next"], "second next")

    def test_without_an_intervening_checkpoint_it_marks(self):
        self.cp(nxt="only next", session=None)
        out = self.consume("claude-code:S", defer=True)
        self.assertTrue(resume.commit_consumed(self.ctx(), "claude-code:S", out["checkpoint_revision"]))
        self.assertEqual(self.doc()["checkpoints"][-1]["resume"]["consumed_by"], "claude-code:S")


    def test_a_non_checkpoint_transaction_in_between_still_marks(self):
        self.cp(nxt="only next", session=None)
        out = self.consume("claude-code:S", defer=True)
        self.usage(700)  # a ledger transaction that is not a checkpoint: doc.revision moves, the checkpoint's does not
        self.edit(lambda d: None)
        self.assertGreater(self.doc()["revision"], out["checkpoint_revision"])
        self.assertTrue(resume.commit_consumed(self.ctx(), "claude-code:S", out["checkpoint_revision"]))
        self.assertEqual(self.doc()["checkpoints"][-1]["resume"]["consumed_by"], "claude-code:S")


class R9F3(Base):
    def test_a_bad_state_or_next_is_refused_before_any_evidence_is_hashed(self):
        with mock.patch.object(receipts, "hash_evidence", side_effect=AssertionError("hashed")):
            for extra in (["--state", " ", "--next", "n"], ["--state", "s", "--next", " "],
                          ["--state", "s" * 2001, "--next", "n"], ["--state", "s", "--next", "n" * 1001]):
                code, out = self.inproc("checkpoint", *extra, "--evidence", "ev.txt")
                self.assertEqual(code, 2, out)
                self.assertNotIn("hashed", json.dumps(out))

    def test_allow_unknown_liveness_without_force_bank_is_refused(self):
        code, out = self.cli("reconcile", extra=["--allow-unknown-liveness"])
        self.assertEqual(code, 2, out)
        self.assertIn("--force-bank", out["error"])


class ForceBankAudit(test_fix8.LiveRefused):
    def test_an_overridden_unknown_liveness_is_recorded_in_the_banked_note(self):
        job_id, pid, patch = self.refuse_live()
        with patch, mock.patch.object(reconcile, "liveness", return_value=("unknown", "injected")):
            code, out = self.inproc("reconcile", "--force-bank", job_id, "--reason", "x", "--allow-unknown-liveness")
        self.assertEqual(code, 0, out)
        self.assertIn("(unknown liveness overridden)", self.job(job_id)["result"]["note"])

    def test_a_gone_dispatcher_note_has_no_override_mark(self):
        job_id, pid, patch = self.refuse_live()
        with patch, mock.patch.object(reconcile, "liveness", return_value=("gone", "injected")):
            code, out = self.inproc("reconcile", "--force-bank", job_id, "--reason", "x")
        self.assertEqual(code, 0, out)
        self.assertNotIn("overridden", self.job(job_id)["result"]["note"])


class NextBudget(Base):
    def test_checkpoint_refuses_a_next_that_can_never_fit(self):
        self.cp(nxt="\U0001d11e" * 1000, expect=2)  # 4000 bytes of four-byte characters at the 4096 default
        self.assertEqual(self.doc()["checkpoints"], [])

    def test_next_fits_never_approves_a_shortened_line(self):
        nxt = ("now): " * 80).strip()
        doc = {"jobs": {}, "drain": None}
        cp = {"next": nxt, "state": "s", "ready_at_checkpoint": {"ready": True, "reasons": []}}
        full = resume.clean(f"Next ({resume.LABEL['again']}): {nxt}", 10 ** 6)
        for size in range(512, 1400):
            cfg = {"resume_max_bytes": size}
            if resume.next_fits(doc, cp, cfg):
                self.assertIn(full, resume.build_injection(doc, cp, "again", size).split("\n"), size)


class UsageStale(test_hooks.HookCase):
    def stop(self, transcript, **over):
        payload = {"hook_event_name": "Stop", "session_id": "sid1", "cwd": str(self.project),
                   "transcript_path": transcript, **over}
        return self.hook(USAGE, payload)

    def test_a_hook_failure_records_unknown_not_the_stale_count(self):
        self.stop(self.transcript(assistant()))
        self.assertEqual(self.recorded()["usage_tokens"], 100)
        self.stop(self.transcript(assistant(model="m" * 5000)))  # the model string is refused by the ledger
        self.assertIsNone(self.recorded()["usage_tokens"])

    def test_synthetic_zero_usage_is_unknown(self):
        self.stop(self.transcript(assistant(input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                            output_tokens=0, model="<synthetic>")))
        self.assertIsNone(self.recorded()["usage_tokens"])


class UsageAge(Base):
    def test_ready_reports_the_usage_age(self):
        self.cp()
        out = self.ready(expect=True)
        self.assertIsInstance(out["usage_age_seconds"], int)


class EvidenceRefs(Base):
    def test_a_missing_path_shaped_ref_is_refused(self):
        for ref in ("missing.txt", "docs/none.md", "../outside.txt"):
            self.cp(evidence=("ev.txt", ref), expect=2)
        self.assertEqual(self.doc()["checkpoints"], [])
        self.cp(evidence=("ev.txt", "commit abc123", "v0.4.1"))  # plain text stays literal

    def test_a_directory_ref_is_refused(self):
        (self.project / "sub").mkdir()
        self.cp(evidence=("ev.txt", "sub/"), expect=2)

    def test_hashing_runs_outside_the_ledger_lock(self):
        real, held, seen = receipts.hash_ref, [], []

        def spy(root, ref):
            seen.append(ref)
            fd = L.os.open(self.state / "ledger.lock", L.os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(ref)
            finally:
                L.os.close(fd)
            return real(root, ref)

        with mock.patch.object(receipts, "hash_ref", spy):
            code, out = self.inproc("checkpoint", "--state", "s", "--next", "n", "--evidence", "ev.txt")
        self.assertEqual(code, 0, out)
        self.assertTrue(seen)
        self.assertEqual(held, [])


class NullBudgetPool(Base):
    def sleeper(self):
        self.fake("worker", "time.sleep(60)")

    def test_an_unbudgeted_start_is_refused_when_a_pool_is_set(self):
        self.sleeper()
        self.setup_config(child_budget_pool_tokens=1000)
        code, out = self.start()
        self.assertEqual(code, 2, out)
        self.assertIn("budget", out["error"])


if __name__ == "__main__":
    unittest.main()
