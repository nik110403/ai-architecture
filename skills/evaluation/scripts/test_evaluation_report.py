import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL = HERE / "evaluation_report.py"
FIXTURES = ROOT / "fixtures"


def run(paths, *args):
    command = [sys.executable, "-B", str(TOOL)]
    for path in paths:
        command.extend(("--input", str(path)))
    return subprocess.run(command + list(args), capture_output=True, text=True)


class EvaluationReportTests(unittest.TestCase):
    def report(self, paths, *args):
        result = run(paths, "--format", "json", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_latest_tie_and_variant_charge_join(self):
        report = self.report([FIXTURES / "evaluation-primary.jsonl"])
        maker = next(
            row
            for row in report["maker_verdicts"]
            if row["role"] == "implementer--terra"
        )
        self.assertEqual(
            (maker["role"], maker["checker_pass"], maker["checker_fail"]),
            ("implementer--terra", 0, 1),
        )
        checker = report["checker_verdicts"]
        self.assertEqual(sum(row["pass"] for row in checker), 4)
        self.assertEqual(sum(row["fail"] for row in checker), 1)

    def test_null_and_missing_charge_ids_are_unjoined(self):
        report = self.report([FIXTURES / "evaluation-primary.jsonl"])
        makers = {row["role"]: row for row in report["maker_verdicts"]}
        self.assertEqual(makers["design-builder"]["checker_none"], 1)
        self.assertEqual(makers["implementer"]["checker_none"], 1)
        self.assertEqual(
            report["joins"],
            {
                "rule": "charge_id only; latest known timestamp wins; equal timestamps use last input order",
                "unmatched_verdicts": 1,
                "unknown_timestamp_verdicts": 1,
            },
        )

    def test_multiple_files_and_window_filter_before_join(self):
        report = self.report(
            [
                FIXTURES / "evaluation-primary.jsonl",
                FIXTURES / "evaluation-second.jsonl",
            ],
            "--since",
            "20",
        )
        self.assertEqual(
            report["window"],
            {"since": 20, "included": 2, "excluded_null_t": 2, "excluded_stale_t": 7},
        )
        maker = report["maker_verdicts"][0]
        self.assertEqual(
            (maker["runs"], maker["checker_pass"], maker["checker_none"]), (1, 1, 0)
        )

    def test_deterministic_and_empty_are_explicit(self):
        paths = [FIXTURES / "evaluation-empty.jsonl"]
        first, second = run(paths, "--format", "json"), run(paths, "--format", "json")
        self.assertEqual((first.returncode, first.stdout), (0, second.stdout))
        report = json.loads(first.stdout)
        self.assertEqual(
            (report["evidence"], report["maker_verdicts"], report["checker_verdicts"]),
            ("none", [], []),
        )

    def test_malformed_unknown_and_duplicate_fields_are_controlled(self):
        with tempfile.TemporaryDirectory() as directory:
            for contents in (
                '{"schema_version":1,"extra":1}\n',
                '{"schema_version":1,"schema_version":1}\n',
            ):
                path = Path(directory) / "bad.jsonl"
                path.write_text(contents, encoding="utf-8")
                result = run([path], "--format", "json")
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("invalid input", result.stderr)

    def test_standalone_bundle_requires_sibling_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recipient"
            (destination / "scripts").mkdir(parents=True)
            for name in ("evaluation-report.md", "receipt-report.md"):
                shutil.copy2(ROOT / name, destination / name)
            for tool in (TOOL, HERE / "receipt_report.py"):
                shutil.copy2(tool, destination / "scripts" / tool.name)
            shutil.copytree(FIXTURES, destination / "fixtures")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(destination / "scripts" / TOOL.name),
                    "--input",
                    str(destination / "fixtures" / "evaluation-primary.jsonl"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("offline charge_id checker joins", result.stdout)


if __name__ == "__main__":
    unittest.main()
