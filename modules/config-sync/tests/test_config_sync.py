import contextlib
import hashlib
import io
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MOD = Path(__file__).resolve().parents[1]
ROOT = MOD.parents[1]
CLI = MOD / "config_sync.py"
SYNC = ROOT / "opencode" / "agents" / "opencode-agents-sync"
SENTINEL = "SENTINEL-" + "SECRET-VALUE"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    root = Path(root)
    return sorted((p.relative_to(root).as_posix(), p.is_symlink() or (sha(p) if p.is_file() else "dir"),
                   p.stat().st_mtime_ns if p.is_file() else 0) for p in root.rglob("*"))


def load_module():
    spec = importlib.util.spec_from_file_location("config_sync_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mcp(entry_id="mcp-demo", harness="claude", name="demo", env=("DEMO_API_KEY",), args=("--stdio",), **extra):
    entry = {"id": entry_id, "kind": "mcp", "harness": harness, "name": name, "command": "demo-server",
             "args": list(args), "env": list(env) if isinstance(env, (tuple, list)) else env}
    entry.update(extra)
    return entry


def skill(text="v1\n", dest=".claude/skills/demo", extra=None):
    files = {"SKILL.md": text}
    files.update(extra or {})
    return {"id": "skill-demo", "kind": "tree", "dest": dest, "files": files}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.state = self.tmp / "state"
        self.home = self.tmp / "home1"
        self.home.mkdir()
        self.before_status = self.worktree_status()

    def tearDown(self):
        self.assertEqual(self.worktree_status(), self.before_status)

    def worktree_status(self):
        return subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True,
                              text=True).stdout

    def manifest(self, entries, version=1, name="m.json"):
        path = self.tmp / name
        path.write_text(json.dumps({"schema_version": 1, "manifest_version": version, "entries": entries}))
        return path

    def env(self):
        return {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "hh"), "XDG_STATE_HOME": str(self.state),
                "XDG_CONFIG_HOME": str(self.tmp / "cc")}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], capture_output=True, text=True,
                              env=self.env(), cwd=self.tmp, timeout=60)

    def apply(self, manifest, home=None, *extra):
        return self.cli("apply", "--manifest", manifest, "--home", home or self.home, "--apply", *extra)

    def status(self, manifest, home=None, *extra):
        return self.cli("status", "--manifest", manifest, "--home", home or self.home, *extra)

    def state_dir(self, home=None):
        return self.state / "aiarch" / "config-sync" / hashlib.sha256(
            os.path.realpath(home or self.home).encode()).hexdigest()

    def all_output_and_state(self, *results):
        text = "".join(r.stdout + r.stderr for r in results)
        if self.state.exists():
            for p in self.state.rglob("*"):
                if p.is_file():
                    text += p.read_text(errors="replace")
        return text


class TestA5Drift(Base):
    def test_clean_changed_broken(self):
        m = self.manifest([mcp(), skill()])
        self.assertEqual(self.status(m).returncode, 1)  # nothing applied yet: missing
        self.assertEqual(self.apply(m).returncode, 0)
        self.assertEqual(self.status(m).returncode, 0)
        target = self.home / ".mcp.json"
        data = json.loads(target.read_text())
        data["mcpServers"]["demo"]["command"] = "edited"
        target.write_text(json.dumps(data))
        r = self.status(m)
        self.assertEqual(r.returncode, 1)
        self.assertIn("mcpServers.demo", r.stdout)
        self.assertNotIn("edited", r.stdout)

    def test_broken_target(self):
        m = self.manifest([mcp()])
        (self.home / ".mcp.json").write_text("{not json")
        before = tree(self.home)
        r = self.status(m)
        self.assertEqual(r.returncode, 1)
        self.assertIn("broken", r.stdout)
        r = self.apply(m)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(tree(self.home), before)

    def test_three_homes_independent_state(self):
        m = self.manifest([mcp()])
        homes = []
        for i in range(3):
            h = self.tmp / f"h{i}"
            h.mkdir()
            homes.append(h)
        self.apply(m, homes[0])
        self.assertEqual(self.status(m, homes[0]).returncode, 0)
        self.assertEqual(self.status(m, homes[1]).returncode, 1)
        self.assertEqual(self.apply(m, homes[2]).returncode, 0)
        self.assertEqual(len(list((self.state / "aiarch" / "config-sync").iterdir())), 2)


