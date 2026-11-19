import getpass
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STEWARD = ROOT / "modules/stewardship/steward.py"
EXPORT = ROOT / "modules/state-adapters/state_export.py"
VIEWER = ROOT / "modules/state-adapters/viewer.py"
MAPPING_SHA = "b21053e62ead5e142ac064ab64f50e11be697aa6aa293d3c08f3d89b7821eb58"
REGISTRY_SHA = "1527e8c1074f8e67ca1ee6edad262a51d98850be563e429e4f058542ddd2e63d"
REASON = "handshake 64 proposed, blocked_until live evidence"
STUB_LOG = "stub-ran"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.tags, self._cell = [], [], None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "tr":
            self.rows.append([])
        elif tag == "td":
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self.rows[-1].append("".join(self._cell))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.registry = self.tmp / "registry.json"

    def run_py(self, script, *args, env=None):
        return subprocess.run(
            [sys.executable, "-B", str(script), *map(str, args)],
            capture_output=True, text=True, cwd=self.tmp, timeout=120, env=env,
        )

    def make_project(self, name, nxt, dead_link=False):
        root = self.tmp / name
        root.mkdir()
        (root / "AGENTS.md").write_text("[x](gone.md)\n" if dead_link else "# Rules\n")
        done = self.run_py(STEWARD, "bootstrap", "--project", root, "--registry", self.registry)
        self.assertEqual(done.returncode, 0, done.stderr)
        tracker = root / "plans/main/TRACKER.md"
        tracker.write_text(
            tracker.read_text().replace("- **Next:** build S1", f"- **Next:** {nxt}")
        )
        return root

    def export(self, out=None):
        out = out or self.tmp / "out" / "state.json"
        out.parent.mkdir(exist_ok=True)
        done = self.run_py(EXPORT, "export", "--registry", self.registry, "--out", out)
        self.assertEqual(done.returncode, 0, done.stderr)
        return out

    def two_projects(self):
        self.alpha = self.make_project("alpha", "ship <script>x</script> & go", dead_link=True)
        self.beta = self.make_project("beta", "wait for review")
        return self.export()


class ExportTests(Base):
    def test_a8(self):
        out = self.two_projects()
        text = out.read_text()
        data = json.loads(text)
        self.assertEqual(set(data), {"schema_version", "projects"})
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["projects"]), 2)
        by_id = {p["id"]: p for p in data["projects"]}
        for project in data["projects"]:
            self.assertEqual(set(project), {"id", "root", "resurvey", "lint", "plans"})
            self.assertEqual(set(project["lint"]), {"findings", "by_code"})
            self.assertEqual(set(project["plans"][0]), {"topic", "status", "next"})
            lint = self.run_py(STEWARD, "lint", "--root", project["root"], "--format", "json")
            self.assertEqual(project["lint"]["findings"], len(json.loads(lint.stdout)["findings"]))
        self.assertGreaterEqual(by_id["alpha"]["lint"]["findings"], 1)
        self.assertTrue(by_id["alpha"]["lint"]["by_code"])
        self.assertEqual(by_id["beta"]["lint"]["findings"], 0)
        self.assertEqual(by_id["alpha"]["plans"][0]["next"], "ship <script>x</script> & go")
        self.assertEqual(by_id["beta"]["plans"][0]["next"], "wait for review")
        self.assertEqual(by_id["beta"]["plans"][0]["status"], "active")
        self.assertEqual(by_id["beta"]["resurvey"], "ok")
        stripped = text
        for project in data["projects"]:
            stripped = stripped.replace(json.dumps(project["root"]), "")
            stripped = stripped.replace(project["root"], "")
        keys = {"id", "root", "resurvey", "lint", "plans", "topic", "status", "next", "findings", "by_code"}
        for identity in (socket.gethostname(), getpass.getuser()):
            if len(identity) > 3 and identity not in keys:
                self.assertNotIn(identity, stripped)


