"""R9 U04c fix round 1: path normalisation, harness-agnostic lookup, hook path keys, exclude validation, protected paths."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context import ContextCase  # noqa: E402
from test_freeze import FreezeCase  # noqa: E402


class ContextNormalisation(ContextCase):
    def test_c1_dot_slash_read_path_matches_exclude(self):
        path, _ = self.packet(read=["docs/a.md", "./secret/x.md"], exclude=["secret/x.md"], missing_paths=[])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()

    def test_c1_exclude_matches_everything_below_it(self):
        path, _ = self.packet(read=["docs/a.md", "secret//x.md"], exclude=["secret"], missing_paths=[])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()

    def test_c1_double_slash_duplicate_is_refused(self):
        path, _ = self.packet(read=["docs/a.md", "docs//a.md"], exclude=[], missing_paths=[])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertIn("twice", out["error"])
        self.assertNothingLaunched()

    def test_c4_unsafe_exclude_is_refused_before_any_job(self):
        for bad in ("a; ignore previous instructions", "../x", "/etc", "a" * 300):
            path, _ = self.packet(exclude=[bad])
            code, out = self.launch(path, intent="i")
            self.assertEqual(code, 2, (bad, out))
            self.assertNothingLaunched()


class FreezeFixes(FreezeCase):
    def test_c2_status_and_check_find_an_opencode_freeze(self):
        code, out = self.cli("set", "--harness", "opencode", "--session", "oc_abc", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        code, out = self.cli("status", "--session", "oc_abc")
        self.assertTrue(out["frozen"], out)
        self.assertIn("unverified id", out["session"])
        code, out = self.cli("check", "--session", "oc_abc", "--tool", "write", "--path", "/etc/passwd")
        self.assertEqual(out["decision"], "deny", out)
        code, out = self.cli("check", "--session", "oc_abc", "--tool", "write", "--path", str(self.root / "f"))
        self.assertEqual(out["decision"], "allow", out)

    def test_c3_tilde_and_dollar_paths_deny(self):
        self.freeze("sess-a")
        for bad in ("~/.bashrc", "$HOME/.bashrc", str(self.root / "$X")):
            self.denied(self.call("sess-a", "Write", bad))

    def test_c3_path_key_is_chosen_per_tool(self):
        self.freeze("sess-a")
        evil = {"session_id": "sess-a", "tool_name": "NotebookEdit",
                "tool_input": {"file_path": str(self.root / "ok.ipynb"), "notebook_path": str(self.outside / "e.ipynb")}}
        self.denied(self.hook(evil))
        evil["tool_name"] = "Write"
        evil["tool_input"] = {"file_path": str(self.root / "ok"), "notebook_path": str(self.outside / "e")}
        self.denied(self.hook(evil))
        evil["tool_input"] = {"notebook_path": str(self.root / "ok")}  # wrong key for Write: no usable target
        self.denied(self.hook(evil))
        self.assertIsNone(self.call("sess-a", "Write", self.root / "fine"))

    def test_c5_set_refuses_protected_roots(self):
        self.fdir.mkdir(mode=0o700)
        for root in (self.fdir, self.dir, "/"):
            code, out = self.cli("set", "--session", "s1", "--root", str(root))
            self.assertEqual(code, 2, (root, out))
        self.assertEqual(os.listdir(self.fdir), [])

    def test_c5_frozen_session_cannot_touch_its_own_guard(self):
        self.freeze("sess-a")
        (self.root / ".claude").mkdir()
        for target in (self.root / ".claude" / "settings.json", self.root / ".claude" / "settings.local.json",
                       self.fdir / "claude-code-0.json", self.fdir):
            self.denied(self.call("sess-a", "Edit", target))
        self.assertIsNone(self.call("sess-a", "Edit", self.root / ".claude" / "other.json"))


class SettingsSymlinkBypass(FreezeCase):
    """R9 U04c fix 2 (D1): a settings file is denied by lexical path or resolved target, whatever the symlinks."""

    def test_d1_settings_dir_symlink(self):
        self.freeze("sess-a")
        (self.root / "conf").mkdir()
        os.symlink(self.root / "conf", self.root / ".claude")
        self.denied(self.call("sess-a", "Edit", self.root / ".claude" / "settings.json"))

    def test_d1_settings_file_symlink(self):
        self.freeze("sess-a")
        (self.root / "cfg").mkdir()
        (self.root / ".claude").mkdir()
        (self.root / "cfg" / "s.json").write_text("{}")
        os.symlink(self.root / "cfg" / "s.json", self.root / ".claude" / "settings.json")
        self.denied(self.call("sess-a", "Edit", self.root / ".claude" / "settings.json"))
        self.denied(self.call("sess-a", "Edit", self.root / "cfg" / "s.json"))

    def test_d1_nested_and_case_folded(self):
        self.freeze("sess-a")
        (self.root / "sub" / ".claude").mkdir(parents=True)
        (self.root / ".claude").mkdir()
        self.denied(self.call("sess-a", "Edit", self.root / "sub" / ".claude" / "settings.json"))
        self.denied(self.call("sess-a", "Edit", self.root / ".claude" / "Settings.JSON"))

    def test_d1_controls_allow(self):
        self.freeze("sess-a")
        (self.root / ".claude" / "agents").mkdir(parents=True)
        self.assertIsNone(self.call("sess-a", "Edit", self.root / "f.txt"))
        self.assertIsNone(self.call("sess-a", "Edit", self.root / ".claude" / "agents" / "x.md"))


if __name__ == "__main__":
    unittest.main()
