"""R9-U04b: READY conjunction (each condition flipped alone), unknown usage, phases, handoff gate, child budgets."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ContCase, L, jobs  # noqa: E402
from test_receipts import CpMixin  # noqa: E402


class Ready(CpMixin, ContCase):
    def setUp(self):
        super().setUp()
        self.write("ev.txt", "evidence v1")
        self.write("TRACKER.md", "tracker v1")
        self.usage(500)

    def checkpointed(self, *extra):
        self.cp("--tracker", "TRACKER.md", *extra)

    def blocked(self, needle):
        out = self.ready(expect=False)
        self.assertIn(needle, self.reasons(out))
        return out

    def test_all_conditions_satisfied_is_ready(self):
        self.ext("e1")
        self.ext_bank("e1")
        self.checkpointed()
        out = self.ready(expect=True)
        self.assertEqual((out["reasons"], out["phase"], out["usage_tokens"]), ([], "UNKNOWN", 500))
        self.assertEqual(out["receipt_sha256"], self.doc()["checkpoints"][-1]["receipt_sha256"])

    def test_no_checkpoint(self):
        self.blocked("no checkpoint")

    def test_pending_job(self):
        self.ext("e1")
        self.checkpointed()
        out = self.blocked("job e1 is not banked")
        self.assertNotIn("changed after", self.reasons(out))  # flipped alone

    def test_unresolved_job_whose_owner_is_gone(self):
        self.ext("e1", pid=self.dead_pid())
        self.checkpointed()
        self.blocked("job e1 is not banked")

    def test_unacknowledged_arrival(self):
        self.ext("e1")
        self.ext_bank("e1")
        self.checkpointed()
        self.edit(lambda d: d["jobs"]["e1"].update(acknowledged_revision=None))
        out = self.blocked("unacknowledged arrival: job e1")
        self.assertNotIn("changed after", self.reasons(out))

    def test_post_bank_after_checkpoint(self):
        self.ext("e1")
        self.ext_bank("e1")
        self.checkpointed()

        def late(doc):
            doc["jobs"]["e1"]["post_bank"].append({"observed_at": L.now(), "outcome": "failed", "sha256": "b" * 64,
                                                   "source": "file"})
            jobs.touch(doc, doc["jobs"]["e1"])
        self.edit(late)
        self.blocked("late result for job e1")

    def test_failed_job_not_accepted_then_accepted(self):
        self.ext("e1")
        self.ext_bank("e1", "failed")
        self.checkpointed()
        out = self.blocked("failed job e1")
        self.assertFalse(self.doc()["checkpoints"][-1]["ready_at_checkpoint"]["ready"])
        self.checkpointed("--accept-failed", "e1")
        self.ready(expect=True)
        self.assertTrue(self.doc()["checkpoints"][-1]["ready_at_checkpoint"]["ready"])
        self.assertEqual(out["phase"], "UNKNOWN")

    def test_job_change_after_checkpoint(self):
        self.ext("e1")
        self.ext_bank("e1")
        self.checkpointed()
        self.edit(lambda d: jobs.touch(d, d["jobs"]["e1"]))
        self.blocked("job e1 changed after the checkpoint")

    def test_new_job_after_checkpoint(self):
        self.checkpointed()
        self.ext("late")
        self.blocked("job late changed after the checkpoint")

    def test_evidence_file_edited_or_removed(self):
        self.checkpointed()
        self.write("ev.txt", "evidence v2")
        self.blocked("evidence ev.txt changed")
        (self.project / "ev.txt").unlink()
        self.blocked("evidence ev.txt changed or is missing")

    def test_tracker_edited(self):
        self.checkpointed()
        self.write("TRACKER.md", "tracker v2")
        self.blocked("tracker TRACKER.md changed")

    def test_receipt_tampered(self):
        self.checkpointed()
        self.edit(lambda d: d["checkpoints"][-1].update(next="something else"))
        self.blocked("does not recompute")

    def test_ledger_invalid(self):
        self.checkpointed()
        (self.state / "ledger.json").write_text("{not json")
        out = self.ready(expect=False)
        self.assertIn("ledger invalid", self.reasons(out))

    def test_usage_unknown_in_every_form(self):
        self.write("ev.txt", "evidence v1")
        self.checkpointed()
        self.ready(expect=True)
        self.usage("unknown")
        self.blocked("usage UNKNOWN")
        out = self.ready("cli:never-recorded", expect=False)
        self.assertIn("usage UNKNOWN", self.reasons(out))
        self.assertEqual((out["phase"], out["usage_tokens"]), ("UNKNOWN", None))

    def test_every_failing_condition_is_listed(self):
        self.ext("e1")  # pending
        self.ext("e2")
        self.ext_bank("e2", "failed")  # failed, not accepted
        self.checkpointed()
        self.write("ev.txt", "evidence v2")
        self.usage("unknown")
        text = self.reasons(self.ready(expect=False))
        for needle in ("job e1 is not banked", "failed job e2", "evidence ev.txt changed", "usage UNKNOWN"):
            self.assertIn(needle, text)

    def test_handoff_is_checkpoint_then_ready(self):
        self.ext("e1")
        self.ext_bank("e1")
        code, out = self.cli("handoff", extra=["--session", "cli:a", "--state", "s", "--next", "n",
                                               "--evidence", "ev.txt"])
        self.assertEqual((code, out["ready"]), (0, True), out)
        self.assertTrue(self.doc()["checkpoints"][-1]["ready_at_checkpoint"]["ready"])
        self.usage("unknown")
        code, out = self.cli("handoff", extra=["--session", "cli:a", "--state", "s", "--next", "n",
                                               "--evidence", "ev.txt"])
        self.assertEqual((code, out["ready"]), (1, False), out)
        self.assertFalse(out["checkpoint"]["ready_at_checkpoint"]["ready"])
        import subprocess
        proc = subprocess.run([sys.executable, "-B", str(L.ROOT / "modules/continuity/continuity.py"), "handoff",
                               "--project", str(self.project), "--state", "s", "--next", "n", "--evidence", "ev.txt"],
                              capture_output=True)
        self.assertEqual(proc.returncode, 2)  # --session is required


class Phases(CpMixin, ContCase):
    def setup_config(self, **cfg):
        (self.state / "config.json").write_text(json.dumps(cfg))

    def status_view(self, session="cli:a"):
        code, out = self.cli("status", extra=["--session", session])
        self.assertEqual(code, 0, out)
        return out

    def test_phase_thresholds_from_recipient_settings(self):
        self.setup_config(handoff_reserve_tokens=20000, prepare_fraction=0.8, context_limits={"m": 100000})
        self.usage(10, "cli:a", "--model", "m")
        self.assertEqual(self.status_view()["phase"], "NORMAL")  # handoff 80000, prepare 64000
        self.usage(64000, "cli:a")
        self.assertEqual(self.status_view()["phase"], "PREPARE")
        self.usage(79999, "cli:a")
        self.assertEqual(self.status_view()["phase"], "PREPARE")
        self.usage(80000, "cli:a")
        view = self.status_view()
        self.assertEqual((view["phase"], view["usage_tokens"], view["limit_tokens"]), ("HANDOFF", 80000, 100000))

    def test_cap_and_explicit_limit(self):
        self.setup_config(handoff_cap_tokens=50000)
        self.usage(50000, "cli:a", "--limit", "1000000")
        self.assertEqual(self.status_view()["phase"], "HANDOFF")

    def test_unknown_phase_without_usage_or_limit(self):
        self.setup_config(context_limits={"m": 100000})
        self.assertEqual(self.status_view("cli:none")["phase"], "UNKNOWN")
        self.usage("unknown", "cli:a", "--model", "m")
        self.assertEqual(self.status_view()["phase"], "UNKNOWN")
        self.usage(5, "cli:b")  # known usage, no limit and no model
        self.assertEqual(self.status_view("cli:b")["phase"], "UNKNOWN")

    def test_handoff_refuses_start_and_unknown_admits_with_a_note(self):
        self.setup_config(handoff_cap_tokens=1000)
        self.usage(1000, "cli:a", "--limit", "50000")
        code, out = self.start("--session", "cli:a")
        self.assertEqual(code, 2, out)
        self.assertIn("bank results and checkpoint before new workers", out["error"])
        self.assertEqual(self.doc()["jobs"], {})
        code, out = self.start("--session", "cli:nobody")  # UNKNOWN admits, with a note
        self.assertEqual(code, 0, out)
        self.assertIn("unknown", out["note"])
        code, out = self.start()  # no --session: no phase gate
        self.assertEqual(code, 0, out)
        self.assertNotIn("note", out)
        self.usage(10, "cli:a")  # back under the line
        self.assertEqual(self.start("--session", "cli:a")[0], 0)

    def test_there_is_no_root_session_count(self):
        self.setup_config(handoff_cap_tokens=1000)
        for n in range(10):
            self.usage(1, f"claude-code:s{n}", "--limit", "500000")
        self.assertEqual(self.start("--session", "claude-code:s9")[0], 0)


class ChildBudgets(CpMixin, ContCase):
    def sleeper(self):
        self.fake("worker", "time.sleep(60)")

    def bank(self, job_id, usage):
        obs = jobs.synth("succeeded", "completed", "t", source="wait")
        obs.update(usage=usage, trust="dispatcher")
        jobs.bank(self.ctx().ledger, job_id, obs)
        return self.job(job_id)["result"]

    def setup_config(self, **cfg):
        (self.state / "config.json").write_text(json.dumps(cfg))

    def test_budget_state_values(self):
        self.sleeper()
        want = {"within": ("100", {"input": 40, "output": 10, "cache_read": 20, "cache_write": 5}, 75),
                "exceeded": ("50", {"input": 40, "output": 20}, 60),
                "unknown": ("50", None, None),
                "unbounded": (None, {"input": 5}, 5)}
        for state, (budget, usage, total) in want.items():
            extra = ["--budget-tokens", budget] if budget else []
            code, out = self.start(*extra)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.job(out["job_id"])["budget"]["tokens"], int(budget) if budget else None)
            result = self.bank(out["job_id"], usage)
            self.assertEqual((result["budget_state"], result["usage_total"]), (state, total), state)

    def test_default_budget_from_config(self):
        self.sleeper()
        self.setup_config(child_budget_tokens=300)
        code, out = self.start()
        self.assertEqual(self.job(out["job_id"])["budget"]["tokens"], 300)
        code, out = self.start("--budget-tokens", "5")
        self.assertEqual(self.job(out["job_id"])["budget"]["tokens"], 5)

    def test_pool_counts_reserved_budgets_and_used_totals(self):
        self.sleeper()
        self.setup_config(child_budget_pool_tokens=1000)
        code, first = self.start("--budget-tokens", "600")
        self.assertEqual(code, 0, first)
        code, out = self.start("--budget-tokens", "600")
        self.assertEqual(code, 2, out)
        self.assertIn("pool", out["error"])
        self.bank(first["job_id"], {"input": 700})  # spent 700 since the last checkpoint, reservation released
        self.assertEqual(self.start("--budget-tokens", "300")[0], 0)
        code, out = self.start("--budget-tokens", "100")  # 700 used + 300 reserved + 100 > 1000
        self.assertEqual(code, 2, out)

    def test_pool_with_an_unknown_usage_arrival_refuses(self):
        self.sleeper()
        self.setup_config(child_budget_pool_tokens=1000)
        code, first = self.start("--budget-tokens", "10")
        self.bank(first["job_id"], None)
        code, out = self.start("--budget-tokens", "10")
        self.assertEqual(code, 2, out)
        self.assertIn("child usage unknown since the last checkpoint", out["error"])

    def test_a_checkpoint_reopens_the_pool(self):
        self.sleeper()
        self.setup_config(child_budget_pool_tokens=1000)
        self.write("ev.txt")
        code, first = self.start("--budget-tokens", "10")
        self.bank(first["job_id"], {"input": 990})
        self.assertEqual(self.start("--budget-tokens", "100")[0], 2)
        self.cp()
        self.assertEqual(self.start("--budget-tokens", "100")[0], 0)


if __name__ == "__main__":
    unittest.main()
