"""R9 U04c fix round 4: both path readings, dangling `.claude` links, direct-child scope, pruned walk, layout corpus."""
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_freeze import FreezeCase  # noqa: E402
from helpers import CONT  # noqa: E402

sys.path.insert(0, str(CONT.parent.parent))
from modules.continuity import freeze  # noqa: E402

TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
NAMES = ("settings.json", "Settings.Local.JSON")
LAYOUTS = ("real", "link_exist", "link_missing", "chained", "chained_missing", "relative", "upper_link")
TIME_BOUND = 3.0


class Corpus(FreezeCase):
    def build(self, layout):
        """A fresh frozen root for `layout`; returns (root, name of the settings dir as reachable under root)."""
        root = self.dir / ("root_" + layout)
        root.mkdir()
        for sub in ("deep/d", "a/b"):
            (root / sub).mkdir(parents=True)
        os.symlink(root / "deep" / "d", root / "L")
        os.symlink(root / "a" / "b", root / "L2")
        t = ".claude"
        if layout == "link_exist":
            (root / "conf").mkdir()
            os.symlink(root / "conf", root / ".claude")
            t = "conf"
        elif layout == "link_missing":
            os.symlink(root / "conf", root / ".claude")
            t = "conf"
        elif layout == "chained":
            (root / "conf").mkdir()
            os.symlink(root / "conf", root / "c1")
            os.symlink(root / "c1", root / ".claude")
            t = "conf"
        elif layout == "chained_missing":
            os.symlink(root / "conf", root / "c1")
            os.symlink(root / "c1", root / ".claude")
            t = "conf"
        elif layout == "relative":
            os.symlink("conf", root / ".claude")
            t = "conf"
        elif layout == "upper_link":
            os.symlink(root / "conf", root / ".CLAUDE")
            t = "conf"
        else:
            (root / ".claude").mkdir()
        code, out = self.cli("set", "--session", layout, "--root", str(root))
        self.assertEqual(code, 0, out)
        return root, t

    def verdict(self, session, tool, path):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        payload = json.dumps({"session_id": session, "tool_name": tool, "tool_input": {key: str(path)}})
        return freeze.decide_payload(payload, str(self.fdir))[0]

    def test_settings_and_escape_cells_deny(self):
        for layout in LAYOUTS:
            root, t = self.build(layout)
            forms = {"direct": f"{root}/.claude/%s", "target": f"{root}/{t}/%s",
                     "via_L": f"{root}/L/../{t}/%s", "via_L2": f"{root}/L2/../../{t}/%s"}
            for form, tmpl in forms.items():
                for name in NAMES:
                    for tool in TOOLS:
                        with self.subTest(layout=layout, form=form, name=name, tool=tool):
                            self.assertFalse(self.verdict(layout, tool, tmpl % name), tmpl % name)
            for tool in TOOLS:
                with self.subTest(layout=layout, cell="escape", tool=tool):  # tribune F3
                    self.assertFalse(self.verdict(layout, tool, f"{root}/L2/../../outside/x.txt"))
                with self.subTest(layout=layout, cell="freeze dir via lexical", tool=tool):
                    self.assertFalse(self.verdict(layout, tool, f"{root}/L2/../../freeze/claude-code-0.json"))

    def test_controls_allow(self):
        for layout in LAYOUTS:
            root, t = self.build(layout)
            for rel in ("f.txt", ".claude/agents/x.md", ".claude/skills/x/settings.json", "conf/other.json",
                        "conf/bar/settings.local.json", "L/f.txt", "L2/../f.txt"):
                for tool in TOOLS:
                    with self.subTest(layout=layout, rel=rel, tool=tool):
                        self.assertTrue(self.verdict(layout, tool, f"{root}/{rel}"), rel)

    def test_named_repros(self):
        root, _ = self.build("link_missing")  # security SEV2 / tribune F1: R/.claude -> R/conf, conf absent
        self.denied(self.call("link_missing", "Write", root / "conf" / "settings.json"))
        root, _ = self.build("relative")
        self.denied(self.call("relative", "Write", root / "conf" / "settings.local.json"))
        root, _ = self.build("chained_missing")
        self.denied(self.call("chained_missing", "Write", root / "conf" / "settings.json"))
        root, _ = self.build("upper_link")  # tribune F6: upper-case `.CLAUDE` dangling link
        self.denied(self.call("upper_link", "Write", root / "conf" / "settings.json"))

    def test_f2_symlink_dotdot(self):  # R/.claude -> R/conf (absent), R/L -> R/deep/d
        root, _ = self.build("link_missing")
        self.denied(self.call("link_missing", "Write", f"{root}/L/../conf/settings.json"))

    def test_direct_child_scope(self):  # tribune F4 / security SEV3 false denies
        root, _ = self.build("real")
        for rel in (".claude/agents/settings.json", ".claude/worktrees/w/app/settings.local.json"):
            self.assertIsNone(self.call("real", "Write", root / rel), rel)


class Cost(FreezeCase):
    def test_pruned_walk_of_20k_dirs(self):
        self.freeze("sess-a")
        n = 0
        for top in ("node_modules", ".git", "__pycache__", ".venv"):  # 10k pruned dirs
            for i in range(50):
                for j in range(50):
                    os.makedirs(self.root / top / f"p{i}" / f"q{j}")
                    n += 1
        for i in range(100):  # 10k walked dirs
            for j in range(100):
                os.makedirs(self.root / "src" / f"p{i}" / f"q{j}")
                n += 1
        self.assertGreaterEqual(n, 20000)
        record = freeze.load_record(self.fdir, "claude-code", "sess-a")
        start = time.monotonic()
        allowed, _ = freeze.decide_tool(record, "Write", str(self.root / "src" / "f.txt"))
        elapsed = time.monotonic() - start
        print(f"[fix4 timing] {n} dirs, one decide_tool: {elapsed:.3f}s (bound {TIME_BOUND}s)", file=sys.stderr)
        self.assertTrue(allowed)
        self.assertLess(elapsed, TIME_BOUND)


if __name__ == "__main__":
    unittest.main()
