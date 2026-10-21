"""Graph and closed-pack regressions; all packs and hook state are temporary."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "tools/plancheck/plancheck"
HOOKS = ROOT / "plugins/gates/hooks"
spec = importlib.util.spec_from_file_location("upe_trackers", HOOKS / "lib/trackers.py")
trackers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trackers)


class PlancheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def pack(self, items, status="active", name="pack"):
        pack = self.root / name
        pack.mkdir(exist_ok=True)
        (pack / "TRACKER.md").write_text(
            f"# Fixture\n**Status: {status}**\n## Resume here\n**Next:** run tests.\n"
        )
        (pack / "SPEC.md").write_text(items)
        return pack

    def run_check(self, path, *args):
        result = subprocess.run(
            ["bash", str(BIN), "--dir", str(path), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.stderr, "")
        return result

    @staticmethod
    def item(id, needs="none", touches="unique/"):
        return (
            f"### {id} — fixture\n- Acceptance: pass\n- Verify: `true`\n"
            f"- Needs: {needs}\n- Touches: {touches}\n\n"
        )

    def test_cycles_rejected_for_plain_hyphenated_and_backticked_ids(self):
        for ids in [("S1", "S2"), ("UP-1", "UP-2"), ("`UP-1`", "`UP-2`")]:
            with self.subTest(ids=ids):
                a, b = ids
                pack = self.pack(self.item(a, b, "a/") + self.item(b, a, "b/"))
                result = self.run_check(pack)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Needs cycle among", result.stdout)
                self.assertNotIn('no "###', result.stdout)

    def test_self_cycle_and_closed_pack_cycle_rejected(self):
        for status in ("active", "parked", "done"):
            with self.subTest(status=status):
                result = self.run_check(self.pack(self.item("S1", "S1"), status))
                self.assertEqual(result.returncode, 1)
                self.assertIn("Needs cycle among S1", result.stdout)

    def test_transitive_order_across_spec_files(self):
        for prefix, quoted in (("S", False), ("UP-", False), ("UP-", True)):
            with self.subTest(prefix=prefix, quoted=quoted):
                ids = [f"{prefix}{i}" for i in range(1, 5)]
                ref = lambda id: f"`{id}`" if quoted else id
                a, b, c, d = ids
                pack = self.pack(
                    self.item(ref(a), touches="shared/")
                    + self.item(ref(b), ref(a), "b/")
                )
                (pack / "SPEC-more.md").write_text(
                    self.item(ref(c), ref(b), "c/")
                    + self.item(ref(d), ref(c), "shared/file.py")
                )
                result = self.run_check(pack)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("no unordered Touches overlaps", result.stdout)
                self.assertNotIn("— overlapping Touches", result.stdout)

    def test_unordered_overlap_is_blocking_and_report_only_overrides(self):
        for a, b in [("S1", "S2"), ("UP-1", "UP-2"), ("`UP-1`", "`UP-2`")]:
            with self.subTest(a=a):
                pack = self.pack(
                    self.item(a, touches="shared/")
                    + self.item(b, touches="`shared/file.py`")
                )
                result = self.run_check(pack)
                self.assertEqual(result.returncode, 1)
                self.assertIn("overlapping Touches", result.stdout)
                self.assertEqual(self.run_check(pack, "--report-only").returncode, 0)
                self.assertEqual(self.run_check(self.root).returncode, 1)

    def test_parked_skips_overlap_but_not_dangling_references(self):
        pack = self.pack(
            self.item("UP-1", touches="shared/")
            + self.item("UP-2", touches="shared/a"),
            "PaRkEd",
        )
        result = self.run_check(pack)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("— overlapping Touches", result.stdout)
        (pack / "SPEC.md").write_text(
            self.item("UP-1", "`UP-9`") + "- Blocked-by: Q9\n"
        )
        result = self.run_check(pack)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Needs: UP-9", result.stdout)
        self.assertIn("Blocked-by: Q9", result.stdout)

    def test_section_divider_does_not_inherit_item_fields(self):
        pack = self.pack(
            self.item("S1") + "### Notes\n- Needs: S9\n- Touches: unique/\n"
        )
        self.assertEqual(self.run_check(pack).returncode, 0)

    def test_backticked_hyphenated_gate_keeps_adoption_contract(self):
        pack = self.pack(self.item("`UP-1`") + "- Gate: human — external publication\n")
        (pack / "TRACKER.md").write_text(
            "Status: active\n## Resume here\n**Next:** `UP-1` on the owner's go.\n"
        )
        result = self.run_check(pack)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("but no named Gate", result.stdout)
        self.assertNotIn("Gate: human reason not", result.stdout)

    def test_closed_status_discovery_and_resume_injection(self):
        for status in ("done", "abandoned", "promoted", "PaRkEd"):
            with self.subTest(status=status):
                pack = self.pack("", status)
                self.assertEqual(
                    trackers.tracker_status(pack / "TRACKER.md"), status.lower()
                )
                self.assertEqual(trackers.active_trackers(str(pack)), [])
                result = subprocess.run(
                    [sys.executable, str(HOOKS / "resume-inject.py")],
                    input=json.dumps({"cwd": str(pack), "session_id": "fixture"}),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=dict(
                        os.environ,
                        AIARCH_CONFIG=str(self.root / "absent.json"),
                        CLAUDE_HOOK_STATE_DIR=str(self.root / "state"),
                    ),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_active_and_legacy_trackers_still_discovered(self):
        pack = self.pack("")
        self.assertEqual(len(trackers.active_trackers(str(pack))), 1)
        (pack / "TRACKER.md").write_text("# Legacy\n## Resume here\n**Next:** test\n")
        self.assertEqual(len(trackers.active_trackers(str(pack))), 1)

    def test_stop_ignores_parked_and_closing_pack_still_satisfies_gate(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        plans = self.root / "plans"
        plans.mkdir()
        parked = plans / "demo"
        parked.mkdir()
        (parked / "TRACKER.md").write_text(
            "Status: parked\n## Resume here\n**Next:** wait\n"
        )
        fixture = HOOKS.parent / "tests/fixtures/transcripts"
        transcript = self.root / "session.jsonl"

        def stop(name):
            transcript.write_text(
                (fixture / name).read_text().replace("@CWD@", str(self.root))
            )
            result = subprocess.run(
                [sys.executable, str(HOOKS / "clean-state-check.py")],
                input=json.dumps(
                    {
                        "cwd": str(self.root),
                        "session_id": "fixture",
                        "transcript_path": str(transcript),
                    }
                ),
                capture_output=True,
                text=True,
                timeout=10,
                env=dict(
                    os.environ,
                    AIARCH_CONFIG=str(self.root / "absent.json"),
                    CLAUDE_HOOK_STATE_DIR=str(self.root / "state"),
                ),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        self.assertEqual(stop("state-wrote-code.jsonl"), "")
        active = plans / "active"
        active.mkdir()
        (active / "TRACKER.md").write_text(
            "Status: active\n## Resume here\n**Next:** test\n"
        )
        self.assertEqual(
            json.loads(stop("state-wrote-code.jsonl"))["decision"], "block"
        )
        self.assertEqual(stop("state-wrote-code-and-tracker.jsonl"), "")


if __name__ == "__main__":
    unittest.main()
