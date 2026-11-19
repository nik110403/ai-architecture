import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "coverage_counts", Path(__file__).resolve().parents[1] / "scripts" / "coverage_counts.py"
)
cc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cc)


class ParseTests(unittest.TestCase):
    def test_unittest_ok_with_skips(self):
        out = "....\nRan 1440 tests in 820.6s\n\nOK (skipped=10)\n"
        self.assertEqual(cc.parse(out, 0), ("PASS", "1440 tests, 10 skipped"))

    def test_unittest_failure(self):
        out = "Ran 5 tests in 0.1s\n\nFAILED (failures=1)\n"
        self.assertEqual(cc.parse(out, 1), ("FAIL (exit 1)", "5 tests"))

    def test_pytest_summary(self):
        self.assertEqual(cc.parse("12 passed, 2 skipped in 3.4s\n", 0), ("PASS", "12 tests, 2 skipped"))

    def test_runner_totals_win_over_inner_unittest_line(self):
        out = "Ran 13 tests in 2.0s\nOK\n33 passed, 0 failed\n"
        self.assertEqual(cc.parse(out, 0), ("PASS", "33 tests"))

    def test_pass_equals_form(self):
        self.assertEqual(cc.parse("pass=33 fail=0\n", 0), ("PASS", "33 tests"))

    def test_nonzero_exit_with_not_run_text_is_fail(self):
        self.assertEqual(cc.parse("NOT RUN: bun missing\n", 1), ("FAIL (exit 1)", "n/a"))

    def test_plain_unittest_skips_are_pass(self):
        out = "Ran 1630 tests in 9.0s\n\nOK (skipped=3)\n"
        self.assertEqual(cc.parse(out, 0), ("PASS", "1630 tests, 3 skipped"))

    def test_exit_zero_with_not_run_marker(self):
        self.assertEqual(cc.parse("5 passed\nNOT RUN: x\n", 0), ("PASS, part NOT RUN", "5 tests"))

    def test_partial_skip_is_not_a_clean_pass(self):
        out = "67 passed, 0 failed\nOPENCODE TESTS PASSED WITH STRUCTURAL PARSER ACCEPTANCE SKIPPED\n"
        self.assertEqual(cc.parse(out, 0), ("PASS, part NOT RUN", "67 tests"))

    def test_no_count_is_not_a_zero(self):
        self.assertEqual(cc.parse("PASS: structure\n", 0), ("PASS", "n/a"))

    def test_render_is_deterministic_and_host_free(self):
        rows = [("s", ["bash", "x/run.sh"], "PASS", "1 tests")]
        self.assertEqual(cc.render(rows), cc.render(rows))
        self.assertIn("HOME=<temp dir> bash x/run.sh", cc.render(rows))


if __name__ == "__main__":
    unittest.main()