class TestA6Preserve(Base):
    def test_user_keys_survive_and_edited_managed_key_conflicts(self):
        target = self.home / ".mcp.json"
        target.write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}, "other": 1}))
        m = self.manifest([mcp()])
        self.assertEqual(self.apply(m).returncode, 0)
        data = json.loads(target.read_text())
        self.assertEqual(data["other"], 1)
        self.assertIn("mine", data["mcpServers"])
        self.assertIn("demo", data["mcpServers"])
        data["mcpServers"]["demo"]["command"] = "user-edit"
        target.write_text(json.dumps(data))
        before = target.read_bytes()
        m2 = self.manifest([mcp(args=("--stdio", "--v2"))], version=2, name="m2.json")
        r = self.apply(m2)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("conflict", r.stdout)
        self.assertEqual(target.read_bytes(), before)

    def test_no_record_with_differing_value_is_conflict(self):
        target = self.home / ".mcp.json"
        target.write_text(json.dumps({"mcpServers": {"demo": {"command": "someone-else"}}}))
        before = target.read_bytes()
        r = self.apply(self.manifest([mcp()]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("conflict", r.stdout)
        self.assertEqual(target.read_bytes(), before)

    def test_backup_first(self):
        target = self.home / ".mcp.json"
        target.write_text(json.dumps({"mcpServers": {}}, indent=2))
        before = target.read_bytes()
        m = self.manifest([mcp()])
        module = load_module()
        with mock.patch.dict(os.environ, self.env()), \
                mock.patch.object(module, "_write_target", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = module.main(["apply", "--manifest", str(m), "--home", str(self.home), "--apply"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(target.read_bytes(), before)
        backups = [p for p in (self.state_dir() / "backups").rglob("*") if p.is_file() and p.name != "snapshot.json"]
        self.assertTrue(backups)
        self.assertTrue(any(p.read_bytes() == before for p in backups))

    def test_status_and_dry_run_write_nothing(self):
        m = self.manifest([mcp(), skill()])
        (self.home / "keep.txt").write_text("k")
        before = tree(self.home)
        self.status(m)
        r = self.cli("apply", "--manifest", m, "--home", self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mcpServers.demo", r.stdout)
        self.assertEqual(tree(self.home), before)
        self.assertFalse(self.state.exists())

    def test_permissions(self):
        self.apply(self.manifest([mcp()]))
        for p in self.state.rglob("*"):
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode, 0o700 if p.is_dir() else 0o600, p)


class TestA7Upgrade(Base):
    def test_upgrade_idempotent_rollback(self):
        original = json.dumps({"mcpServers": {"mine": {"command": "x"}}}, indent=2) + "\n"
        (self.home / ".mcp.json").write_text(original)
        m1 = self.manifest([mcp(), skill("v1\n")], 1, "m1.json")
        m2 = self.manifest([mcp(args=("--stdio", "--v2")), skill("v2\n", extra={"extra.md": "e\n"})], 2, "m2.json")
        self.assertEqual(self.apply(m1).returncode, 0)
        v1_json = (self.home / ".mcp.json").read_bytes()
        v1_skill = (self.home / ".claude/skills/demo/SKILL.md").read_bytes()
        self.assertEqual(self.apply(m2).returncode, 0)
        self.assertIn(b"--v2", (self.home / ".mcp.json").read_bytes())
        snapshot = tree(self.home)
        r = self.apply(m2)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(tree(self.home), snapshot)  # hashes and mtimes
        r = self.cli("rollback", "--home", self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(tree(self.home), snapshot)  # dry-run default
        self.assertEqual(self.cli("rollback", "--home", self.home, "--apply").returncode, 0)
        self.assertEqual((self.home / ".mcp.json").read_bytes(), v1_json)
        self.assertEqual((self.home / ".claude/skills/demo/SKILL.md").read_bytes(), v1_skill)
        self.assertFalse((self.home / ".claude/skills/demo/extra.md").exists())
        self.assertEqual(self.cli("rollback", "--home", self.home, "--apply").returncode, 0)
        self.assertEqual((self.home / ".mcp.json").read_text(), original)
        self.assertFalse((self.home / ".claude/skills/demo/SKILL.md").exists())
        self.assertEqual(self.cli("rollback", "--home", self.home, "--apply").returncode, 0)  # nothing left

    def test_rollback_refuses_after_user_edit(self):
        self.apply(self.manifest([mcp()]))
        target = self.home / ".mcp.json"
        target.write_text(target.read_text() + "\n")
        before = target.read_bytes()
        r = self.cli("rollback", "--home", self.home, "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(target.read_bytes(), before)

    def test_env_written_as_references(self):
        self.apply(self.manifest([mcp(), mcp("mcp-oc", "opencode")]))
        claude = json.loads((self.home / ".mcp.json").read_text())
        self.assertEqual(claude["mcpServers"]["demo"]["env"], {"DEMO_API_KEY": "${DEMO_API_KEY}"})
        oc = json.loads((self.home / "opencode.json").read_text())
        self.assertEqual(oc["mcp"]["demo"]["environment"], {"DEMO_API_KEY": "{env:DEMO_API_KEY}"})
        self.assertEqual(oc["mcp"]["demo"]["command"], ["demo-server", "--stdio"])


class TestA8Secrets(Base):
    def test_literal_values_refused(self):
        bad = {
            "kv": mcp(env=["K=v"]),
            "obj": mcp(env={"K": "v"}),
            "arg": mcp(args=["--api-key=abc"]),
            "arg2": mcp(args=["--Auth-Token=abc"]),
            "header": mcp(url="https://example.invalid/x", command=None, headers={"Authorization": "Bearer abc"}),
            "userinfo": mcp(url="https://user:pw@example.invalid/x", command=None),
            "query": mcp(url="https://example.invalid/x?token=abc", command=None),
        }
        for name, entry in bad.items():
            entry = {k: v for k, v in entry.items() if v is not None}
            m = self.manifest([entry], name=f"{name}.json")
            self.assertEqual(self.cli("validate", "--manifest", m).returncode, 2, name)
            before = tree(self.home)
            self.assertEqual(self.apply(m).returncode, 2, name)
            self.assertEqual(tree(self.home), before)
        good = mcp(url="https://example.invalid/x", headers={"Authorization": "EXAMPLE_TOKEN"})
        good.pop("command")
        self.assertEqual(self.cli("validate", "--manifest", self.manifest([good], name="good.json")).returncode, 0)

    def test_credentials_and_values_never_surface(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text(SENTINEL + "-cred")
        (self.home / ".claude" / "auth.json").write_text(SENTINEL + "-auth")
        (self.home / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"demo": {"command": "demo-server", "env": {"DEMO_API_KEY": SENTINEL + "-env"}}}}))
        m = self.manifest([mcp(), skill()])
        results = [self.status(m), self.apply(m)]
        self.assertEqual(results[0].returncode, 1)
        self.assertEqual(results[1].returncode, 1)
        self.assertNotIn(SENTINEL, self.all_output_and_state(*results))
        # also after a clean apply in another home with planted credentials
        h2 = self.tmp / "h2"
        (h2 / ".claude").mkdir(parents=True)
        (h2 / ".claude" / "auth.json").write_text(SENTINEL + "-auth2")
        results = [self.apply(m, h2), self.status(m, h2)]
        self.assertNotIn(SENTINEL, self.all_output_and_state(*results))

    def test_credential_file_names_refused_in_tree(self):
        m = self.manifest([skill(extra={"auth.json": "{}"})])
        self.assertEqual(self.cli("validate", "--manifest", m).returncode, 2)

    def test_reconcile(self):
        m1 = self.manifest([mcp(args=("--a",))], 1, "m1.json")
        m2 = self.manifest([mcp(args=("--b",))], 1, "m2.json")
        h1 = self.tmp / "ha"
        h2 = self.tmp / "hb"
        h1.mkdir()
        h2.mkdir()
        (h1 / "x").write_text("1")
        before = (tree(h1), tree(h2))
        out = self.tmp / "P.json"
        r = self.cli("reconcile", "--a", m1, "--b", m2, "--out", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("version-conflict", r.stdout)
        self.assertIn("mcp-demo", r.stdout)
        proposed = json.loads(out.read_text())
        self.assertEqual(proposed["entries"][0]["id"], "mcp-demo")
        self.assertTrue(proposed["reconcile"]["diff"])
        self.assertEqual(self.cli("validate", "--manifest", out).returncode, 0)
        self.assertEqual((tree(h1), tree(h2)), before)
        self.assertFalse(self.state.exists())
        # a different version resolves to the higher one with exit 0
        m3 = self.manifest([mcp(args=("--c",))], 2, "m3.json")
        r = self.cli("reconcile", "--a", m1, "--b", m3, "--out", self.tmp / "P2.json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((self.tmp / "P2.json").read_text())["manifest_version"], 2)


class TestContainment(Base):
    def test_escape_refused(self):
        for entry in (skill(dest="../escape"), skill(extra={"../../x": "y"}), skill(dest="/abs/path")):
            m = self.manifest([entry], name="esc.json")
            self.assertEqual(self.cli("validate", "--manifest", m).returncode, 2)
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "skills").symlink_to(outside)
        before = tree(outside)
        r = self.apply(self.manifest([skill()]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(tree(outside), before)
        (self.home / ".mcp.json").symlink_to(self.tmp / "elsewhere.json")
        r = self.apply(self.manifest([mcp()], name="m2.json"))
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.tmp / "elsewhere.json").exists())


class TestA9Agents(Base):
    def agents_dir(self, prompt="Do the thing."):
        d = self.tmp / "agents"
        d.mkdir(exist_ok=True)
        (d / "scout.md").write_text(f"---\nname: scout\ndescription: Scouts.\ntools: Read, Grep\n---\n{prompt}\n")
        return d

    def generated(self, d):
        r = subprocess.run([sys.executable, "-B", str(SYNC), "--agents-dir", str(d)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_drift_and_clean(self):
        d = self.agents_dir()
        m = self.manifest([])
        target = self.home / "opencode.json"
        data = self.generated(d)
        data["agent"]["scout"]["model"] = "someprovider/real-model"  # user-chosen model is ignored
        data["mcp"] = {"demo": {"type": "local", "command": ["x"]}}
        target.write_text(json.dumps(data, indent=2))
        r = self.status(m, None, "--agents-dir", d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("drift: opencode agents", r.stdout)
        self.agents_dir("Changed prompt.")
        r = self.status(m, None, "--agents-dir", d)
        self.assertEqual(r.returncode, 1)
        self.assertIn("drift: opencode agents", r.stdout)
        target.write_text("{broken")
        r = self.status(m, None, "--agents-dir", d)
        self.assertEqual(r.returncode, 1)


class TestA14(Base):
    def test_descriptor_and_privacy(self):
        def load(name, rel):
            spec = importlib.util.spec_from_file_location(name, ROOT / rel)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        contracts = load("contracts_u15c", "modules/contracts/contracts.py")
        contracts.check_module_descriptor(MOD, module="config-sync")
        self.assertEqual(json.loads((MOD / "descriptor.json").read_text())["claimed_support"], "planned")
        privacy = load("privacy_u15c", "scripts/privacy_scan.py")
        planted = self.tmp / "planted"
        planted.mkdir()
        (planted / "x.md").write_text("see " + "/" + "home/someone/file\n")
        self.assertTrue(privacy.scan(planted))
        shutil.copytree(MOD, self.tmp / "scan" / "modules" / "config-sync",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(privacy.scan(self.tmp / "scan"), [])


if __name__ == "__main__":
    unittest.main()
