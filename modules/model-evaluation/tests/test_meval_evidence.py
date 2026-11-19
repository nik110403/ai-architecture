"""A3 (scoreboard), A4 (matched evidence), A5 (reuse) for model-evaluation."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meval_helpers import A, B, MOD, ROOT, Isolated, scores  # noqa: E402
import evidence  # noqa: E402

FIXTURE = ROOT / "skills" / "evaluation" / "fixtures" / "mixed.jsonl"


def receipt(i, model, verify, harness="h", role="implementer", charge=None):
    row = {"schema_version": 1, "t": 100 + i, "task_id": f"task{i}", "source": "fx", "harness": harness, "role": role,
           "model": model, "verify": verify, "tok_source": "agent", "in_tok": 1, "out_tok": 1, "reasoning_tok": 0,
           "cache_read_tok": 0, "cache_write_tok": 0, "cost_usd": 0.1, "dur_ms": 1}
    if charge:
        row["charge_id"] = charge
    return json.dumps(row)


class Scoreboard(Isolated):  # A3
    def test_counts_only_no_ranking_and_key_order(self):
        rows = [receipt(i, "provider/model-a", "fail") for i in range(3)] + \
               [receipt(10 + i, "provider/model-b", "pass") for i in range(3)] + \
               [receipt(20, "provider/model-c", "none")]
        rows.append(receipt(30, "provider/model-a", "pass", role="verifier", charge="chg-" + "0" * 8))
        path = self.t / "r.jsonl"
        path.write_text("\n".join(rows) + "\n")
        code, out = self.cli("scoreboard", "--receipts", path)
        self.assertEqual(code, 0)
        self.assertIsNone(out["ranking"])
        self.assertIn("completion", out["ranking_reason"])
        self.assertEqual(out["evidence_class"], "outcome")
        models = [a["model"] for a in out["arms"] if a["role"] == "implementer"]
        self.assertEqual(models, sorted(models))  # key order, not pass-count order (a: 0 pass, b: 3 pass)
        allowed = {"harness", "role", "model", "runs", "pass", "fail", "none", "usage_known", "usage_partial",
                   "usage_missing", "usage_excluded"}
        for arm in out["arms"]:
            self.assertLessEqual(set(arm), allowed)  # counts only: no rate, no score
        self.assertEqual(out["checker_verdicts"], {"pass": 1, "fail": 0})

    def test_scores_without_matching_do_not_rank(self):
        path = self.t / "r.jsonl"
        path.write_text(receipt(1, "provider/model-a", "pass") + "\n")
        ev = self.evidence(scores({A: [1] * 6, B: [0] * 5}))
        code, out = self.cli("scoreboard", "--receipts", path, "--scores", ev)
        self.assertEqual(code, 0)
        self.assertIsNone(out["ranking"])
        self.assertEqual(out["match"]["status"], "insufficient")


class Matched(unittest.TestCase):  # A4
    def check(self, doc, **kw):
        return evidence.match_check(doc["scores"], **kw)

    def test_missing_score_for_one_arm_is_insufficient(self):
        doc = scores({A: [.9] * 6, B: [.3] * 5})
        res = self.check(doc)
        self.assertEqual(res["status"], "insufficient")
        self.assertIsNone(res["ranking"])
        self.assertTrue(any("task set" in r for r in res["reasons"]))

    def test_different_grader_is_insufficient(self):
        doc = scores({A: [.9] * 6, B: [.3] * 6})
        doc["scores"][-1]["grader"] = "grader-2"
        res = self.check(doc)
        self.assertEqual(res["status"], "insufficient")
        self.assertTrue(any("grader" in r for r in res["reasons"]))

    def test_fully_matched_ranks_with_n_pairs_and_label(self):
        res = self.check(scores({A: [.9] * 6, B: [.3] * 6}))
        self.assertEqual(res["status"], "matched")
        self.assertEqual(res["paired_task_ids"], [f"t{i}" for i in range(6)])
        self.assertEqual([r["arm"] for r in res["ranking"]], [A, B])
        self.assertEqual(res["ranking"][0]["n"], 6)
        self.assertAlmostEqual(res["ranking"][0]["mean"], .9)
        self.assertEqual(res["ranking"][0]["wins"], {B: 6})
        self.assertEqual(res["ranking_label"], "descriptive (no significance claim)")

    def test_fewer_than_min_pairs_is_insufficient_even_when_fully_matched(self):
        res = self.check(scores({A: [.9] * 4, B: [.3] * 4}, tasks=4))
        self.assertEqual(res["status"], "insufficient")
        self.assertEqual(self.check(scores({A: [.9] * 4, B: [.3] * 4}, tasks=4), min_pairs=4)["status"], "matched")

    def test_single_arm_duplicates_and_bad_scores_are_insufficient(self):
        self.assertEqual(self.check(scores({A: [.9] * 6}))["status"], "insufficient")
        doc = scores({A: [.9] * 6, B: [.3] * 6})
        doc["scores"].append(dict(doc["scores"][0]))
        self.assertEqual(self.check(doc)["status"], "insufficient")
        doc = scores({A: [.9] * 6, B: [.3] * 6})
        doc["scores"][0]["score"] = None
        self.assertEqual(self.check(doc)["status"], "insufficient")
        doc["scores"][0]["score"] = True
        self.assertEqual(self.check(doc)["status"], "insufficient")


class Reuse(unittest.TestCase):  # A5
    def test_usage_class_and_latest_verdicts_are_imported_not_redefined(self):
        text = (MOD / "evidence.py").read_text()
        self.assertNotRegex(text, r"def usage_class\(")
        self.assertNotRegex(text, r"def latest_verdicts\(")
        import evaluation_report
        import receipt_report
        self.assertIs(evidence.usage_class, receipt_report.usage_class)
        self.assertIs(evidence.latest_verdicts, evaluation_report.latest_verdicts)

    def test_fixture_usage_class_counts_equal_receipt_report(self):
        import receipt_report
        loaded, _ = receipt_report.load([str(FIXTURE)])
        summary = receipt_report.summarize(loaded, [str(FIXTURE)], 0, None)
        want = {k: sum(g[k] for g in summary["groups"]) for k in
                ("usage_known", "usage_partial", "usage_missing", "usage_excluded")}
        got = evidence.usage_class_counts(evidence.load_receipts([str(FIXTURE)]))
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