class ViewerTests(Base):
    def render(self, state, out):
        return self.run_py(VIEWER, "render", "--state", state, "--out", out)

    def test_a9_render_from_real_export(self):
        state = self.two_projects()
        out = self.tmp / "page" / "index.html"
        out.parent.mkdir()
        done = self.render(state, out)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(list(out.parent.iterdir())), 1)
        table = Table()
        table.feed(out.read_text())
        data = json.loads(state.read_text())
        rows = [r for r in table.rows if r]
        self.assertEqual(len(rows), 2)
        flat = {cell for row in rows for cell in row}
        for project in data["projects"]:
            self.assertIn(project["id"], flat)
            self.assertIn(str(project["lint"]["findings"]), flat)
            self.assertIn(project["plans"][0]["next"], flat)
        self.assertNotIn("script", table.tags)

    def test_a9_unknown_schema_writes_nothing(self):
        state = self.tmp / "s.json"
        state.write_text(json.dumps({"schema_version": 2, "projects": []}))
        out = self.tmp / "o" / "x.html"
        out.parent.mkdir()
        done = self.render(state, out)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(list(out.parent.iterdir()), [])

    def test_a9_viewer_is_standalone(self):
        source = VIEWER.read_text()
        for name in ("state_export", "steward"):
            self.assertNotIn(f"import {name}", source)
            self.assertNotIn(f"from {name}", source)

    def listing(self, *args, path=None):
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = str(path)
        done = self.run_py(VIEWER, "list", "--format", "json", *args, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return {v["name"]: v for v in json.loads(done.stdout)["viewers"]}

    def manifest(self, command="fancy-viewer", **extra):
        path = self.tmp / "m.json"
        path.write_text(json.dumps({"name": "fancy", "command": command, **extra}))
        return path

    def test_a10_unavailable_is_explicit(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        listed = self.listing()
        self.assertEqual(listed["static-html"]["status"], "available")
        self.assertEqual(listed["companion"]["status"], "unavailable")
        self.assertIn("no companion manifest given (--companion FILE)", json.dumps(listed["companion"]))
        self.assertIn(REASON, json.dumps(listed["companion"]))

        absent = self.listing("--companion", self.manifest(), path=bin_dir)
        self.assertIn("unavailable: fancy-viewer not found on PATH", json.dumps(absent["companion"]))

        stub = bin_dir / "fancy-viewer"
        stub.write_text(f"#!/bin/sh\necho ran >> '{self.tmp / STUB_LOG}'\n")
        stub.chmod(0o755)
        present = self.listing("--companion", self.manifest(), path=bin_dir)
        self.assertEqual(present["companion"]["status"], "declared-unexercised")
        self.assertFalse((self.tmp / STUB_LOG).exists())
        state = self.two_projects()
        out = self.tmp / "h.html"
        self.assertEqual(self.render(state, out).returncode, 0)
        for suffix in (".png", ".jpg", ".svg"):
            self.assertEqual(list(self.tmp.rglob(f"*{suffix}")), [])
        self.assertFalse((self.tmp / STUB_LOG).exists())

    def test_a10_stale_metadata_unchanged(self):
        self.assertEqual(sha(ROOT / "modules/contracts/mapping.json"), MAPPING_SHA)
        self.assertEqual(sha(ROOT / "capabilities/registry.json"), REGISTRY_SHA)

    def test_f3_manifest_validation(self):
        bad = {
            "slash": json.dumps({"name": "n", "command": "/bin/sh"}),
            "missing": json.dumps({"name": "n"}),
            "dup": '{"name": "a", "name": "b", "command": "c"}',
            "extra": json.dumps({"name": "n", "command": "c", "x": 1}),
        }
        for label, text in bad.items():
            path = self.tmp / f"{label}.json"
            path.write_text(text)
            done = self.run_py(VIEWER, "list", "--companion", path)
            self.assertEqual(done.returncode, 2, label)


class DescriptorAndPrivacyTests(unittest.TestCase):
    def test_a11_descriptor_valid(self):
        spec = importlib.util.spec_from_file_location("contracts_u14b", ROOT / "modules/contracts/contracts.py")
        contracts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contracts)
        contracts.check_module_descriptor(ROOT / "modules/state-adapters", module="state-adapters")
        data = json.loads((ROOT / "modules/state-adapters/descriptor.json").read_text())
        self.assertEqual(data["claimed_support"], "planned")
        self.assertEqual(data["schemas"], ["status"])

    def test_a12_privacy_clean(self):
        spec = importlib.util.spec_from_file_location("privacy_u14b", ROOT / "scripts/privacy_scan.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.scan(ROOT / "modules/state-adapters"), [])
        self.assertEqual(mod.scan(ROOT / "modules/stewardship"), [])


if __name__ == "__main__":
    unittest.main()
