"""R9-U06: guarded repair, undo, audit, descriptor and privacy (A9-A13, F1, F4)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import test_curated_retrieval as base  # noqa: E402

MEMCHECK = base.ROOT / "tools" / "memcheck" / "memcheck"
FIX = base.ROOT / "tools" / "memcheck" / "tests" / "fixtures"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def state(root):
    return {str(p.relative_to(root)): (p.lstat().st_mtime_ns, p.lstat().st_ctime_ns, p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in (root, *root.rglob("*"))}


class RepairCase(base.Base):
    def fixture(self, name="scan", dest="root"):
        root = self.tmp / dest
        shutil.copytree(FIX / name, root)
        return root

    def direct(self, root, *extra):
        proc = subprocess.run([str(MEMCHECK), "--root", ".", "--fix-index", *extra, "--dir", "demo/memory"], cwd=root,
                              capture_output=True, env=self.env())
        return proc.stdout

    def repair(self, root, *extra, dir="demo/memory"):
        return self.cli("repair", "--root", f"x={root}", "--dir", dir, *extra)

    def repair_dirs(self, root):
        base_dir = root / ".memory-repair"
        return sorted(p for p in base_dir.glob("rep-*")) if base_dir.is_dir() else []

    def test_a9_dry_run_passthrough(self):
        for name, extra in (("scan", []), ("shorten", ["--shorten"])):
            root = self.fixture(name, f"dry-{name}")
            before = state(root)
            proc = self.repair(root, *extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(proc.stdout)
            self.assertEqual(proc.stdout.encode(), self.direct(root, *extra))
            self.assertEqual(state(root), before)
            self.assertFalse((root / ".memory-repair").exists())

    def test_a10_idempotent_apply(self):
        for name, extra in (("scan", []), ("shorten", ["--shorten"])):
            root = self.fixture(name, f"app-{name}")
            index = root / "demo" / "memory" / "MEMORY.md"
            original = index.read_bytes()
            proposed = [ln.split(" :: ", 1)[1] for ln in self.direct(root, *extra).decode().splitlines() if " :: " in ln]
            self.assertTrue(proposed)
            proc = self.repair(root, *extra, "--apply")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            after = index.read_bytes()
            self.assertNotEqual(after, original)
            if name == "scan":
                self.assertEqual(after, original + "".join(ln + "\n" for ln in proposed).encode())
            dirs = self.repair_dirs(root)
            self.assertEqual(len(dirs), 1)
            manifest = json.loads((dirs[0] / "manifest.json").read_text())
            self.assertEqual(manifest["before_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(manifest["after_sha256"], hashlib.sha256(after).hexdigest())
            self.assertEqual(manifest["dir"], "demo/memory")
            self.assertEqual(manifest["repair_id"], dirs[0].name)
            self.assertEqual(oct(dirs[0].stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct((dirs[0] / "MEMORY.md.before").stat().st_mode & 0o777), "0o600")
            second = self.repair(root, *extra, "--apply")
            self.assertEqual(second.returncode, 0)
            self.assertIn("no change", second.stdout)
            self.assertEqual(len(self.repair_dirs(root)), 1)
            self.assertEqual(index.read_bytes(), after)

    def test_a11_reversible(self):
        root = self.fixture(dest="rev")
        index = root / "demo" / "memory" / "MEMORY.md"
        original = sha(index)
        self.repair(root, "--apply")
        rid = self.repair_dirs(root)[0].name
        self.assertNotEqual(sha(index), original)
        proc = self.cli("undo", "--root", f"x={root}", "--repair-id", rid)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sha(index), original)
        mtime = index.stat().st_mtime_ns
        proc = self.cli("undo", "--root", f"x={root}", "--repair-id", rid)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("already undone", proc.stdout)
        self.assertEqual(index.stat().st_mtime_ns, mtime)
        self.assertTrue(json.loads((root / ".memory-repair" / rid / "manifest.json").read_text())["undone"])
        # an edit after the repair blocks undo
        root2 = self.fixture(dest="rev2")
        index2 = root2 / "demo" / "memory" / "MEMORY.md"
        self.repair(root2, "--apply")
        rid2 = self.repair_dirs(root2)[0].name
        index2.write_text(index2.read_text() + "- [Mine](mine.md) — hand edit\n")
        edited = index2.read_bytes()
        proc = self.cli("undo", "--root", f"x={root2}", "--repair-id", rid2)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(index2.read_bytes(), edited)
        # symlinked .memory-repair
        root3 = self.fixture(dest="rev3")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, root3 / ".memory-repair")
        before = (root3 / "demo" / "memory" / "MEMORY.md").read_bytes()
        proc = self.repair(root3, "--apply")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual((root3 / "demo" / "memory" / "MEMORY.md").read_bytes(), before)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def outside_repair_root(self, dest="f4"):
        root = self.fixture(dest=dest)
        outside = self.tmp / f"{dest}-out"
        shutil.copytree(FIX / "scan" / "demo", outside / "o")
        canary = outside / "o" / "memory" / "MEMORY.md"
        canary.write_text(canary.read_text() + f"- [Out](out.md) — {base.OUTSIDE}\n")
        return root, outside, canary

    def assert_contained(self, root, outside, snapshot):
        self.assertEqual({p: p.read_bytes() for p in outside.rglob("*") if p.is_file()}, snapshot)
        self.assertEqual(list(root.rglob("manifest.json")), [])

    def test_a11b_untrusted_paths(self):
        root, outside, canary = self.outside_repair_root()
        snap = {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()}
        os.symlink(outside / "o", root / "evil")
        os.symlink(outside / "o" / "memory", root / "demo2")
        (root / "slug3").mkdir()
        os.symlink(outside / "o" / "memory", root / "slug3" / "memory")
        for bad in ("evil/memory", "slug3/memory", "/etc", str(outside / "o" / "memory"), "demo/memory/memory", "../f4-out/o/memory",
                    "demo", "demo/memory/", "./demo/memory"):
            proc = self.repair(root, "--apply", dir=bad)
            self.assertIn(proc.returncode, (2, 3), bad)
            self.assertNotIn(base.OUTSIDE, proc.stdout)
            self.assert_contained(root, outside, snap)
        # symlinked MEMORY.md
        root2, outside2, canary2 = self.outside_repair_root("f4b")
        snap2 = {p: p.read_bytes() for p in outside2.rglob("*") if p.is_file()}
        idx = root2 / "demo" / "memory" / "MEMORY.md"
        idx.unlink()
        os.symlink(canary2, idx)
        for extra in ([], ["--apply"]):
            proc = self.repair(root2, *extra)
            self.assertIn(proc.returncode, (2, 3))
            self.assert_contained(root2, outside2, snap2)
        # bad repair ids
        for rid in ("../x", "rep-x", "/etc/passwd", "rep-20260101T000000Z-zzzzzz"):
            proc = self.cli("undo", "--root", f"x={root}", "--repair-id", rid)
            self.assertIn(proc.returncode, (2, 3), rid)
        # tampered manifest and symlinked snapshot
        for tamper in ("dotdot", "absolute", "snapshot"):
            r, out, can = self.outside_repair_root(f"t-{tamper}")
            snap_t = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
            self.repair(r, "--apply")
            rd = self.repair_dirs(r)[0]
            local = (r / "demo" / "memory" / "MEMORY.md").read_bytes()
            manifest = json.loads((rd / "manifest.json").read_text())
            if tamper == "dotdot":
                manifest["dir"] = f"../t-{tamper}-out/o/memory"
            elif tamper == "absolute":
                manifest["dir"] = str(out / "o" / "memory")
            else:
                (rd / "MEMORY.md.before").unlink()
                os.symlink(can, rd / "MEMORY.md.before")
            (rd / "manifest.json").write_text(json.dumps(manifest))
            proc = self.cli("undo", "--root", f"x={r}", "--repair-id", rd.name)
            self.assertEqual(proc.returncode, 3, tamper)
            self.assertEqual({p: p.read_bytes() for p in out.rglob("*") if p.is_file()}, snap_t)
            self.assertEqual((r / "demo" / "memory" / "MEMORY.md").read_bytes(), local)
        # memcheck failure removes the snapshot
        r = self.fixture(dest="mcfail")
        (r / ".memory-repair").mkdir()
        sentinel = self.tmp / "sentinel"
        sentinel.write_text("KEEP")
        os.symlink(sentinel, r / ".memory-repair" / "audit.log")
        before = (r / "demo" / "memory" / "MEMORY.md").read_bytes()
        proc = self.repair(r, "--apply")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.repair_dirs(r), [])
        self.assertEqual(sentinel.read_text(), "KEEP")
        self.assertEqual((r / "demo" / "memory" / "MEMORY.md").read_bytes(), before)

    def test_a5_symlinked_note_stays_out(self):
        """F1: a valid symlinked note pointing outside is neither read by audit nor indexed by repair."""
        outside = self.tmp / "o5"
        outside.mkdir()
        (outside / "secret.md").write_text(f"---\nname: secret\ndescription: {base.OUTSIDE} payload\n---\nbody {base.OUTSIDE}\n")
        root = self.tmp / "r5"
        mem = root / "demo" / "memory"
        base.note(mem / "real.md", "real", "plain body")
        base.index(mem, [("real.md", "Real", "plain")])
        os.symlink(outside / "secret.md", mem / "link.md")
        index = mem / "MEMORY.md"
        before = index.read_bytes()
        for extra in ([], ["--shorten"]):
            proc = self.repair(root, *extra)
            self.assertNotIn(base.OUTSIDE, proc.stdout + proc.stderr)
            proc = self.repair(root, "--apply", *extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(base.OUTSIDE, proc.stdout + proc.stderr)
            self.assertEqual(index.read_bytes(), before)
        proc = self.cli("audit", "--root", f"x={root}")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(base.OUTSIDE, proc.stdout + proc.stderr)
        self.assertNotIn("link.md: ", json.loads(proc.stdout)["memcheck_report"])

    def test_a5b_symlinked_memory_md_and_newline_name(self):
        """Fix1: symlinked MEMORY.md is never read by audit; a newline-named file cannot alias a symlink."""
        outside = self.tmp / "o5b"
        outside.mkdir()
        (outside / "outside.md").write_text(f"- [Leak](leak.md) - {base.OUTSIDE} idx\n")
        (outside / "linked.md").write_text(f"---\nname: nl\ndescription: {base.OUTSIDE} nl\n---\nbody\n")
        root = self.tmp / "r5b"
        mem = root / "demo" / "memory"
        base.note(mem / "real.md", "real", "plain body")
        base.index(mem, [("real.md", "Real", "plain")])
        os.symlink(outside / "linked.md", mem / "linked.md")
        (mem / "x\nlinked.md").write_text("plain\n")
        mem2 = root / "other" / "memory"
        base.note(mem2 / "n.md", "n", "plain body")
        os.symlink(outside / "outside.md", mem2 / "MEMORY.md")
        index = mem / "MEMORY.md"
        before = index.read_bytes()
        for extra in ([], ["--shorten"], ["--apply"], ["--apply", "--shorten"]):
            proc = self.repair(root, *extra)
            self.assertNotIn(base.OUTSIDE, proc.stdout + proc.stderr)
        self.assertEqual(index.read_bytes(), before)
        proc = self.cli("audit", "--root", f"x={root}")
        self.assertNotIn(base.OUTSIDE, proc.stdout + proc.stderr)
        self.assertNotIn("leak.md", json.loads(proc.stdout)["memcheck_report"])

    def test_a11c_relative_root(self):
        """Fix1: a relative --root run from another cwd creates nothing outside and logs inside the root."""
        for n, rel in enumerate(("rel", "../w/rel")):
            work = self.tmp / f"cwd{n}"
            (work / "w").mkdir(parents=True)
            cwd = work / "c"
            cwd.mkdir()
            root = (cwd / rel).resolve()
            shutil.copytree(FIX / "scan", root)
            def outer():
                return sorted(str(p.relative_to(work)) for p in work.rglob("*") if root != p and root not in p.parents)
            snap = outer()
            proc = self.cli("repair", "--root", f"x={rel}", "--dir", "demo/memory", "--apply", cwd=cwd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((root / ".memory-repair" / "audit.log").is_file(), rel)
            self.assertEqual(snap, outer(), rel)

    def test_a12_descriptor_and_privacy(self):
        from modules.contracts import contracts
        from scripts import privacy_scan
        descriptor = contracts.check_module_descriptor(base.MEM, module="memory")
        self.assertEqual((descriptor["claimed_support"], descriptor["evidence"]), ("planned", []))
        self.assertEqual(privacy_scan.scan(base.MEM), [])
        needle = "/" + "home/"
        files = [p for p in base.MEM.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(needle, path.read_bytes().decode("utf-8", "replace"), str(path))

    def test_a13_audit(self):
        root = self.fixture(dest="aud")
        before = state(root)
        proc = self.cli("audit", "--root", f"ex={root}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("not referenced in MEMORY.md", out["memcheck_report"])
        self.assertTrue(any(s["citation"] == "ex:demo/memory/alpha.md" and s["date"] == "2020-01-15" for s in out["stale"]), out["stale"])
        self.assertEqual(out["conflicts"], [])
        self.assertEqual(out["review"], "human or reviewer seat; no model call")
        self.assertEqual(state(root), before)


if __name__ == "__main__":
    unittest.main()
