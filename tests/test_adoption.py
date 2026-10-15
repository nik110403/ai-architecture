import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "adoption" / "run.py"
LOCAL_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\s#]+)(?:#[^)\s]*)?\)")


class AdoptionTests(unittest.TestCase):
    def test_selective_copy_runs_recipient_clis_in_space_unicode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            recipient = Path(directory) / "recipient space ü"
            result = subprocess.run(
                [sys.executable, "-B", str(EXAMPLE), "--recipient", str(recipient)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("adoption fixture: PASS", result.stdout)
            self.assertEqual(
                json.loads(
                    (recipient / "artifacts/project/adoption-summary.json").read_text()
                ),
                {
                    "total": 10,
                    "executed_fixed_next": "write adoption-summary.json from verified inputs.json",
                },
            )

    def test_declared_links_and_assets_arrive_with_each_selective_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            recipient = Path(directory) / "recipient"
            result = subprocess.run(
                [sys.executable, "-B", str(EXAMPLE), "--recipient", str(recipient)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            skills = recipient / ".claude" / "skills"
            for bundle in ("discovery", "maintenance", "evaluation", "continuity"):
                with self.subTest(bundle=bundle):
                    documents = (skills / bundle).glob("*.md")
                    for document in documents:
                        for target in LOCAL_LINK.findall(
                            document.read_text(encoding="utf-8")
                        ):
                            if "://" not in target and not target.startswith("#"):
                                linked = document.parent / target
                                self.assertTrue(
                                    linked.is_dir()
                                    if target.endswith("/")
                                    else linked.is_file(),
                                    f"{document.relative_to(skills)} -> {target}",
                                )
            self.assertTrue(
                (skills / "evaluation/scripts/evaluation_report.py").is_file()
            )
            self.assertTrue((skills / "evaluation/scripts/receipt_report.py").is_file())
            self.assertTrue(
                (skills / "continuity/scripts/continuity_check.py").is_file()
            )

    def test_source_collection_and_authored_recipient_inputs_are_not_mutated(self):
        source_inputs = {
            path: path.read_bytes()
            for path in (
                ROOT / "skills/discovery/scripts/memory_check.py",
                ROOT / "skills/maintenance/scripts/loop_check.py",
                ROOT / "skills/evaluation/scripts/receipt_report.py",
                ROOT / "skills/continuity/scripts/journal.py",
            )
        }
        with tempfile.TemporaryDirectory() as directory:
            recipient = Path(directory) / "recipient"
            result = subprocess.run(
                [sys.executable, "-B", str(EXAMPLE), "--recipient", str(recipient)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (recipient / "artifacts/project/inputs.json").read_text(
                    encoding="utf-8"
                ),
                "[2, 3, 5]\n",
            )
        self.assertEqual(
            source_inputs, {path: path.read_bytes() for path in source_inputs}
        )


if __name__ == "__main__":
    unittest.main()
