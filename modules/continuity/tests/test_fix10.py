"""R9 U04c fix round 3: a NEW settings*.json is denied through a symlinked `.claude` dir (target resolved, tail appended)."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_freeze import FreezeCase  # noqa: E402


class NewSettingsThroughSymlink(FreezeCase):
    def test_a_new_settings_through_claude_symlink(self):
        self.freeze("sess-a")
        (self.root / "conf").mkdir()
        os.symlink(self.root / "conf", self.root / ".claude")
        self.denied(self.call("sess-a", "Write", self.root / ".claude" / "settings.local.json"))

    def test_b_new_settings_in_symlink_target_dir(self):
        self.freeze("sess-a")
        (self.root / "conf").mkdir()
        os.symlink(self.root / "conf", self.root / ".claude")
        self.denied(self.call("sess-a", "Write", self.root / "conf" / "settings.json"))

    def test_c_nested_claude_symlink(self):
        self.freeze("sess-a")
        (self.root / "sub").mkdir()
        (self.root / "x").mkdir()
        os.symlink(self.root / "x", self.root / "sub" / ".claude")
        self.denied(self.call("sess-a", "Write", self.root / "x" / "Settings.JSON"))

    def test_controls_allow(self):
        self.freeze("sess-a")
        (self.root / "conf").mkdir()
        os.symlink(self.root / "conf", self.root / ".claude")
        (self.root / "conf" / "agents").mkdir()
        for p in (self.root / "f.txt", self.root / ".claude" / "agents" / "x.md", self.root / "conf" / "other.json"):
            self.assertIsNone(self.call("sess-a", "Write", p), p)


if __name__ == "__main__":
    unittest.main()
