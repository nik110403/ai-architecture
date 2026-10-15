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
TOOL = HERE / "loop_check.py"


def run(registry, adapters, *args):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(TOOL),
            "--registry",
            str(registry),
            "--adapters",
            str(adapters),
            *args,
        ],
        capture_output=True,
        text=True,
    )


class LoopCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry, self.adapters = (
            self.root / "registry.json",
            self.root / "adapters.json",
        )

    def valid(self, loops, adapters):
        self.registry.write_text(
            json.dumps({"schema_version": 1, "loops": loops}), encoding="utf-8"
        )
        self.adapters.write_text(
            json.dumps({"schema_version": 1, "adapters": adapters}), encoding="utf-8"
        )

    def report(self, *args):
        result = run(self.registry, self.adapters, "--format", "json", *args)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result, json.loads(result.stdout)

    def test_all_outcomes_and_adapter_absence(self):
        self.valid(
            [
                {"id": "z-live", "adapter": "active", "status": "live"},
                {"id": "a-info", "adapter": "inactive", "status": "live"},
                {"id": "m-drift", "adapter": "missing", "status": "live"},
                {"id": "u-unavailable", "adapter": "unavailable", "status": "live"},
                {"id": "b-absent", "adapter": "absent", "status": "live"},
                {"id": "p-paused", "adapter": "active", "status": "paused"},
                {"id": "r-retired", "adapter": "active", "status": "retired"},
            ],
            [
                {"id": "active", "state": "active"},
                {"id": "inactive", "state": "inactive"},
                {"id": "missing", "state": "missing"},
                {"id": "unavailable", "state": "unavailable"},
                {"id": "unused", "state": "active"},
            ],
        )
        result, value = self.report()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(value["complete"])
        self.assertEqual(
            value["counts"],
            {"checked": 1, "info": 1, "drift": 1, "unavailable": 2, "skipped": 2},
        )
        self.assertEqual(
            [row["id"] for row in value["results"]],
            sorted(row["id"] for row in value["results"]),
        )

    def test_deterministic_text_json_and_strict_equivalence(self):
        self.valid(
            [{"id": "loop", "adapter": "adapter", "status": "live"}],
            [{"id": "adapter", "state": "missing"}],
        )
        default, repeated, strict = (
            run(self.registry, self.adapters),
            run(self.registry, self.adapters),
            run(self.registry, self.adapters, "--strict"),
        )
        json_default, json_strict = (
            run(self.registry, self.adapters, "--format", "json"),
            run(self.registry, self.adapters, "--format", "json", "--strict"),
        )
        self.assertEqual(default.stdout, repeated.stdout)
        self.assertEqual(default.stdout, strict.stdout)
        self.assertEqual(json_default.stdout, json_strict.stdout)
        self.assertEqual((default.returncode, strict.returncode), (0, 1))

    def test_unavailable_only_is_incomplete_but_not_strict_failure(self):
        self.valid(
            [{"id": "loop", "adapter": "adapter", "status": "live"}],
            [{"id": "adapter", "state": "unavailable"}],
        )
        result, value = self.report("--strict")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(value["complete"])
        self.assertEqual(value["counts"]["unavailable"], 1)

    def test_invalid_inputs_are_controlled(self):
        invalids = [
            (
                '{"schema_version": 1, "schema_version": 1, "loops": []}',
                '{"schema_version": 1, "adapters": []}',
            ),
            (
                '{"schema_version": true, "loops": []}',
                '{"schema_version": 1, "adapters": []}',
            ),
            (
                '{"schema_version": 1, "loops": [{"id":"x", "adapter":"a", "status":"bad"}]}',
                '{"schema_version": 1, "adapters": []}',
            ),
            (
                '{"schema_version": 1, "loops": [{"id":"x", "adapter":"a", "status":"live"}, {"id":"x", "adapter":"a", "status":"live"}]}',
                '{"schema_version": 1, "adapters": []}',
            ),
            (
                '{"schema_version": 1, "loops": []}',
                '{"schema_version": 1, "adapters": [{"id":"a", "state":"active", "extra":1}]}',
            ),
            ("{", '{"schema_version": 1, "adapters": []}'),
        ]
        for registry, adapters in invalids:
            with self.subTest(registry=registry):
                self.registry.write_text(registry, encoding="utf-8")
                self.adapters.write_text(adapters, encoding="utf-8")
                result = run(self.registry, self.adapters)
                self.assertEqual(result.returncode, 2)
                self.assertIn("invalid input", result.stderr)
        self.assertEqual(run(self.root / "missing.json", self.adapters).returncode, 2)

    def test_deeply_nested_malformed_json_is_controlled(self):
        self.registry.write_text("[" * 10000, encoding="utf-8")
        self.adapters.write_text(
            '{"schema_version": 1, "adapters": []}', encoding="utf-8"
        )
        result = run(self.registry, self.adapters)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid input", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_only_explicit_files_are_read_and_inputs_are_unchanged(self):
        self.valid(
            [{"id": "loop", "adapter": "adapter", "status": "live"}],
            [{"id": "adapter", "state": "active"}],
        )
        before = {path: path.read_bytes() for path in (self.registry, self.adapters)}
        spec = importlib.util.spec_from_file_location("loop_check", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original, reads = Path.read_text, []

        def recording(path, *args, **kwargs):
            reads.append(Path(path))
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", recording):
            self.assertEqual(
                module.main(
                    ["--registry", str(self.registry), "--adapters", str(self.adapters)]
                ),
                0,
            )
        self.assertEqual(reads, [self.registry, self.adapters])
        self.assertEqual(
            before, {path: path.read_bytes() for path in (self.registry, self.adapters)}
        )
        self.assertNotIn("subprocess", module.__dict__)

    def test_copied_standalone_bundle_with_authored_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recipient"
            (destination / "scripts").mkdir(parents=True)
            shutil.copy2(HERE.parent / "SKILL.md", destination / "SKILL.md")
            shutil.copy2(HERE.parent / "maintenance.md", destination / "maintenance.md")
            shutil.copy2(TOOL, destination / "scripts/loop_check.py")
            shutil.copytree(HERE.parent / "fixtures", destination / "fixtures")
            self.assertIn(
                "[maintenance.md](maintenance.md)",
                (destination / "SKILL.md").read_text(),
            )
            self.assertTrue((destination / "maintenance.md").is_file())
            self.assertTrue((destination / "fixtures/registry.json").is_file())
            self.assertTrue((destination / "fixtures/adapters.json").is_file())
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(destination / "scripts/loop_check.py"),
                    "--registry",
                    str(destination / "fixtures/registry.json"),
                    "--adapters",
                    str(destination / "fixtures/adapters.json"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("loop-check: snapshot report", result.stdout)


if __name__ == "__main__":
    unittest.main()
