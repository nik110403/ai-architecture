"""A7..A11, A8b, A12 (reweight half) for model-evaluation: guarded reweighting with dry-run, apply and rollback."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meval_helpers import (A, B, BUN_CHECK, NEW, OFF, P, ROOT, Isolated, expected_pick, hook_pick, pool_doc,  # noqa: E402
                           scores, sha_bytes)
import reweight  # noqa: E402


def plan_sha(plan):
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Waterfill(unittest.TestCase):  # A8
    def test_two_pass_fixture(self):
        got = reweight.waterfill([0.7, 0.2, 0.05, 0.05], 0.1, 0.4)
        for g, w in zip(got, [0.4, 0.4, 0.1, 0.1]):
            self.assertAlmostEqual(g, w, delta=1e-9)

    def test_bounds_and_sum_on_many_inputs(self):
        import random
        rng = random.Random(7)
        for _ in range(300):
            n = rng.randint(2, 8)
            floor = rng.uniform(0, 0.9 / n)
            cap = rng.uniform(1 / n + 0.01, 1)
            raw = [rng.random() ** 3 for _ in range(n)]
            got = reweight.waterfill(raw, floor, cap)
            self.assertAlmostEqual(sum(got), 1, delta=1e-9)
            self.assertTrue(all(floor - 1e-9 <= g <= cap + 1e-9 for g in got), (raw, floor, cap, got))

    def test_floor_times_n_over_one_is_equal_split(self):
        self.assertEqual(reweight.waterfill([5, 1, 1, 1], 0.3, 0.9), [0.25] * 4)

    def test_infeasible_is_refused(self):
        with self.assertRaises(ValueError):
            reweight.waterfill([1, 1, 1], 0.1, 0.3)  # cap*n < 1
        with self.assertRaises(ValueError):
            reweight.waterfill([1, 1], 0.6, 0.5)  # floor > cap


class DryRun(Isolated):  # A8 (plan), A9, AM3
    def test_plan_weights_shares_and_holds(self):
        pool, ev = self.pool(), self.evidence()
        _, plan = self.dry_plan(pool, ev, "--floor", "0.05", "--cap", "0.6")
        self.assertEqual(plan["role"], "builder")
        self.assertEqual(plan["after"], {A: 24, B: 16, P: 20, OFF: 5, NEW: 5})
        self.assertEqual(plan["before"], {A: 30, B: 10, P: 20, OFF: 5, NEW: 5})
        self.assertEqual(set(plan["held"]), {P, OFF, NEW})
        self.assertAlmostEqual(plan["shares"][A], 0.6, delta=1e-9)
        self.assertAlmostEqual(plan["shares"][B], 0.4, delta=1e-9)
        m = plan["before"][A] + plan["before"][B]
        for arm in (A, B):
            self.assertLessEqual(abs(plan["after"][arm] / m - plan["shares"][arm]), 1 / m)
        self.assertEqual(plan["params"], {"min_samples": 5, "floor": 0.05, "cap": 0.6})
        self.assertEqual(plan["pool_sha256"], sha_bytes(pool.read_bytes()))
        self.assertEqual(plan["evidence_sha256"], sha_bytes(ev.read_bytes()))
        self.assertEqual(plan["plan_sha256"], plan_sha(plan))

    def test_under_min_samples_keeps_weights(self):
        pool, ev = self.pool(), self.evidence()
        _, plan = self.dry_plan(pool, ev, "--min-samples", "7")
        self.assertEqual(plan["after"], plan["before"])
        self.assertTrue(any("min_samples" in r for r in plan["reasons"]))

    def test_every_share_within_floor_cap_after_renormalisation(self):
        doc = pool_doc()
        doc["roles"]["builder"] = [{"model": f"provider/m{i}", "weight": 25} for i in range(4)]
        ev = self.evidence(scores({"provider/m0": [.9] * 6, "provider/m1": [.25] * 6, "provider/m2": [.03] * 6,
                                   "provider/m3": [.03] * 6}))
        _, plan = self.dry_plan(self.pool(doc), ev, "--floor", "0.1", "--cap", "0.4")
        self.assertEqual(sorted(round(s, 9) for s in plan["shares"].values()), [0.1, 0.1, 0.4, 0.4])
        self.assertTrue(all(w >= 1 for w in plan["after"].values()))

    def test_floor_times_n_over_one_gives_equal_split(self):
        doc = pool_doc()
        doc["roles"]["builder"] = [{"model": f"provider/m{i}", "weight": 25} for i in range(4)]
        ev = self.evidence(scores({f"provider/m{i}": [.9 - i * .2] * 6 for i in range(4)}))
        _, plan = self.dry_plan(self.pool(doc), ev, "--floor", "0.3", "--cap", "0.9")
        self.assertEqual(plan["after"], {f"provider/m{i}": 25 for i in range(4)})

    def test_infeasible_parameters_exit_2(self):
        pool, ev = self.pool(), self.evidence()
        self.assertEqual(self.reweight(pool, ev, "--dry-run", "--floor", "0.6", "--cap", "0.5")[0], 2)
        self.assertEqual(self.reweight(pool, ev, "--dry-run", "--cap", "0.3")[0], 2)  # cap*2 < 1

    def test_dry_run_leaves_the_pool_untouched(self):  # A9
        pool, ev = self.pool(), self.evidence()
        before = (pool.read_bytes(), pool.stat().st_mtime_ns, stat.S_IMODE(pool.stat().st_mode))
        self.dry_plan(pool, ev)
        self.assertEqual((pool.read_bytes(), pool.stat().st_mtime_ns, stat.S_IMODE(pool.stat().st_mode)), before)

    def test_pool_and_out_are_required_and_pool_is_safe(self):  # AM8
        pool, ev = self.pool(), self.evidence()
        self.assertEqual(self.cli("reweight", "--evidence", ev, "--out", self.out, "--dry-run")[0], 2)
        self.assertEqual(self.cli("reweight", "--pool", pool, "--evidence", ev, "--dry-run")[0], 2)
        link = self.t / "link.json"
        os.symlink(pool, link)
        before = pool.read_bytes()
        self.assertEqual(self.reweight(link, ev, "--dry-run")[0], 2)
        big = self.t / "big.json"
        big.write_text(" " * 1_000_001)
        self.assertEqual(self.reweight(big, ev, "--dry-run")[0], 2)
        self.assertEqual(pool.read_bytes(), before)


class OutcomeNeverMoves(Isolated):  # A8b, AM2
    def test_outcome_evidence_moves_nothing(self):
        outcomes = [{"arm": A, "task_id": f"t{i}", "completed": True, "verdict": "pass"} for i in range(100)] + \
                   [{"arm": B, "task_id": f"t{i}", "completed": False, "verdict": "fail"} for i in range(100)]
        ev = self.evidence({"version": 1, "role": "builder", "evidence_class": "outcome", "outcomes": outcomes})
        _, plan = self.dry_plan(self.pool(), ev)
        self.assertEqual(plan["after"], plan["before"])
        self.assertTrue(any("outcome" in r for r in plan["reasons"]))

    def test_declared_non_matched_class_vetoes_valid_looking_scores(self):  # F2
        import scoreboard
        doc = scores({A: [.9] * 6, B: [.3] * 6})
        doc["evidence_class"] = "outcome"
        ev = self.evidence(doc)
        _, plan = self.dry_plan(self.pool(), ev)
        self.assertEqual(plan["after"], plan["before"])
        self.assertTrue(any("outcome" in r for r in plan["reasons"]))
        self.assertIsNone(scoreboard.build([], doc)["ranking"])
        ok = scores({A: [.9] * 6, B: [.3] * 6})
        self.assertIsNotNone(scoreboard.build([], ok)["ranking"])

    def test_matched_claim_with_unmatched_rows_moves_nothing(self):
        doc = scores({A: [.9] * 6, B: [.3] * 5})
        doc["evidence_class"] = "matched"
        doc["status"] = "matched"
        _, plan = self.dry_plan(self.pool(), self.evidence(doc))
        self.assertEqual(plan["after"], plan["before"])
        self.assertTrue(any("insufficient" in r for r in plan["reasons"]))


class ApplyRollback(Isolated):  # A7, A10, AM4
    def applied(self):
        pool, ev = self.pool(), self.evidence()
        plan_file, plan = self.dry_plan(pool, ev)
        return pool, ev, plan_file, plan

    def apply(self, pool, ev, plan_file):
        return self.reweight(pool, ev, "--apply", plan_file)

    def test_apply_changes_only_movable_weights(self):
        pool, ev, plan_file, plan = self.applied()
        original = json.loads(pool.read_text())
        mode = stat.S_IMODE(pool.stat().st_mode)
        code, out = self.apply(pool, ev, plan_file)
        self.assertEqual(code, 0, out)
        new = json.loads(pool.read_text())
        expect = json.loads(json.dumps(original))
        for e in expect["roles"]["builder"]:
            e["weight"] = plan["after"][e["model"]]
        self.assertEqual(new, expect)  # deep equality: order, independent_roles, pinned preserved
        self.assertEqual(new["roles"]["builder"][2]["weight"], 20)  # pinned arm keeps its weight
        self.assertEqual(new["roles"]["builder"][3]["weight"], 5)  # disabled arm
        self.assertEqual(stat.S_IMODE(pool.stat().st_mode), mode)
        backups = list((self.out / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_apply_then_rollback_restores_bytes_and_mode(self):
        pool, ev, plan_file, plan = self.applied()
        before, mode = pool.read_bytes(), stat.S_IMODE(pool.stat().st_mode)
        self.assertEqual(self.apply(pool, ev, plan_file)[0], 0)
        self.assertNotEqual(pool.read_bytes(), before)
        code, out = self.cli("reweight", "--pool", pool, "--out", self.out, "--rollback", plan["plan_sha256"][:16])
        self.assertEqual(code, 0, out)
        self.assertEqual((pool.read_bytes(), stat.S_IMODE(pool.stat().st_mode)), (before, mode))

    def test_stale_pool_second_apply_and_late_rollback_exit_4(self):
        pool, ev, plan_file, plan = self.applied()
        original = pool.read_bytes()
        pool.write_bytes(original + b" ")  # edited after the dry-run
        edited = pool.read_bytes()
        code, out = self.apply(pool, ev, plan_file)
        self.assertEqual(code, 4)
        self.assertIn("pool changed", out["error"])  # the explicit sha check, not only the recompute behind it
        self.assertEqual(pool.read_bytes(), edited)
        pool.write_bytes(original)
        self.assertEqual(self.apply(pool, ev, plan_file)[0], 0)
        self.assertEqual(self.apply(pool, ev, plan_file)[0], 4)  # second apply of the same plan
        applied = pool.read_bytes()
        pool.write_text(pool.read_text() + "\n")
        later = pool.read_bytes()
        self.assertNotEqual(applied, later)
        code, _ = self.cli("reweight", "--pool", pool, "--out", self.out, "--rollback", plan["plan_sha256"][:16])
        self.assertEqual(code, 4)
        self.assertEqual(pool.read_bytes(), later)

    def test_tampered_plan_with_recomputed_hash_exits_4(self):
        pool, ev, plan_file, plan = self.applied()
        plan["after"][A] = 39
        plan["after"][B] = 1
        plan["plan_sha256"] = plan_sha(plan)
        plan_file.write_text(json.dumps(plan))
        before = pool.read_bytes()
        self.assertEqual(self.apply(pool, ev, plan_file)[0], 4)
        self.assertEqual(pool.read_bytes(), before)

    def test_apply_with_changed_evidence_exits_4(self):
        pool, ev, plan_file, plan = self.applied()
        ev.write_text(json.dumps(scores({A: [.1] * 6, B: [.9] * 6})))
        self.assertEqual(self.apply(pool, ev, plan_file)[0], 4)

    def test_pinned_arm_survives_in_both_hooks(self):  # A7 hooks
        pool, ev, plan_file, _ = self.applied()
        self.assertEqual(self.apply(pool, ev, plan_file)[0], 0)
        for seed in range(5):
            self.assertEqual(hook_pick(pool, "pin_arm: provider/model-b\n\ndo it", seed), B)
            self.assertEqual(hook_pick(pool, "pin_arm: provider/model-p\n\ndo it", seed), P)


class HookConsumers(Isolated):  # A11
    def test_reweighted_pool_is_valid_for_both_hooks_and_draws_follow_the_algorithm(self):
        pool, ev = self.pool(), self.evidence()
        plan_file, plan = self.dry_plan(pool, ev, "--floor", "0.05", "--cap", "0.7")
        self.assertEqual(self.reweight(pool, ev, "--apply", plan_file)[0], 0)
        doc = json.loads(pool.read_text())
        arms = [(e["model"], e["weight"]) for e in doc["roles"]["builder"] if e.get("enabled", True) and e["weight"] > 0]
        for seed in range(30):
            self.assertEqual(hook_pick(pool, "do it", seed), expected_pick(arms, seed))
        self.assertIsNotNone(hook_pick(pool, "do it", 1, role="reviewer"))
        cfg = self.write_json("oc.json", {"agent": {"builder": {"model": A}, "builder--provider-model-b": {"model": B}}})
        env = dict(os.environ, ROTATION_POOL=str(pool), ROTATION_OC_CONFIG=str(cfg), ROTATION_TEST="1", PATH=os.environ["PATH"])
        res = subprocess.run(["bun", str(BUN_CHECK), str(pool)], capture_output=True, text=True, env=env, cwd=self.t / "work", timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        info = json.loads(res.stdout)
        self.assertTrue(all(info["arms"][r] is not None for r in doc["roles"]))
        total = sum(w for _, w in arms)
        self.assertEqual(len(info["picks"]["builder"]), total)
        self.assertEqual([info["picks"]["builder"][n] for n in range(total)],
                         [m for m, w in arms for _ in range(w)])
        self.assertEqual(info["pinned"], "builder--provider-model-b")


class QuotaNeverCuts(Isolated):  # A12 (reweight half), AM10
    def test_no_quota_cut_in_any_state(self):
        import time
        snap = self.write_json("snap.json", {"provider": "provider", "fetched_at": time.time(),
                                             "windows": [{"name": "5h", "used_percent": 99, "resets_at": None}]})
        providers = {"provider": {"adapter": "snapshot-file", "path": str(snap), "gate": True, "threshold_percent": 90,
                                  "max_age_seconds": 900},
                     "uns": {"adapter": "claude-statusline", "path": str(snap), "gate": True, "threshold_percent": 90}}
        cfg = self.write_json("quota.json", {"version": 1, "providers": providers})
        pool, ev = self.pool(), self.evidence()
        _, plain = self.dry_plan(pool, ev)
        code, out = self.reweight(pool, ev, "--dry-run", "--quota-config", cfg)
        self.assertEqual(code, 0, out)
        quota_plan = json.loads((self.out / out["plan_file"]).read_text())
        self.assertEqual(quota_plan, plain)  # nothing from quota reaches the plan
        self.assertEqual(out["quota"]["provider"], "available")


if __name__ == "__main__":
    unittest.main()
