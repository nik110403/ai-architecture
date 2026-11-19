"""R9-U06: curated lexical retrieval (A1-A6). Real subprocesses, temp dirs, temp HOME, no network, no model."""

import ast
import contextlib
import datetime
import errno
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MEM = ROOT / "modules" / "memory"
CURATED = MEM / "curated.py"
HOOK = MEM / "hooks" / "session_start.py"
EXAMPLE = MEM / "example" / "root"
FORBIDDEN = re.compile(r"(?i)vector|embedding|semantic search")
HOME_CANARY = "HOMECANARY9"
OUTSIDE = "OUTSIDE7Q"


def note(path, name, body, desc="a note", mtype="project"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: {mtype}\n---\n{body}\n", encoding="utf-8")


def index(memdir, entries):
    """entries: list of (file, title, hook)."""
    memdir.mkdir(parents=True, exist_ok=True)
    (memdir / "MEMORY.md").write_text("".join(f"- [{t}]({f}) — {h}\n" for f, t, h in entries), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(os.path.realpath(self._td.name))
        self.home = self.tmp / "home"
        note(self.home / ".claude" / "projects" / "p" / "memory" / "c.md", "canary", HOME_CANARY)
        self.outputs = []

    def tearDown(self):
        for text in self.outputs:
            self.assertNotIn(str(self.tmp), text)

    def env(self, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "LC_ALL": "C.UTF-8", "AI_MEMORY_ROOT": str(self.home)}
        env.update(extra)
        return env

    def run_py(self, script, *args, stdin="", env=None, cwd=None):
        proc = subprocess.run([sys.executable, "-B", str(script), *args], input=stdin, text=True,
                              capture_output=True, env=env if env is not None else self.env(), cwd=cwd or self.tmp)
        self.outputs += [proc.stdout, proc.stderr]
        self.assertNotIn(HOME_CANARY, proc.stdout + proc.stderr)
        return proc

    def cli(self, *args, **kw):
        return self.run_py(CURATED, *args, **kw)

    def query(self, roots, *args, **kw):
        flags = [x for label, path in roots for x in ("--root", f"{label}={path}")]
        proc = self.cli("query", *flags, *args, **kw)
        return proc, (json.loads(proc.stdout) if proc.returncode == 0 and "text" not in args else None)

    def mkroot(self, name="r"):
        path = self.tmp / name
        path.mkdir()
        return path


class Retrieval(Base):
    def test_a1_fresh_recipient(self):
        root = self.tmp / "T"
        shutil.copytree(EXAMPLE, root)
        proc, out = self.query([("ex", root)], "--q", "sourdough starter feeding")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["retrieval"], "lexical")
        self.assertFalse(out["abstained"])
        terms = {"sourdough", "starter", "feeding"}
        targets = [h for h in out["hits"] if h["citation"].startswith("ex:demo/memory/sourdough-starter.md#L")]
        self.assertTrue(targets)
        self.assertTrue(targets[0]["snippet"].strip())
        for hit in out["hits"]:
            label, rest = hit["citation"].split(":", 1)
            file, line = rest.rsplit("#L", 1)
            text = (root / file).read_text(encoding="utf-8").split("\n")[int(line) - 1].lower()
            self.assertTrue(terms & set(re.findall(r"[a-z0-9]+", text)), hit["citation"])
        helptext = self.cli("--help").stdout + self.cli("query", "--help").stdout + self.cli("inject", "--help").stdout
        self.assertIn("lexical", helptext)
        self.assertIsNone(FORBIDDEN.search(helptext))
        self.assertIsNone(FORBIDDEN.search(proc.stdout))

    def corpus(self):
        root = self.mkroot()
        note(root / "demo" / "memory" / "a.md", "a", "alphaterm betaterm gammaterm")
        index(root / "demo" / "memory", [("a.md", "A", "alphaterm")])
        return root

    def test_a2_abstention(self):
        root = self.corpus()
        proc, out = self.query([("x", root)], "--q", "zzqx plugh")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(out["abstained"])
        self.assertEqual(out["hits"], [])
        proc, _ = self.query([("x", root)], "--q", "zzqx plugh", "--format", "text")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        self.assertTrue(proc.stdout.startswith("NO MATCH"))
        proc, out = self.query([("x", root)], "--q", "alphaterm zzqx plugh")
        self.assertTrue(out["abstained"])
        self.assertEqual(out["hits"], [])
        proc, out = self.query([("x", root)], "--q", "alphaterm zzqx")  # 1 of 2 meets ceil(0.5 * 2)
        self.assertFalse(out["abstained"])
        self.assertEqual(len(out["hits"]), 1)

    def test_a3_budget(self):
        root = self.mkroot()
        for i in range(30):
            note(root / "demo" / "memory" / f"n{i:02}.md", f"n{i:02}", "budgetterm " * (1 + i % 7) + f"filler words number {i}")
        index(root / "demo" / "memory", [(f"n{i:02}.md", f"N{i:02}", "budgetterm") for i in range(30)])
        proc, out = self.query([("x", root)], "--q", "budgetterm", "--max-tokens", "200")
        self.assertEqual(proc.returncode, 0)
        total = sum(len(h["citation"].encode()) + len(h["snippet"].encode()) for h in out["hits"])
        self.assertTrue(out["hits"])
        self.assertLessEqual(total, 800)
        self.assertEqual(out["budget_used_tokens"], math.ceil(total / 4))
        self.assertEqual(out["token_estimate"], "utf8_bytes/4")
        self.assertEqual(out["omitted"], 30 - len(out["hits"]))
        self.assertGreater(out["omitted"], 0)
        scores = [h["score"] for h in out["hits"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for bad in ("--max-tokens=0", "--max-tokens=-5"):
            proc, _ = self.query([("x", root)], "--q", "budgetterm", bad)
            self.assertEqual((proc.returncode, proc.stdout), (2, ""))

    def test_a4_explicit_roots_and_ids(self):
        roots = {}
        for label in "abc":
            roots[label] = self.mkroot(label)
            note(roots[label] / "demo" / "memory" / "n1.md", "n1", f"canaryzz marker{label.upper()}")
            index(roots[label] / "demo" / "memory", [("n1.md", "N1", "canaryzz")])
        proc, out = self.query([("a", roots["a"]), ("b", roots["b"])], "--q", "canaryzz")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(sorted(h["citation"].split(":")[0] for h in out["hits"]), ["a", "b"])
        self.assertNotIn("markerC", proc.stdout)
        proc, out = self.query([("a", roots["a"]), ("b", roots["b"])], "--id", "a:demo/memory/n1.md")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(out["hits"]), 1)
        self.assertTrue(out["hits"][0]["citation"].startswith("a:demo/memory/n1.md#L"))
        for bad in ("a:../x", "a:demo/memory/../../c/x.md", f"a:{roots['c']}/demo/memory/n1.md", "z:demo/memory/n1.md",
                    "a:/etc/passwd", "a:demo/memory/n1.md/../n1.md"):
            proc, _ = self.query([("a", roots["a"]), ("b", roots["b"])], "--id", bad)
            self.assertEqual((proc.returncode, proc.stdout), (2, ""), bad)
        proc = self.cli("query", "--q", "canaryzz")
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        proc = self.cli("inject", "--scope", "a:demo")
        self.assertEqual(proc.returncode, 2)

    def outside_root(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text(f"---\nname: secret\ndescription: s\n---\nescape {OUTSIDE}\n")
        (outside / "outside.md").write_text(f"escape {OUTSIDE}\n")
        note(outside / "evil" / "memory" / "x.md", "x", f"escape {OUTSIDE}")
        note(outside / "mem" / "y.md", "y", f"escape {OUTSIDE}")
        root = self.mkroot("rootx")
        mem = root / "demo" / "memory"
        note(mem / "real.md", "real", "escape plan for the demo")
        index(mem, [("real.md", "Real", "escape plan"), ("../../../outside.md", "Escape", "escape out")])
        os.symlink(outside / "secret.md", mem / "link.md")
        os.symlink(outside / "evil", root / "evil")
        (root / "s3").mkdir()
        os.symlink(outside / "mem", root / "s3" / "memory")
        return root, outside

    def test_a5_no_outside_root_read(self):
        root, outside = self.outside_root()
        proc, out = self.query([("x", root)], "--q", "escape demo plan")
        self.assertEqual(proc.returncode, 0)
        reasons = {s["path"]: s["reason"] for s in out["skipped"]}
        for path in ("x:demo/memory/link.md", "x:evil", "x:s3/memory"):
            self.assertEqual(reasons.get(path), "symlink", reasons)
        self.assertTrue(any(h["citation"].startswith("x:demo/memory/real.md") for h in out["hits"]))
        for args in (["inject", "--scope", "x:demo"], ["inject", "--scope", "x:evil"], ["audit"]):
            proc = self.cli(args[0], "--root", f"x={root}", *args[1:])
            self.assertNotIn(OUTSIDE, proc.stdout + proc.stderr)
        link = self.tmp / "rootlink"
        os.symlink(root, link)
        for cmd in (["query", "--q", "escape"], ["inject", "--scope", "x:demo"], ["audit"]):
            self.assertEqual(self.cli(cmd[0], "--root", f"x={link}", *cmd[1:]).returncode, 2)
        self.assertEqual(self.cli("query", "--root", f"x={self.tmp / 'missing'}", "--q", "escape").returncode, 2)

    def test_a5_audit_hook_in_process(self):
        root, outside = self.outside_root()
        sys.path.insert(0, str(MEM))
        try:
            import curated
        finally:
            sys.path.remove(str(MEM))
        real = os.path.realpath(root)
        seen = []
        state = {"on": False}

        def hook(event, args):
            if not state["on"] or event not in ("open", "os.listdir", "os.scandir"):
                return
            target = args[0]
            if isinstance(target, bytes):
                target = os.fsdecode(target)
            if isinstance(target, str) and os.path.isabs(target):
                seen.append((event, target))

        sys.addaudithook(hook)
        buf = io.StringIO()
        for argv in (["query", "--root", f"x={root}", "--q", "escape plan"], ["inject", "--root", f"x={root}", "--scope", "x:demo"]):
            with contextlib.redirect_stdout(buf):
                state["on"] = True
                try:
                    code = curated.main(argv)
                finally:
                    state["on"] = False
            self.assertEqual(code, 0)
        self.assertNotIn(OUTSIDE, buf.getvalue())
        for event, target in seen:
            inside = os.path.realpath(target) == real or os.path.realpath(target).startswith(real + os.sep)
            self.assertTrue(inside, (event, target))

    def test_a5_static_ast(self):
        tree = ast.parse(CURATED.read_text())
        allowed = {"_open_nofollow", "_open_root", "_open_write"}
        found = []
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
                f = call.func
                if isinstance(f, ast.Attribute) and f.attr == "open" and isinstance(f.value, ast.Name) and f.value.id == "os":
                    found.append(func.name)
        # nested defs are walked by their parents too; every os.open must sit inside an allowed helper
        self.assertTrue(found)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name not in allowed:
                for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
                    f = call.func
                    self.assertFalse(isinstance(f, ast.Attribute) and f.attr == "open" and isinstance(f.value, ast.Name)
                                     and f.value.id == "os", f"os.open in {node.name}")
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        for call in calls:
            f = call.func
            self.assertFalse(isinstance(f, ast.Name) and f.id == "open", "builtin open")
            self.assertFalse(isinstance(f, ast.Attribute) and f.attr in ("read_text", "read_bytes", "write_text", "write_bytes"))
            self.assertFalse(isinstance(f, ast.Attribute) and f.attr == "open" and not (isinstance(f.value, ast.Name) and f.value.id == "os"))
        self.assertIn("_open_nofollow", {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)})

    def test_a5b_open_nofollow_each_component(self):
        sys.path.insert(0, str(MEM))
        try:
            import curated
        finally:
            sys.path.remove(str(MEM))
        real = self.tmp / "tree"
        (real / "a" / "b").mkdir(parents=True)
        (real / "a" / "b" / "c.md").write_text("hello")
        rootfd = os.open(real, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, rootfd)
        fd = curated._open_nofollow(rootfd, ["a", "b", "c.md"])
        self.assertEqual(os.read(fd, 10), b"hello")
        os.close(fd)
        for position in range(3):
            other = self.tmp / f"alt{position}"
            shutil.copytree(real, other)
            target = [other / "a", other / "a" / "b", other / "a" / "b" / "c.md"][position]
            moved = self.tmp / f"moved{position}"
            shutil.move(str(target), moved)
            os.symlink(moved, target)
            fd0 = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd0)
            with self.assertRaises(OSError) as ctx:
                curated._open_nofollow(fd0, ["a", "b", "c.md"])
            self.assertIn(ctx.exception.errno, (errno.ELOOP, errno.ENOTDIR))

    def a6_root(self):
        root = self.mkroot("r6")
        today = datetime.date.today().isoformat()
        specs = [
            ("s1", "old.md", "oldnote", "polterm " * 2 + "decided 2020-01-15"),
            ("s1", "dep.md", "deploy-policy", "polterm " * 9 + "rolling deploys"),
            ("s2", "dep.md", "deploy-policy", "polterm " * 6 + "blue green deploys"),
            ("s3", "twin1.md", "twin", "polterm identical twin body"),
            ("s3", "twin2.md", "twin", "polterm identical twin body"),
            ("s3", "fresh.md", "fresh", "polterm " * 3 + f"checked {today}"),
            ("s3", "solo.md", "solo", "polterm solo note nothing else"),
        ]
        for slug, file, name, body in specs:
            note(root / slug / "memory" / file, name, body)
        for slug in ("s1", "s2", "s3"):
            index(root / slug / "memory", [(f, name.title(), "polterm") for s, f, name, _ in specs if s == slug])
        return root

    def test_a6_stale_and_conflict_visible(self):
        root = self.a6_root()
        proc, out = self.query([("x", root)], "--q", "polterm")
        by = {h["citation"].split("#")[0]: h for h in out["hits"]}
        self.assertEqual(len(by), 7)
        self.assertIn("stale", by["x:s1/memory/old.md"]["flags"])
        self.assertIn("conflict", by["x:s1/memory/dep.md"]["flags"])
        self.assertIn("conflict", by["x:s2/memory/dep.md"]["flags"])
        self.assertEqual(by["x:s1/memory/dep.md"]["conflicts"], ["x:s2/memory/dep.md"])
        self.assertEqual(by["x:s2/memory/dep.md"]["conflicts"], ["x:s1/memory/dep.md"])
        # negative controls
        self.assertNotIn("stale", by["x:s3/memory/fresh.md"]["flags"])
        for twin in ("x:s3/memory/twin1.md", "x:s3/memory/twin2.md", "x:s3/memory/solo.md", "x:s3/memory/fresh.md"):
            self.assertNotIn("conflict", by[twin]["flags"], twin)
            self.assertEqual(by[twin]["conflicts"], [], twin)
        self.assertEqual(sum("stale" in h["flags"] for h in out["hits"]), 1)
        # budget cuts the partner but the conflict stays listed
        sizes = [len(h["citation"].encode()) + len(h["snippet"].encode()) for h in out["hits"]]
        tokens = math.ceil(sizes[0] / 4)
        self.assertLess(4 * tokens, sizes[0] + sizes[1])
        proc, cut = self.query([("x", root)], "--q", "polterm", "--max-tokens", str(tokens))
        self.assertEqual(len(cut["hits"]), 1)
        self.assertEqual(cut["hits"][0]["citation"].split("#")[0], "x:s1/memory/dep.md")
        self.assertEqual(cut["hits"][0]["conflicts"], ["x:s2/memory/dep.md"])
        proc = self.cli("inject", "--root", f"x={root}", "--scope", "x:s1")
        self.assertIn("[stale]", proc.stdout)
        self.assertIn("[conflict]", proc.stdout)
        proc, out = self.query([("x", root)], "--q", "polterm", "--stale-before", "2019-01-01")
        self.assertEqual(sum("stale" in h["flags"] for h in out["hits"]), 0)
        self.assertEqual(self.query([("x", root)], "--q", "polterm", "--stale-before", "nope")[0].returncode, 2)


if __name__ == "__main__":
    unittest.main()
