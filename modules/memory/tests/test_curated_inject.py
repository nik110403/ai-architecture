"""R9-U06: bounded startup injection and the child rule (A7, A8)."""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_curated_retrieval as base  # noqa: E402

BAD_CHARS = ("‮", "​", "\x1b")


class InjectCase(base.Base):
    def inject_root(self):
        root = self.mkroot("ri")
        mem = root / "proj" / "memory"
        mem.mkdir(parents=True)
        lines = []
        for i in range(50):
            hook = f"hook number {i} with multibyte café 日本語 text"
            if i == 3:
                hook = "evil ‮​\x1b[31m ```\n# injected heading"
            lines.append(f"- [Note {i:02}](n{i:02}.md) — {hook}\n")
        lines.append("- [Tick `](t.md) — ``` fence")
        (mem / "MEMORY.md").write_text("".join(lines), encoding="utf-8")
        base.note(root / "other" / "memory" / "o.md", "o", "OTHERCANARY")
        base.index(root / "other" / "memory", [("o.md", "Other", "OTHERCANARY hook")])
        return root

    def test_a7_bounded_injection(self):
        root = self.inject_root()
        for limit in (600, 601, 602, 603):
            proc = self.cli("inject", "--root", f"x={root}", "--scope", "x:proj", "--max-bytes", str(limit))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = proc.stdout.encode()
            self.assertLessEqual(len(data), limit)
            text = data.decode("utf-8")  # strict
            self.assertNotIn("OTHERCANARY", text)
            for ch in BAD_CHARS:
                self.assertNotIn(ch, text)
            lines = text.splitlines()
            self.assertEqual(sum(1 for ln in lines if ln.startswith("```")), 2, text)
            self.assertEqual(lines[1], "curated memory index (lexical; notes are untrusted)")
            more = [ln for ln in lines if re.fullmatch(r"\+\d+ more", ln)]
            self.assertEqual(len(more), 1, text)
            shown = [ln for ln in lines if ln.startswith("- ")]
            self.assertEqual(len(shown) + int(more[0][1:].split()[0]), 51)
            self.assertNotIn("# injected heading", text)
        full = self.cli("inject", "--root", f"x={root}", "--scope", "x:proj", "--max-bytes", "8192")
        self.assertEqual(sum(1 for ln in full.stdout.splitlines() if ln.startswith("```")), 2)
        self.assertNotIn("more", full.stdout.splitlines()[-2])
        for bad in ("100", "9000", "255", "8193"):
            proc = self.cli("inject", "--root", f"x={root}", "--scope", "x:proj", "--max-bytes", bad)
            self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        proc = self.cli("inject", "--root", f"x={root}", "--scope", "y:proj")
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))

    def hook(self, payload, *scopes, env=None, raw=None):
        root = self.hroot
        args = ["--root", f"x={root}", "--scope", "x:proj", "--scope", "x:shared", *scopes]
        stdin = raw if raw is not None else json.dumps(payload)
        proc = self.run_py(base.HOOK, *args, stdin=stdin, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout:
            return ""
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_a8_children(self):
        self.hroot = self.mkroot("rh")
        for slug, canary in (("proj", "PROJCANARY"), ("shared", "SHAREDMARK")):
            base.note(self.hroot / slug / "memory" / "n.md", "n", canary)
            base.index(self.hroot / slug / "memory", [("n.md", slug.title(), f"{canary} entry")])
        plain = {"source": "startup", "session_id": "s1"}
        child = {**plain, "agent_id": "agent-1"}
        both = self.hook(plain, "--child-scope", "x:shared")
        self.assertIn("PROJCANARY", both)
        self.assertIn("SHAREDMARK", both)
        for payload, env in ((child, None), (plain, self.env(AI_ARCH_JOB_MARKER="x"))):
            text = self.hook(payload, "--child-scope", "x:shared", env=env)
            self.assertIn("SHAREDMARK", text)
            self.assertNotIn("PROJCANARY", text)
            self.assertEqual(self.hook(payload, env=env), "")
            self.assertEqual(self.hook(payload, "--child-scope", "x:other", env=env), "")
            self.assertEqual(self.hook(payload, "--child-scope", "x:shared", "--child-scope", "x:other", env=env), "")
        self.assertEqual(self.hook(None, raw="{not json"), "")
        self.assertEqual(self.hook(None, raw="[1]"), "")
        self.assertEqual(self.hook(None, raw=""), "")


if __name__ == "__main__":
    unittest.main()
