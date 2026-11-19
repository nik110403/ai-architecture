import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import capabilities
from scripts.capabilities import evidence_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capabilities.py"
REGISTRY = ROOT / "capabilities" / "registry.json"


class CapabilityTests(unittest.TestCase):
    def command(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def fixture(self, change):
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        change(data)
        temp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        )
        self.addCleanup(Path(temp.name).unlink)
        with temp:
            json.dump(data, temp)
        return Path(temp.name)

    def test_check_and_list_cover_all_ids(self):
        checked = self.command("check")
        listed = self.command("list")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("72 declared capabilities", checked.stdout)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(listed.stdout.splitlines()), 72)
        self.assertIn("01\tIntent routing\tpartial", listed.stdout)
        self.assertIn("57\tRetro/gated learning\tplanned", listed.stdout)

    def test_status_is_actionable_and_never_discovers(self):
        result = self.command("status", "57")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("planned (unknown)", result.stdout)
        self.assertIn("next setup action:", result.stdout)

    def test_full_status_explains_the_supported_states(self):
        result = self.command("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "available, partial, planned, unavailable, unsupported", result.stdout
        )

    def test_duplicate_ids_are_rejected_cleanly(self):
        path = self.fixture(
            lambda data: data["capabilities"].__setitem__(1, data["capabilities"][0])
        )
        result = self.command("--registry", str(path), "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate capability ID", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_state_is_rejected_cleanly(self):
        path = self.fixture(
            lambda data: data["capabilities"][0].__setitem__("support", "invented")
        )
        result = self.command("--registry", str(path), "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid support state", result.stderr)

    def test_path_escape_is_rejected_cleanly(self):
        def escape(data):
            entry = data["capabilities"][0]
            entry["evidence_class"] = "artifact"
            entry["evidence"] = ["../secret.txt"]

        result = self.command("--registry", str(self.fixture(escape)), "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("escaping evidence path", result.stderr)

    def test_evidence_rejects_ancestor_symlink_before_external_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, external = base / "repo", base / "external"
            (root / "assets").mkdir(parents=True)
            external.mkdir()
            normal = root / "assets" / "receipt.txt"
            normal.write_text("inside", encoding="utf-8")
            (external / "receipt.txt").write_text("outside", encoding="utf-8")
            (root / "linked-assets").symlink_to(external, target_is_directory=True)

            evidence_path(root, "assets/receipt.txt")
            with self.assertRaisesRegex(ValueError, "escaping evidence path"):
                evidence_path(root, "linked-assets/receipt.txt")

    def test_unknown_availability_is_not_available(self):
        path = self.fixture(
            lambda data: data["capabilities"][56].__setitem__("support", "available")
        )
        result = self.command("--registry", str(path), "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown availability cannot be available", result.stderr)

    def command_env(self, path, *args, env_path=None):
        env = dict(os.environ)
        if env_path is not None:
            env["PATH"] = env_path
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--registry", str(path), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )

    def with_requirements(self, entry_index, items):
        return self.fixture(
            lambda data: data["capabilities"][entry_index].__setitem__(
                "optional_requirements", items
            )
        )

    def test_missing_command_and_module_are_reported_without_failing(self):
        items = [
            {"kind": "command", "name": "no-such-tool-xyz", "install": "Get it here"},
            {"kind": "python-module", "name": "no_such_module_xyz", "install": "pip it"},
        ]
        path = self.with_requirements(0, items)
        result = self.command_env(path, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no-such-tool-xyz (command): MISSING, Get it here", result.stdout)
        self.assertIn("no_such_module_xyz (python-module): MISSING, pip it", result.stdout)
        self.assertIn("72 ", result.stdout)
        self.assertEqual(result.stdout.count("next setup action:"), 72)

    def test_found_command_is_not_executed_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "ran"
            tool = base / "faketool"
            tool.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            tool.chmod(0o755)
            path = self.with_requirements(
                0, [{"kind": "command", "name": "faketool", "install": "n/a"}]
            )
            result = self.command_env(path, "status", "01", env_path=str(base))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("optional requirement: faketool (command): found", result.stdout)
            self.assertIn("partial (declared)", result.stdout)
            self.assertFalse(marker.exists())

    def test_status_prints_report_fields(self):
        result = self.command("status", "04")
        self.assertEqual(result.returncode, 0, result.stderr)
        for field in ("module:", "depends on: none", "evidence class: artifact"):
            self.assertIn(field, result.stdout)
        self.assertRegex(
            result.stdout, r"optional requirement: opencode \(command\): (found|MISSING)"
        )

    def test_requirement_rules_are_refused(self):
        good = {"kind": "command", "name": "git", "install": "x"}
        cases = {
            "wrong type": "git",
            "unknown kind": [{**good, "kind": "binary"}],
            "path separator": [{**good, "name": "bin/git"}],
            "leading dash": [{**good, "name": "-git"}],
            "dotted module": [{"kind": "python-module", "name": "a.b", "install": "x"}],
            "empty install": [{**good, "install": " "}],
            "non-string install": [{**good, "install": 3}],
            "extra key": [{**good, "extra": 1}],
            "missing key": [{"kind": "command", "name": "git"}],
            "duplicate": [good, dict(good)],
            "non-dict item": ["git"],
        }
        for label, value in cases.items():
            with self.subTest(label):
                result = self.command("--registry", str(self.with_requirements(0, value)), "check")
                self.assertEqual(result.returncode, 1)
                self.assertIn("FAIL:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_missing_requirements_field_is_refused(self):
        path = self.fixture(lambda d: d["capabilities"][0].pop("optional_requirements"))
        result = self.command("--registry", str(path), "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("schema fields mismatch", result.stderr)

    def test_check_passes_when_all_requirements_absent_from_host(self):
        items = [{"kind": "command", "name": "no-such-tool-xyz", "install": "x"}]
        result = self.command("--registry", str(self.with_requirements(0, items)), "check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_string_id_and_module_are_clean_failures(self):
        for field, bad in (("id", ["01"]), ("id", {"a": 1}), ("id", 1), ("module", ["x"])):
            with self.subTest(field=field, bad=bad):
                path = self.fixture(lambda d: d["capabilities"][0].__setitem__(field, bad))
                result = self.command("--registry", str(path), "check")
                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid", result.stderr)
                self.assertNotIn("unhashable", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        path = self.fixture(lambda d: d["capabilities"][0].__setitem__("id", ["01"]))
        self.assertIn(
            "invalid capability ID", self.command("--registry", str(path), "check").stderr
        )

    def test_real_registry_actions_and_requirements(self):
        entries = json.loads(REGISTRY.read_text(encoding="utf-8"))["capabilities"]
        actions = [e["next_setup_action"] for e in entries]
        self.assertEqual(len(set(actions)), 72)
        for action in actions:
            self.assertIsNone(re.search(r"\bU\d\d\b", action))
            self.assertNotIn("Implement and verify the declared recipient adapter", action)
        declared = {
            e["id"]: e["optional_requirements"] for e in entries if e["optional_requirements"]
        }
        pairs = {
            e["id"]: [(r["kind"], r["name"]) for r in e["optional_requirements"]]
            for e in entries
            if e["optional_requirements"]
        }
        cmd = lambda *names: [("command", n) for n in names]
        chrome = "google-chrome"
        self.assertEqual(
            pairs,
            {
                "04": cmd("opencode"),
                "07": cmd("git"),
                "08": cmd("git"),
                "21": cmd("graphify"),
                "27": cmd("bwrap"),
                "31": cmd(chrome),
                "33": cmd("provenote"),
                "34": cmd("provenote"),
                "35": cmd("provenote"),
                "36": cmd("provenote"),
                "37": cmd("provenote"),
                "38": cmd("provenote", "Rscript"),
                "41": cmd("git"),
                "42": cmd("git"),
                "43": cmd("git"),
                "44": cmd("git"),
                "45": cmd(chrome),
                "46": cmd(chrome),
                "47": cmd(chrome, "pdfinfo", "pdftotext", "pdffonts", "fc-list"),
                "48": cmd(chrome, "fc-list", "git"),
                "49": cmd(chrome, "ffmpeg", "ffprobe", "fc-list"),
                "50": [("python-module", "playwright")],
                "53": cmd("soffice", "pdftotext", "pandoc"),
                "61": cmd("systemd-run"),
                "66": cmd("git"),
            },
        )
        self.assertEqual(
            {e["id"] for e in entries if e["support"] == "planned"},
            {"21", "22", "32", "33", "34", "35", "36", "37", "38", "57", "58", "69", "70"},
        )

    def test_list_valued_state_fields_are_clean_failures(self):
        for field in ("support", "availability", "evidence_class"):
            for bad in (["planned"], {"a": 1}):
                with self.subTest(field=field, bad=bad):
                    path = self.fixture(
                        lambda d: d["capabilities"][0].__setitem__(field, bad)
                    )
                    result = self.command("--registry", str(path), "check")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("FAIL:", result.stderr)
                    self.assertNotIn("unhashable", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_check_and_list_skip_the_host_but_status_looks_up(self):
        out = io.StringIO()
        with mock.patch.object(
            capabilities, "requirement_found", return_value=True
        ) as found, mock.patch.object(
            capabilities.shutil, "which"
        ) as which, mock.patch.object(
            capabilities.importlib.util, "find_spec"
        ) as spec, contextlib.redirect_stdout(out):
            self.assertEqual(capabilities.main(["check"]), 0)
            self.assertEqual(capabilities.main(["list"]), 0)
        found.assert_not_called()
        which.assert_not_called()
        spec.assert_not_called()
        with mock.patch.object(
            capabilities, "requirement_found", return_value=True
        ) as found, contextlib.redirect_stdout(out):
            self.assertEqual(capabilities.main(["status", "04"]), 0)
        found.assert_called_once()

    def test_found_python_module_is_reported_without_importing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "imported"
            (base / "fakemod_xyz.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
                encoding="utf-8",
            )
            path = self.with_requirements(
                0, [{"kind": "python-module", "name": "fakemod_xyz", "install": "n/a"}]
            )
            env = dict(os.environ, PYTHONPATH=str(base))
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--registry", str(path), "status", "01"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "optional requirement: fakemod_xyz (python-module): found", result.stdout
            )
            self.assertFalse(marker.exists())

    def test_preview_states_are_honest(self):
        entries = json.loads(REGISTRY.read_text(encoding="utf-8"))["capabilities"]
        by_id = {e["id"]: e for e in entries}
        self.assertEqual(len({e["next_setup_action"] for e in entries}), 72)
        for entry in entries:
            with self.subTest(entry["id"]):
                self.assertNotEqual(entry["support"], "available")
                if entry["support"] == "planned":
                    self.assertEqual(entry["evidence"], [])
                else:
                    self.assertGreaterEqual(len(entry["evidence"]), 2)
                    self.assertNotIn("Nothing to install yet", entry["next_setup_action"])
        for identifier in ("57", "58"):
            self.assertEqual(by_id[identifier]["support"], "planned")
            self.assertTrue(
                by_id[identifier]["next_setup_action"].startswith("Incomplete in this preview:")
            )
        self.assertNotIn("cannot parse", by_id["24"]["next_setup_action"])
        self.assertIn("tripwire", by_id["24"]["next_setup_action"])


if __name__ == "__main__":
    unittest.main()
