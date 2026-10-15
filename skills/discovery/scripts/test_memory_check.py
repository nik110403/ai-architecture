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
TOOL = HERE / "memory_check.py"


def run(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(TOOL), "--root", str(root), *args],
        capture_output=True,
        text=True,
    )


class MemoryCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "memory"
        self.root.mkdir()

    def write(self, name, text, binary=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text) if binary else path.write_text(text, encoding="utf-8")
        return path

    def indexed(self, *names):
        self.write("MEMORY.md", "\n".join(f"- [{name}]({name}.md)" for name in names))

    def report(self, *args):
        result = run(self.root, "--format", "json", *args)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result, json.loads(result.stdout)

    def checks(self, report):
        return {(item["severity"], item["check"]) for item in report["findings"]}

    def module(self):
        spec = importlib.util.spec_from_file_location("memory_check", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_clean_default_and_strict_exit_zero(self):
        self.indexed("note")
        self.write("note.md", "[[note]]\n2026-10-15\n")
        for args in ((), ("--strict",)):
            result, report = self.report(*args)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(
                report["counts"],
                {"drift": 0, "inaccessible": 0, "info": 0, "unsupported": 0},
            )

    def test_missing_target_unindexed_stale_isolated_and_long_line(self):
        self.write("MEMORY.md", "- [missing](missing.md)\n" + "x" * 121)
        self.write("orphan.md", "2020-01-01\n")
        default, report = self.report()
        self.assertEqual(default.returncode, 0)
        self.assertTrue(
            {
                ("drift", "missing-index-target"),
                ("drift", "unindexed-file"),
                ("info", "stale-explicit-date"),
                ("info", "isolated-file"),
                ("info", "long-index-line"),
            }
            <= self.checks(report)
        )
        strict, _ = self.report("--strict")
        self.assertEqual(strict.returncode, 1)

    def test_dangling_wikilink_and_configured_path_reference(self):
        self.indexed("note")
        missing = self.root / "gone"
        self.write("note.md", f"[[absent]]\n{missing}\n")
        _, report = self.report("--path-root", str(self.root))
        self.assertTrue(
            {("info", "dangling-wikilink"), ("info", "missing-path-reference")}
            <= self.checks(report)
        )
        outside = self.root.parent / "outside"
        outside.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL),
                "--root",
                str(self.root),
                "--path-root",
                str(outside),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("path root must", result.stderr)

    def test_malformed_inaccessible_and_symlink_are_incomplete(self):
        self.write("MEMORY.md", "- [note](note.md)\n- [escape](escape.md)\n")
        self.write("note.md", b"\xff", binary=True)
        self.write("not-a-file.md/child", "ignored")
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.root / "escape.md").symlink_to(outside)
        _, report = self.report()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn(("unsupported", "encoding"), self.checks(report))
        self.assertIn(("unsupported", "symlink-file"), self.checks(report))
        self.assertIn(("unsupported", "index-target"), self.checks(report))
        module = self.module()
        findings = []
        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError("denied")
        ):
            self.assertIsNone(
                module.safe_text(self.root, self.root / "MEMORY.md", findings)
            )
        self.assertEqual(findings[0]["severity"], "inaccessible")

    def test_symlink_hop_never_stats_or_opens_outside_for_references(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        secret = outside / "secret.md"
        secret.write_text("outside", encoding="utf-8")
        (self.root / "hop").symlink_to(outside, target_is_directory=True)
        target = self.root / "target.md"
        target.symlink_to(secret)
        self.write("MEMORY.md", "- [note](note.md)\n- [escape](hop/secret.md)\n")
        self.write("note.md", f"[[target]]\n{self.root}/hop/secret.md\n")
        module, seen = self.module(), []
        original = module.os.lstat

        def recording(path):
            seen.append(Path(path))
            return original(path)

        with mock.patch.object(module.os, "lstat", side_effect=recording):
            report = module.scan(self.root, self.root, module.date(2026, 4, 15))
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(
            {
                ("unsupported", "index-target"),
                ("unsupported", "wikilink-target"),
                ("unsupported", "path-reference"),
            }
            <= self.checks(report)
        )
        self.assertTrue(all(not path.is_relative_to(outside) for path in seen), seen)

    def test_malformed_index_constructs_are_incomplete(self):
        for line in (
            "[broken](absent.md",
            "[empty]()",
            "](note.md)",
            "[space](note file.md)",
            "[other](note.txt)",
        ):
            with self.subTest(line=line):
                self.write("MEMORY.md", line)
                _, report = self.report()
                self.assertEqual(report["status"], "incomplete")
                self.assertTrue(
                    {
                        ("unsupported", "malformed-index-link"),
                        ("unsupported", "index-link"),
                    }
                    & self.checks(report)
                )

    def test_deterministic_text_json_and_no_mutation(self):
        self.write("MEMORY.md", "- [missing](missing.md)\n")
        self.write("z.md", "[[a]]\n2020-01-01")
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        first = run(self.root)
        second = run(self.root)
        self.assertEqual(first.stdout, second.stdout)
        _, report = self.report()
        self.assertEqual(
            report["findings"],
            sorted(
                report["findings"],
                key=lambda item: (
                    item["severity"],
                    item["check"],
                    item["path"],
                    item["detail"],
                ),
            ),
        )
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_copied_standalone_bundle_help_and_actual_report(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recipient"
            shutil.copytree(
                HERE.parent,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "test_memory_check.py"),
            )
            tool = destination / "scripts" / "memory_check.py"
            help_result = subprocess.run(
                [sys.executable, "-B", str(tool), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--root", help_result.stdout)
            actual = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(tool),
                    "--root",
                    str(destination / "fixtures" / "memory-check-clean"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertIn("memory-check: complete", actual.stdout)

    def test_invalid_root_is_controlled_nonzero(self):
        result = subprocess.run(
            [sys.executable, "-B", str(TOOL), "--root", str(self.root / "missing")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid input", result.stderr)


if __name__ == "__main__":
    unittest.main()
