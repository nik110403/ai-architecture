import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import CLI, CONT, L  # noqa: E402

HOOK = CONT / "hooks" / "freeze_guard.py"


class FreezeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.fdir = self.dir / "freeze"
        self.root = self.dir / "root"
        self.root.mkdir()
        self.outside = self.dir / "outside"
        self.outside.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        self.env["AI_ARCH_FREEZE_DIR"] = str(self.fdir)

    def cli(self, *args, env=None):
        proc = subprocess.run([sys.executable, "-B", str(CLI), "freeze", *args], capture_output=True, text=True,
                              env=env or self.env, cwd=self.dir)
        return proc.returncode, json.loads(proc.stdout)

    def hook(self, payload, env=None):
        raw = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
        proc = subprocess.run([sys.executable, "-B", str(HOOK)], input=raw, capture_output=True,
                              text=isinstance(raw, str), env=env or self.env, cwd=self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.decode() if isinstance(proc.stdout, bytes) else proc.stdout
        return json.loads(out) if out.strip() else None

    def call(self, session, tool, path=None, **extra):
        tool_input = {} if path is None else {("notebook_path" if tool == "NotebookEdit" else "file_path"): str(path)}
        return self.hook({"session_id": session, "tool_name": tool, "tool_input": tool_input, **extra})

    def denied(self, out):
        self.assertIsNotNone(out)
        spec = out["hookSpecificOutput"]
        self.assertEqual((spec["hookEventName"], spec["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertTrue(spec["permissionDecisionReason"])

    def freeze(self, session="sess-a", *extra):
        code, out = self.cli("set", "--session", session, "--root", str(self.root), *extra)
        self.assertEqual(code, 0, out)
        return out


class FreezeTargetsTheSession(FreezeCase):
    def test_guard_denies_only_the_frozen_session(self):
        record = self.freeze("sess-a")
        path = Path(record["record"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.fdir.stat().st_mode & 0o777, 0o700)
        stored = json.loads(path.read_text())
        self.assertEqual((stored["harness"], stored["session_id"], stored["root"], stored["bash"]),
                         ("claude-code", "sess-a", str(self.root), "deny"))
        self.assertRegex(path.name, r"^claude-code-[0-9a-f]{32}\.json$")
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.denied(self.call("sess-a", tool, self.outside / "x.txt"))
            self.assertIsNone(self.call("sess-a", tool, self.root / "x.txt"), tool)
            self.assertIsNone(self.call("sess-b", tool, self.outside / "x.txt"), "another session is not frozen")
        self.denied(self.call("sess-a", "Bash"))
        self.assertIsNone(self.call("sess-b", "Bash"))

    def test_missing_unresolvable_and_traversing_paths_deny(self):
        self.freeze("sess-a")
        self.denied(self.call("sess-a", "Write"))  # no path field
        self.denied(self.call("sess-a", "Write", "relative/x.txt"))  # relative, no cwd
        self.denied(self.call("sess-a", "Write", "bad\x00path"))
        self.denied(self.call("sess-a", "Write", self.root / ".." / "outside" / "x"))
        self.assertIsNone(self.call("sess-a", "Write", "inner.txt", cwd=str(self.root)))

    def test_symlink_inside_root_pointing_outside_is_denied(self):
        self.freeze("sess-a")
        (self.root / "link").symlink_to(self.outside)
        self.denied(self.call("sess-a", "Write", self.root / "link" / "x.txt"))
        (self.outside / "t.txt").write_text("t")
        (self.root / "flink").symlink_to(self.outside / "t.txt")
        self.denied(self.call("sess-a", "Edit", self.root / "flink"))

    def test_clear_lifts_only_that_session(self):
        self.freeze("sess-a")
        self.freeze("sess-b")
        code, out = self.cli("clear", "--session", "sess-a")
        self.assertEqual((code, out["cleared"]), (0, True))
        self.assertIsNone(self.call("sess-a", "Write", self.outside / "x"))
        self.denied(self.call("sess-b", "Write", self.outside / "x"))
        self.assertEqual(self.cli("clear", "--session", "sess-a")[1]["cleared"], False)

    def test_set_without_any_session_id_is_refused(self):
        code, out = self.cli("set", "--root", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("no session id", out["error"])
        self.assertFalse(self.fdir.exists() and any(self.fdir.iterdir()), "no global freeze was written")

    def test_session_id_comes_from_the_claude_environment(self):
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": "from-env"}
        code, out = self.cli("set", "--root", str(self.root), env=env)
        self.assertEqual((code, out["session_id"], out["harness"]), (0, "from-env", "claude-code"))
        self.denied(self.call("from-env", "Bash"))
        self.assertIsNone(self.call("other", "Bash"))

    def test_other_harness_is_refused(self):
        code, out = self.cli("set", "--session", "s", "--harness", "other", "--root", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("no enforcement adapter", out["error"])

    def test_opencode_record_without_session_is_refused_and_with_one_is_labelled(self):
        code, _ = self.cli("set", "--harness", "opencode", "--root", str(self.root),
                           env={**self.env, "CLAUDE_CODE_SESSION_ID": "ignored"})
        self.assertEqual(code, 2)
        code, out = self.cli("set", "--harness", "opencode", "--session", "oc-session-1", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        code, out = self.cli("status", "--harness", "opencode", "--session", "oc-session-1")
        self.assertTrue(out["frozen"])
        self.assertIn("unverified id", out["session"])
        self.assertIn("NOT frozen", out["session"])
        self.assertIn("opencode/runtime plugin", out["enforcement"])
        # the Claude Code guard never reads an OpenCode record
        self.assertIsNone(self.call("oc-session-1", "Bash"))
        code, out = self.cli("check", "--harness", "opencode", "--session", "oc-session-1", "--tool", "apply_patch",
                             "--path", str(self.outside / "x"))
        self.assertEqual(out["decision"], "deny")
        code, out = self.cli("check", "--harness", "opencode", "--session", "oc-session-1", "--tool", "write",
                             "--path", str(self.root / "x"))
        self.assertEqual(out["decision"], "allow")

    def test_invalid_record_file_denies(self):
        record = Path(self.freeze("sess-a")["record"])
        for bad in ("{not json", json.dumps({"version": 1}), "[]",
                    json.dumps({**json.loads(record.read_text()), "bash": "maybe"}),
                    json.dumps({**json.loads(record.read_text()), "session_id": "sess-other"}),
                    json.dumps({**json.loads(record.read_text()), "extra": 1})):
            record.write_text(bad)
            self.denied(self.call("sess-a", "Write", self.root / "x"))
            self.assertIsNone(self.call("sess-b", "Write", self.outside / "x"))
        code, out = self.cli("check", "--session", "sess-a", "--tool", "Write", "--path", str(self.root / "x"))
        self.assertEqual(out["decision"], "deny")

    def test_unparseable_payload_with_and_without_a_record(self):
        self.assertIsNone(self.hook("not json at all"), "no freeze directory: allow")
        self.fdir.mkdir()
        os.chmod(self.fdir, 0o700)
        self.assertIsNone(self.hook("not json at all"), "an empty freeze directory: allow")
        self.freeze("sess-a")
        self.denied(self.hook("not json at all"))
        self.denied(self.hook(b"\xff\xfe"))
        self.denied(self.hook(json.dumps({"tool_name": "Write"})))  # parseable but names no session
        self.denied(self.hook(json.dumps([1, 2])))

    def test_bash_unscoped_allows_bash_and_status_says_so(self):
        out = self.freeze("sess-a", "--bash", "unscoped")
        self.assertIn("Bash unscoped: not enforced", out["enforcement"])
        self.assertIsNone(self.call("sess-a", "Bash"))
        self.denied(self.call("sess-a", "Write", self.outside / "x"))
        code, out = self.cli("status", "--session", "sess-a")
        self.assertIn("Bash unscoped: not enforced", out["enforcement"])
        self.assertIn("freeze_guard.py is installed as a PreToolUse hook", out["enforcement"])

    def test_status_lists_records_and_flags_invalid_ones(self):
        self.freeze("sess-a")
        (self.fdir / "junk.json").write_text("{")
        code, out = self.cli("status")
        self.assertEqual((code, len(out["records"]), out["invalid"]), (0, 1, ["junk.json"]))
        self.assertIn("Edit, Write, MultiEdit, NotebookEdit outside root denied; Bash denied", out["records"][0]["enforcement"])

    def test_freeze_dir_resolution_order(self):
        from modules.continuity import freeze
        keep = {k: os.environ.get(k) for k in ("AI_ARCH_FREEZE_DIR", "XDG_STATE_HOME", "HOME")}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None)
                                 for k, v in keep.items()])
        os.environ["AI_ARCH_FREEZE_DIR"] = "/x/explicit"
        os.environ["XDG_STATE_HOME"] = "/x/xdg"
        self.assertEqual(freeze.freeze_dir(), Path("/x/explicit"))
        del os.environ["AI_ARCH_FREEZE_DIR"]
        self.assertEqual(freeze.freeze_dir(), Path("/x/xdg/ai-architecture/freeze"))
        del os.environ["XDG_STATE_HOME"]
        os.environ["HOME"] = "/x/homedir"
        self.assertEqual(freeze.freeze_dir(), Path("/x/homedir/.local/state/ai-architecture/freeze"))
        os.environ["AI_ARCH_FREEZE_DIR"] = "relative"
        with self.assertRaises(L.Refused):
            freeze.freeze_dir()

    def test_group_writable_freeze_directory_is_refused(self):
        self.fdir.mkdir()
        os.chmod(self.fdir, 0o777)
        code, out = self.cli("set", "--session", "s", "--root", str(self.root))
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
