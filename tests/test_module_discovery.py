"""Runs module-level test suites under the project loop (`unittest discover -s tests`)."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite(standard_tests)
    seen = set()
    for path in sorted((ROOT / "modules").glob("*/tests/test_*.py")):
        if path.resolve() in seen:
            continue
        seen.add(path.resolve())
        spec = importlib.util.spec_from_file_location(f"module_tests_{path.parent.parent.name}_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


if __name__ == "__main__":
    unittest.main()
