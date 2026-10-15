import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from route import route

READY = {
    "capabilities": ["code.builder", "code.verifier"],
    "models": ["builder", "verifier"],
}


class RouteTests(unittest.TestCase):
    def test_unknown_ambiguous_and_substring_intents_clarify(self):
        self.assertEqual(route("sing a song", READY)["status"], "REFUSE")
        self.assertEqual(route("research and fix code", READY)["reason"], "ambiguous")
        self.assertEqual(route("build a sculpture", READY)["reason"], "unknown")
        self.assertEqual(
            route("route:coding build a sculpture", READY)["route"], "coding"
        )

    def test_unread_family_refuses(self):
        self.assertEqual(
            route("make a system atlas", READY)["reason"], "no-port"
        )

    def test_explicit_route_ids_are_exact_and_cannot_bypass_unread(self):
        maintenance = {
            "capabilities": ["maintenance.inventory", "maintenance.checker"],
            "models": ["maintainer", "checker"],
        }
        for mode in ("enforced", "manual"):
            for intent, reason in (
                ("route:coding-extra", "unknown"),
                ("route:bogus", "unknown"),
                ("route:coding route:research", "ambiguous"),
                ("route:coding troubleshoot", "no-port"),
            ):
                result = route(intent, READY, mode=mode)
                self.assertEqual(
                    (result["status"], result["reason"], result["route"]),
                    ("REFUSE", reason, None),
                )
            for intent in ("retro", "run a skill-scout", "instinct-promote this"):
                result = route(intent, maintenance, mode=mode)
                self.assertEqual(
                    (result["status"], result["reason"]),
                    ("REFUSE", "no-port"),
                )

    def test_malformed_availability_is_refused_not_coerced(self):
        cases = (
            None,
            [],
            {"capabilities": None, "models": None},
            {"capabilities": {"code.builder": False}, "models": {"builder": False}},
            {**READY, "extra": []},
            {**READY, "review_budget": False},
            {**READY, "review_budget": -1},
        )
        for available in cases:
            with self.subTest(available=available):
                self.assertEqual(route("fix code test", available)["status"], "REFUSE")
        self.assertEqual(
            route("fix code test", READY, pending_jobs=1)["status"], "REFUSE"
        )
        self.assertEqual(
            route("fix code test", READY, unknown_arrivals=1)["status"], "REFUSE"
        )

    def test_missing_model_and_capability_preserve_required_roles(self):
        report = route(
            "fix code test", {"capabilities": ["code.builder"], "models": ["builder"]}
        )
        self.assertEqual(report["status"], "REFUSE")
        self.assertIn("code.verifier", report["missing_adapters"])
        self.assertIn("verifier", report["roles"])
        self.assertIn("verifier", report["missing_model_roles"])

    def test_external_is_conditional_not_authority(self):
        available = {
            "capabilities": ["external.survey", "external.approval", "external.act"],
            "models": ["operator", "checker"],
        }
        self.assertEqual(route("submit browser form", available)["status"], "REFUSE")
        available["approval"] = {
            "state_hash": "abc",
            "approved_action": "submit fixture",
            "authorized": True,
        }
        report = route("submit browser form", available)
        self.assertEqual(report["status"], "CONDITIONAL")
        self.assertFalse(report["native_enforced"])
        self.assertEqual(
            report["ordered_checks"][:3],
            [
                "survey observed state",
                "write proposal",
                "independently review proposal against state",
            ],
        )

    def test_manual_and_lifecycle_states_never_claim_ready(self):
        self.assertEqual(
            route("fix code test", {"capabilities": [], "models": []}, mode="manual")[
                "status"
            ],
            "MANUAL_INSTRUCTIONS",
        )
        report = route(
            "fix code test", READY, pending_jobs=("review",), unknown_arrivals=True
        )
        self.assertEqual(report["status"], "REFUSE")
        self.assertTrue(report["unknown_arrivals"])

    def test_review_budget_has_real_exhausted_and_invalid_dispositions(self):
        exhausted = route("fix code test", {**READY, "review_budget": 0})
        self.assertEqual(exhausted["status"], "REFUSE")
        self.assertEqual(exhausted["review"]["disposition"], "REVIEW_BUDGET_EXHAUSTED")
        self.assertEqual(
            route("fix code test", {**READY, "review_budget": 3})["status"], "REFUSE"
        )

    def test_selective_routing_bundle_runs_from_a_fresh_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw)
            shutil.copytree(ROOT / "skills/routing", target / ".claude/skills/routing")
            (target / "scripts").mkdir()
            (target / "templates").mkdir()
            shutil.copy2(ROOT / "scripts/route.py", target / "scripts/route.py")
            shutil.copy2(
                ROOT / "templates/route-report.md", target / "templates/route-report.md"
            )
            self.assertTrue(
                (target / ".claude/skills/routing/DEPENDENCIES.md").is_file()
            )
            result = subprocess.run(
                [sys.executable, str(target / "scripts/route.py"), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--capabilities", result.stdout)


if __name__ == "__main__":
    unittest.main()
