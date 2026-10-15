import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL = HERE / "receipt_report.py"
FIXTURES = ROOT / "fixtures"


def run(paths, *args):
    command = [sys.executable, "-B", str(TOOL)]
    for path in paths:
        command.extend(("--input", str(path)))
    return subprocess.run(command + list(args), capture_output=True, text=True)


class ReceiptReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, contents, binary=False):
        path = self.root / name
        if binary:
            path.write_bytes(contents)
        else:
            path.write_text(contents, encoding="utf-8")
        return path

    def report(self, paths, *args):
        result = run(paths, "--format", "json", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_mixed_receipts_preserve_nulls_and_observed_totals(self):
        report = self.report([FIXTURES / "mixed.jsonl"])
        group = report["groups"][0]
        self.assertEqual(
            report["inputs"], {"files": 1, "nonblank_lines": 4, "blank_lines": 0}
        )
        self.assertEqual(
            {
                key: group[key]
                for key in (
                    "usage_known",
                    "usage_partial",
                    "usage_missing",
                    "usage_excluded",
                )
            },
            {
                "usage_known": 1,
                "usage_partial": 1,
                "usage_missing": 1,
                "usage_excluded": 1,
            },
        )
        self.assertEqual(group["usage_label"], "INCOMPLETE")
        self.assertEqual(
            (group["observed_tokens_total"], group["observed_tokens_median"]), (17, 8.5)
        )
        self.assertEqual(
            (
                group["cost_usd_sum"],
                group["cost_usd_measured"],
                group["cost_usd_missing"],
            ),
            (3.5, 3, 1),
        )
        self.assertEqual((group["dur_ms_median"], group["dur_ms_measured"]), (20, 3))
        self.assertIsNone(report["receipts"][2]["in_tok"])
        self.assertEqual(report["receipts"][2]["verify"], "none")

    def test_first_wins_across_files_and_rows_without_task_ids_do_not_dedup(self):
        first = self.write(
            "first.jsonl",
            '{"schema_version":1,"task_id":"x","source":"s","harness":"z","role":"z","model":"z"}\n{"schema_version":1,"task_id":null,"source":"s","harness":"b","role":"b","model":"b"}\n',
        )
        second = self.write(
            "second.jsonl",
            '{"schema_version":1,"task_id":"x","source":"s","harness":"a","role":"a","model":"a"}\n{"schema_version":1,"task_id":null,"source":"s","harness":"a","role":"a","model":"a"}\n',
        )
        report = self.report([first, second])
        self.assertEqual(report["dedup"], {"duplicates": 1, "receipts_after_dedup": 3})
        self.assertEqual(
            [group["harness"] for group in report["groups"]], ["a", "b", "z"]
        )
        self.assertEqual(report["receipts"][0]["harness"], "z")

    def test_since_includes_boundary_and_excludes_null_and_stale(self):
        report = self.report([FIXTURES / "window.jsonl"], "--since", "10")
        self.assertEqual(
            report["window"],
            {"since": 10, "included": 1, "excluded_null_t": 1, "excluded_stale_t": 1},
        )
        self.assertEqual(report["receipts"][0]["task_id"], "boundary")

    def test_empty_file_is_explicit_no_evidence(self):
        report = self.report([self.write("empty.jsonl", "\n\n")])
        self.assertEqual(
            report["inputs"], {"files": 1, "nonblank_lines": 0, "blank_lines": 2}
        )
        self.assertEqual((report["receipts"], report["groups"]), ([], []))

    def test_invalid_rows_including_outside_window_produce_no_report(self):
        invalids = [
            '{"schema_version":1,"extra":1}',
            '{"schema_version":true}',
            '{"schema_version":1,"verify":null}',
            '{"schema_version":1,"model":"  "}',
            '{"schema_version":1,"t":true}',
            '{"schema_version":1,"cost_usd":NaN}',
            '{"schema_version":1,"dur_ms":Infinity}',
            '{"schema_version":1,"schema_version":1}',
            "[1]",
        ]
        for index, contents in enumerate(invalids):
            with self.subTest(contents=contents):
                result = run(
                    [self.write(f"bad-{index}.jsonl", contents + "\n")], "--since", "99"
                )
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("input 1 line 1", result.stderr)
        result = run([self.write("deep.jsonl", "[" * 10000)])
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertNotIn("Traceback", result.stderr)

    def test_unreadable_and_invalid_utf8_are_controlled(self):
        missing = run([self.root / "missing.jsonl"])
        self.assertEqual((missing.returncode, missing.stdout), (2, ""))
        bad_bytes = run([self.write("bad.jsonl", b"\xff", binary=True)])
        self.assertEqual((bad_bytes.returncode, bad_bytes.stdout), (2, ""))

    def test_overflowing_cost_sum_is_controlled_in_both_formats(self):
        path = self.write(
            "cost-overflow.jsonl",
            '{"schema_version":1,"cost_usd":1e308}\n' * 2,
        )
        for output in ("json", "text"):
            with self.subTest(output=output):
                result = run([path], "--format", output)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("numeric range", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_large_float_duration_median_remains_finite(self):
        path = self.write("duration.jsonl", '{"schema_version":1,"dur_ms":1e308}\n' * 2)
        report = self.report([path])
        self.assertEqual(report["groups"][0]["dur_ms_median"], 1e308)
        self.assertEqual(len(report["receipts"]), 2)

    def test_large_integer_cost_is_preserved_without_float_conversion(self):
        value = 10**400
        path = self.write(
            "integer.jsonl", json.dumps({"schema_version": 1, "cost_usd": value})
        )
        report = self.report([path])
        self.assertEqual(report["receipts"][0]["cost_usd"], value)
        self.assertEqual(report["groups"][0]["cost_usd_sum"], value)

    def test_large_even_integer_duration_median_is_exact(self):
        value = 10**400
        path = self.write(
            "integer-duration.jsonl",
            json.dumps({"schema_version": 1, "dur_ms": value})
            + "\n"
            + json.dumps({"schema_version": 1, "dur_ms": value + 2})
            + "\n",
        )
        report = self.report([path])
        self.assertEqual(report["groups"][0]["dur_ms_median"], value + 1)

    def test_unrepresentable_mixed_sum_is_controlled(self):
        path = self.write(
            "mixed-range.jsonl",
            json.dumps({"schema_version": 1, "cost_usd": 10**400})
            + '\n{"schema_version":1,"cost_usd":0.5}\n',
        )
        result = run([path], "--format", "json")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("numeric range", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_reads_only_explicit_paths_and_never_mutates_them(self):
        path = self.write("one.jsonl", '{"schema_version":1}\n')
        before = path.read_bytes()
        spec = importlib.util.spec_from_file_location("receipt_report", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original, reads = Path.open, []

        def recording(path, *args, **kwargs):
            reads.append(Path(path))
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", recording):
            self.assertEqual(module.main(["--input", str(path)]), 0)
        self.assertEqual(reads, [path])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("subprocess", module.__dict__)

    def test_standalone_selective_bundle_runs_authored_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recipient"
            (destination / "scripts").mkdir(parents=True)
            for name in (
                "SKILL.md",
                "measurement.md",
                "receipt-report.md",
            ):
                shutil.copy2(ROOT / name, destination / name)
            shutil.copy2(TOOL, destination / "scripts" / TOOL.name)
            shutil.copytree(FIXTURES, destination / "fixtures")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(destination / "scripts" / TOOL.name),
                    "--input",
                    str(destination / "fixtures" / "mixed.jsonl"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("receipt-report: offline accounting", result.stdout)


if __name__ == "__main__":
    unittest.main()
