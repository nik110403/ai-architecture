"""Deterministic checker fixture, not an AI judge or production acceptance."""

import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "parser.py"
SPEC = importlib.util.spec_from_file_location("fixture_parser", PATH)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


class ParserTests(unittest.TestCase):
    def test_empty_and_whitespace(self):
        for value in ("", " ", "\n\t"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "must not be empty"),
            ):
                PARSER.parse_numbers(value)

    def test_nonempty_signed_numbers(self):
        self.assertEqual(PARSER.parse_numbers("2, 3,5"), [2, 3, 5])
        self.assertEqual(PARSER.parse_numbers(" -12, +4, 0 "), [-12, 4, 0])
        self.assertEqual(PARSER.parse_numbers("7"), [7])

    def test_malformed_nonempty(self):
        for value in (",", "1,", ",2", "1,,2", "one", "1.5", "1 2", "--1", "1e3"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "comma-separated integers"),
            ):
                PARSER.parse_numbers(value)

    def test_nontext_rejected(self):
        for value in (None, 12, [1, 2], b"1,2"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(TypeError, "must be text"),
            ):
                PARSER.parse_numbers(value)

    def test_separate_results(self):
        result = PARSER.parse_numbers("1,2")
        result.append(99)
        self.assertEqual(PARSER.parse_numbers("1,2"), [1, 2])


if __name__ == "__main__":
    unittest.main()
