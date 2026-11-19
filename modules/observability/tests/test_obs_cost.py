"""A1, A2, A3 (spend part), A5 (usage_class reuse) for the observability module."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from obs_helpers import PRICES, ROOT, Isolated  # noqa: E402
from modules.observability import cost, reports, usage  # noqa: E402

FIXTURE = ROOT / "skills" / "evaluation" / "fixtures" / "mixed.jsonl"


def env_row(env, model="provider/model-a", role="builder"):
    return usage.row_from_envelope(env, harness="h", role=role, model=model, source="dispatch")


class MissingUsage(Isolated):  # A1
    def test_partial_and_empty_envelopes_have_no_cost(self):
        partial = env_row({"input": 1000, "output": 2000, "cache_read": 4000})  # cache_write absent
        empty = env_row({})
        self.assertEqual(partial["usage_class"], "usage_partial")
        self.assertEqual(empty["usage_class"], "usage_missing")
        self.assertEqual(cost.cost_of(partial, PRICES), {"cost_usd": None, "reason": "usage_partial"})
        self.assertEqual(cost.cost_of(empty, PRICES)["cost_usd"], None)
        self.assertEqual(cost.cost_of(empty, PRICES)["reason"], "usage_missing")

    def test_spend_report_is_incomplete_with_both_rows_unknown(self):
        rows = [env_row({"input": 1, "output": 2, "cache_read": 3}), env_row({})]
        rep = reports.spend_report(rows, PRICES)
        self.assertIs(rep["complete"], False)
        self.assertIsNone(rep["known_total"])
        self.assertEqual([u["reason"] for u in rep["unknown"]], ["usage_partial", "usage_missing"])

    def test_reported_cost_zero_with_missing_tokens_is_not_a_cost(self):
        row = env_row({"cost_usd": 0})
        self.assertEqual(row["reported_cost_usd"], 0)
        self.assertEqual(row["usage_class"], "usage_missing")
        self.assertIsNone(cost.cost_of(row, PRICES)["cost_usd"])
        rep = reports.spend_report([row], PRICES)
        self.assertIsNone(rep["known_total"])
        self.assertEqual(rep["unknown"][0]["reported_cost_usd"], 0)


class MissingPrice(Isolated):  # A2 + AM5
    FULL = {"input": 1000, "output": 2000, "cache_read": 4000, "cache_write": 500}

    def test_absent_model_is_price_missing(self):
        got = cost.cost_of(env_row(self.FULL, model="provider/other"), PRICES)
        self.assertEqual(got, {"cost_usd": None, "reason": "price_missing"})

    def test_cache_tokens_without_cache_price(self):
        got = cost.cost_of(env_row(self.FULL, model="provider/model-nocache"), PRICES)
        self.assertEqual(got, {"cost_usd": None, "reason": "cache_price_missing"})

    def test_all_zero_known_tokens_cost_exactly_zero(self):
        zero = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.assertEqual(cost.cost_of(env_row(zero), PRICES)["cost_usd"], 0)
        self.assertEqual(cost.cost_of(env_row(zero, model="provider/model-nocache"), PRICES)["cost_usd"], 0)

    def test_positive_cost_is_the_hand_computed_figure(self):
        row = env_row(self.FULL)
        self.assertEqual(row["usage_class"], "usage_known")
        self.assertIsNone(row["tokens"]["reasoning"])  # reasoning is neither required nor priced
        # (1000*3 + 2000*15 + 4000*0.3 + 500*3.75) / 1e6
        self.assertEqual(cost.cost_of(row, PRICES), {"cost_usd": 0.036075, "reason": "ok"})

    def test_reasoning_tokens_are_not_silently_dropped(self):  # F1
        env = {"input": 1000, "output": 1, "cache_read": 0, "cache_write": 0, "reasoning": 99}
        declared = {"provider/model-a": {**PRICES["provider/model-a"], "reasoning": "in_output"}}
        row = env_row(env)
        self.assertEqual(cost.cost_of(row, PRICES), {"cost_usd": None, "reason": "reasoning_unpriced"})
        rep = reports.spend_report([row], PRICES)
        self.assertIs(rep["complete"], False)
        self.assertIsNone(rep["known_total"])
        priced = cost.cost_of(row, declared)
        self.assertEqual(priced["reason"], "ok")
        self.assertIs(reports.spend_report([row], declared)["complete"], True)
        for zero in (0, None):
            self.assertEqual(cost.cost_of(env_row({**env, "reasoning": zero}), PRICES)["reason"], "ok")
        receipt = {"harness": "h", "role": "r", "model": "provider/model-a", "source": "s", "in_tok": 1000, "out_tok": 1,
                   "cache_read_tok": 0, "cache_write_tok": 0, "reasoning_tok": 99, "cost_usd": None, "tok_source": "x"}
        rrow = usage.row_from_receipt(receipt)
        self.assertEqual(cost.cost_of(rrow, PRICES)["reason"], "reasoning_unpriced")

    def test_invalid_price_entries_are_missing(self):
        bad = {"provider/model-a": {"input": True, "output": 15}}
        self.assertEqual(cost.cost_of(env_row(self.FULL), bad)["reason"], "price_missing")


class NoRankingFromUnknowns(Isolated):  # A3 (spend)
    def test_spend_report_is_key_ordered_and_unknown_is_listed(self):
        rows = [
            env_row({"input": 1000000, "output": 0, "cache_read": 0, "cache_write": 0}, model="provider/model-a"),  # 3.0
            env_row({}, model="provider/model-b"),  # unknown
            env_row({"input": 1, "output": 0, "cache_read": 0, "cache_write": 0}, model="provider/model-c"),
        ]
        prices = dict(PRICES, **{"provider/model-c": PRICES["provider/model-a"]})
        rep = reports.spend_report(rows, prices)
        self.assertEqual([g["model"] for g in rep["groups"]],
                         ["provider/model-a", "provider/model-b", "provider/model-c"])
        self.assertEqual([g["model"] for g in rep["groups"] if g["unknown_rows"]], ["provider/model-b"])
        self.assertEqual([u["model"] for u in rep["unknown"]], ["provider/model-b"])
        self.assertIsNone(rep["groups"][1]["known_cost_usd"])
        self.assertAlmostEqual(rep["known_total"], 3.000003)
        self.assertIs(rep["complete"], False)


class ReceiptReuse(Isolated):  # A5 (observability half)
    def test_receipt_rows_keep_the_offline_usage_class(self):
        import receipt_report
        loaded, _ = receipt_report.load([str(FIXTURE)])
        rows = usage.load_receipts([str(FIXTURE)])
        summary = receipt_report.summarize(loaded, [str(FIXTURE)], 0, None)
        want = {k: sum(g[k] for g in summary["groups"]) for k in
                ("usage_known", "usage_partial", "usage_missing", "usage_excluded")}
        got = {k: sum(1 for r in rows if r["usage_class"] == k) for k in want}
        self.assertEqual(got, want)
        self.assertGreater(sum(want.values()), 3)
        excluded = [r for r in rows if r["usage_class"] == "usage_excluded"]
        self.assertEqual(cost.cost_of(excluded[0], PRICES)["reason"], "usage_excluded")
        self.assertIsNone(cost.cost_of(excluded[0], PRICES)["cost_usd"])

    def test_usage_class_is_imported_not_redefined(self):
        text = (ROOT / "modules" / "observability" / "usage.py").read_text()
        self.assertNotIn("def usage_class", text)
        import receipt_report
        self.assertIs(usage.receipt_usage_class, receipt_report.usage_class)


if __name__ == "__main__":
    unittest.main()
