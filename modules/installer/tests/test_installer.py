"""U16 A15 (install / upgrade / rollback / interrupt / recover) and the installer half of A16. Offline, temp roots only.

The interrupt seam runs `apply` in a subprocess that calls os._exit(137) right after journal step k (no handler runs);
`recover` then runs in a fresh process.
"""

import hashlib
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
CLI = MOD / "installer.py"
PY = sys.executable
TAKE = ["--allow-takeover"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            out[str(p.relative_to(root))] = (p.read_bytes(), st.st_mode & 0o7777)
    return out


def tree_mtimes(root):
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in sorted(Path(root).rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / "target"
        self.root.mkdir()
        self.state = self.tmp / "inst-state"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp / "home"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        (self.tmp / "home").mkdir()

    def manifest(self, name, version, files, sub=None):
        """files: {dest: (bytes, mode_int)}; writes sources and manifest.json; returns its path."""
        d = self.tmp / (sub or f"m-{version}")
        (d / "files").mkdir(parents=True, exist_ok=True)
        entries = []
        for i, (dest, (data, mode)) in enumerate(files.items()):
            (d / "files" / f"f{i}").write_bytes(data)
            entries.append({"src": f"files/f{i}", "dest": dest, "mode": f"{mode:04o}", "sha256": sha(data)})
        m = d / "manifest.json"
        m.write_text(json.dumps({"schema": "installer-manifest/1", "name": name, "version": version, "files": entries}))
        return m

    def run_cli(self, sub, manifest=None, root="default", extra=(), env=None, state="default"):
        a = [PY, "-B", str(CLI), sub]
        if manifest is not None:
            a += ["--manifest", str(manifest)]
        if root == "default":
            root = self.root
        if root is not None:
            a += ["--target-root", str(root)]
        if state == "default":
            state = self.state
        if state is not None:
            a += ["--state-dir", str(state)]
        a += list(extra)
        r = subprocess.run(a, capture_output=True, text=True, env=env or self.env, cwd=str(self.tmp), stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        out = None
        for line in reversed(r.stdout.splitlines()):
            if line.startswith("{"):
                out = json.loads(line)
                break
        if out is None and r.stdout.strip().startswith("{"):
            out = json.loads(r.stdout)
        return r.returncode, out, r

    V1 = {"bin/tool": (b"#!/bin/sh\necho v1\n", 0o755), "etc/tool.conf": (b"setting=1\n", 0o644)}
    V2 = {"bin/tool": (b"#!/bin/sh\necho v2\n", 0o755), "etc/tool.conf": (b"setting=2\n", 0o640), "share/new.txt": (b"new\n", 0o644)}

    def seed_original(self):
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc" / "tool.conf").write_bytes(b"user setting\n")
        (self.root / "etc" / "tool.conf").chmod(0o600)
        (self.root / "keep.txt").write_text("keep\n")


class A15InstallRollback(Base):
    def test_a15_apply_noop_upgrade_rollback(self):
        self.seed_original()
        original = tree(self.root)
        m1 = self.manifest("demo", "1.0.0", self.V1)
        m2 = self.manifest("demo", "2.0.0", self.V2)
        rc, out, _ = self.run_cli("plan", m1)
        self.assertEqual((rc, out["reason"]), (0, "plan"))
        self.assertEqual(tree(self.root), original, "plan wrote")
        self.assertFalse(self.state.exists(), "plan created the state dir")
        rc, out, _ = self.run_cli("apply", m1, extra=["--allow-takeover"])
        self.assertEqual((rc, out["reason"]), (0, "applied"), out)
        self.assertEqual((self.root / "bin/tool").read_bytes(), self.V1["bin/tool"][0])
        self.assertEqual(os.stat(self.root / "bin/tool").st_mode & 0o7777, 0o755)
        after1 = tree(self.root)
        mt = tree_mtimes(self.root)
        rc, out, _ = self.run_cli("apply", m1)
        self.assertEqual((rc, out["reason"]), (0, "no-change"))
        self.assertEqual(tree_mtimes(self.root), mt, "a no-op apply touched files")
        self.assertEqual(tree(self.root), after1)
        rc, out, _ = self.run_cli("apply", m2)
        self.assertEqual((rc, out["reason"]), (0, "applied"), out)
        self.assertEqual((self.root / "share/new.txt").read_bytes(), b"new\n")
        self.assertEqual(os.stat(self.root / "etc/tool.conf").st_mode & 0o7777, 0o640)
        before_status = tree(self.root)
        rc, st, _ = self.run_cli("status", m2)
        self.assertEqual((rc, st["state"]), (0, "installed"))
        self.assertEqual(tree(self.root), before_status, "status wrote")
        rc, out, _ = self.run_cli("rollback", m2)
        self.assertEqual((rc, out["reason"]), (0, "rolled-back"), out)
        self.assertEqual(tree(self.root), original, "rollback is not byte- and mode-identical to the original")
        rc, st, _ = self.run_cli("status", m2)
        self.assertEqual(st["state"], "absent")

    def test_a15_user_edited_file_is_preserved(self):
        m1 = self.manifest("demo", "1.0.0", self.V1)
        m2 = self.manifest("demo", "2.0.0", self.V2)
        self.assertEqual(self.run_cli("apply", m1)[0], 0)
        (self.root / "etc/tool.conf").write_bytes(b"my own edit\n")
        snap = tree(self.root)
        rc, out, _ = self.run_cli("apply", m2)
        self.assertEqual((rc, out["reason"]), (1, "user-change"), out)
        self.assertEqual(tree(self.root), snap, "a user-changed file was overwritten")
        self.assertEqual((self.root / "etc/tool.conf").read_bytes(), b"my own edit\n")

    def test_a15_rollback_removes_created_files_but_keeps_user_added_ones(self):
        m1 = self.manifest("demo", "1.0.0", self.V1)
        self.assertEqual(self.run_cli("apply", m1)[0], 0)
        (self.root / "bin" / "mine").write_text("mine")
        self.assertEqual(self.run_cli("rollback", m1)[0], 0)
        self.assertFalse((self.root / "etc/tool.conf").exists())
        self.assertFalse((self.root / "bin/tool").exists())
        self.assertTrue((self.root / "bin/mine").exists())

    def test_a15_interrupt_at_every_journal_step_then_recover(self):
        self.seed_original()
        m1 = self.manifest("demo", "1.0.0", self.V1)
        m2 = self.manifest("demo", "2.0.0", self.V2)
        for label, prep, manifest in (("fresh", None, m1), ("upgrade", m1, m2)):
            # reference run: the state before and after an uninterrupted apply
            ref = self.tmp / f"ref-{label}"
            shutil.copytree(self.root, ref)
            ref_state = self.tmp / f"refstate-{label}"
            if prep:
                self.assertEqual(self.run_cli("apply", prep, root=ref, state=ref_state, extra=TAKE)[0], 0)
            before = tree(ref)
            self.assertEqual(self.run_cli("apply", manifest, root=ref, state=ref_state, extra=TAKE)[0], 0)
            after = tree(ref)
            k = 1
            crashed = 0
            while True:
                root = self.tmp / f"r-{label}-{k}"
                state = self.tmp / f"s-{label}-{k}"
                shutil.copytree(self.root, root)
                if prep:
                    self.assertEqual(self.run_cli("apply", prep, root=root, state=state, extra=TAKE)[0], 0)
                rc, out, r = self.run_cli("apply", manifest, root=root, state=state, extra=TAKE, env={**self.env, "AIARCH_INSTALLER_CRASH_AFTER": str(k)})
                if rc == 0:
                    self.assertGreater(k, 3, "the transaction has too few journal steps to be a transaction")
                    break
                self.assertEqual(rc, 137, (label, k, r.stderr))
                crashed += 1
                rc, st, _ = self.run_cli("status", manifest, root=root, state=state)
                self.assertEqual((rc, st["state"]), (1, "interrupted"), (label, k, st))
                rc, rec, _ = self.run_cli("recover", manifest, root=root, state=state)
                self.assertEqual(rc, 0, (label, k, rec))
                got = tree(root)
                self.assertIn(rec["outcome"], ("reverted", "completed"))
                expect = after if rec["outcome"] == "completed" else before
                got_files = {p: v for p, v in got.items() if not p.startswith(".")}
                exp_files = {p: v for p, v in expect.items() if not p.startswith(".")}
                self.assertEqual(got_files, exp_files, f"{label} step {k}: a mixed tree after recover")
                rc, st, _ = self.run_cli("status", manifest, root=root, state=state)
                self.assertEqual(rc, 0, (label, k, st))
                want = "installed" if expect is after else ("installed-previous" if prep else "absent")
                self.assertEqual(st["state"], want, (label, k, st))
                k += 1
            self.assertGreaterEqual(crashed, 4)

    def test_a15_refusals(self):
        def m(dest):
            return self.manifest("bad", "1", {dest: (b"x", 0o644)}, sub=f"bad-{abs(hash(dest))}")
        for dest in ("../escape", "a/../../escape", "/abs/path", "a//b/../../../x"):
            rc, out, _ = self.run_cli("apply", m(dest))
            self.assertEqual((rc, out["reason"]), (2, "dest-unsafe"), (dest, out))
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside)
        rc, out, _ = self.run_cli("apply", m("link/file"))
        self.assertEqual((rc, out["reason"]), (2, "symlink-component"), out)
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "leaf").symlink_to(outside / "t")
        rc, out, _ = self.run_cli("apply", m("leaf"))
        self.assertEqual((rc, out["reason"]), (2, "symlink-component"), out)
        rc, out, _ = self.run_cli("apply", m("ok/file"), root=None)
        self.assertEqual((rc, out["reason"]), (2, "target-root-required"), out)
        rc, out, _ = self.run_cli("apply", m("ok/file"), root=self.tmp / "missing-root")
        self.assertEqual((rc, out["reason"]), (2, "target-root-invalid"), out)
        bad = self.manifest("badhash", "1", {"x": (b"x", 0o644)}, sub="badhash")
        (bad.parent / "files" / "f0").write_bytes(b"tampered")
        rc, out, _ = self.run_cli("apply", bad)
        self.assertEqual((rc, out["reason"]), (2, "hash-mismatch"), out)
        self.assertFalse((self.root / "x").exists())

    def test_a15_foreign_owner_seam(self):
        sys.path.insert(0, str(ROOT))
        from unittest import mock
        from modules.installer import installer as I
        m = self.manifest("demo", "1", {"a": (b"x", 0o644)})
        with mock.patch.object(I, "expected_uid", lambda: os.geteuid() + 1):
            with self.assertRaises(I.Refused) as cm:
                I.load_target(str(self.root))
        self.assertEqual(cm.exception.reason, "foreign-owner")
        self.assertEqual(self.run_cli("apply", m)[0], 0)


class A17Unmanaged(Base):
    """An existing destination the installer never recorded is refused unless the caller names --allow-takeover."""

    def put(self, dest, data, mode):
        p = self.root / dest
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)

    def recorded_state(self):
        """State written by apply (the lock file alone does not count)."""
        return sorted(p.name for p in self.state.glob("*") if p.name not in ("lock", ".lock")) if self.state.exists() else []

    def actions(self, manifest, extra=()):
        rc, out, _ = self.run_cli("plan", manifest, extra=extra)
        self.assertEqual((rc, out["reason"]), (0, "plan"), out)
        return {a["dest"]: a["action"] for a in out["actions"]}

    def test_differing_unmanaged_file_is_refused(self):
        for dest in ("etc/tool.conf", ".claude/CLAUDE.md", ".claude/settings.json"):
            with self.subTest(dest=dest):
                shutil.rmtree(self.root)
                self.root.mkdir()
                shutil.rmtree(self.state, ignore_errors=True)
                self.put(dest, b"mine\n", 0o600)
                m = self.manifest("demo", "1", {dest: (b"theirs\n", 0o644)}, sub="m-" + dest.replace("/", "_"))
                snap = tree(self.root)
                rc, out, _ = self.run_cli("apply", m)
                self.assertEqual((rc, out["reason"]), (1, "unmanaged-file"), out)
                self.assertIn(dest, out["message"])
                self.assertEqual(tree(self.root), snap)
                self.assertEqual(self.recorded_state(), [], "a refused apply recorded state")

    def test_mode_only_difference_is_refused(self):
        self.put("bin/tool", self.V1["bin/tool"][0], 0o700)
        m = self.manifest("demo", "1", {"bin/tool": self.V1["bin/tool"]})
        snap = tree(self.root)
        rc, out, _ = self.run_cli("apply", m)
        self.assertEqual((rc, out["reason"]), (1, "unmanaged-file"), out)
        self.assertEqual(tree(self.root), snap)
        self.assertEqual(self.actions(m), {"bin/tool": "unmanaged"})

    def test_one_collision_blocks_the_creatable_files_too(self):
        self.put("etc/tool.conf", b"mine\n", 0o600)
        m = self.manifest("demo", "1", self.V1)
        snap = tree(self.root)
        rc, out, _ = self.run_cli("apply", m)
        self.assertEqual((rc, out["reason"]), (1, "unmanaged-file"), out)
        self.assertIn("etc/tool.conf", out["message"])
        self.assertNotIn("bin/tool", out["message"])
        self.assertEqual(tree(self.root), snap, "a creatable file was written next to a refused one")
        self.assertFalse((self.root / "bin").exists())
        self.assertEqual(self.recorded_state(), [])

    def test_plan_labels_with_and_without_the_flag(self):
        self.put("etc/tool.conf", b"mine\n", 0o600)
        self.put("bin/tool", self.V1["bin/tool"][0], 0o700)
        m = self.manifest("demo", "1", {**self.V1, "share/n": (b"n", 0o644)})
        snap = tree(self.root)
        self.assertEqual(self.actions(m), {"bin/tool": "unmanaged", "etc/tool.conf": "unmanaged", "share/n": "create"})
        self.assertEqual(self.actions(m, ["--allow-takeover"]), {"bin/tool": "takeover", "etc/tool.conf": "takeover", "share/n": "create"})
        self.assertEqual(tree(self.root), snap, "plan wrote")
        self.assertFalse(self.state.exists())

    def test_takeover_then_rollback_restores_bytes_and_mode(self):
        self.put("etc/tool.conf", b"mine\n", 0o600)
        self.put("bin/tool", self.V1["bin/tool"][0], 0o700)  # mode-only collision
        original = tree(self.root)
        m = self.manifest("demo", "1", self.V1)
        rc, out, _ = self.run_cli("apply", m, extra=TAKE)
        self.assertEqual((rc, out["reason"]), (0, "applied"), out)
        self.assertEqual((self.root / "etc/tool.conf").read_bytes(), b"setting=1\n")
        self.assertEqual(os.stat(self.root / "bin/tool").st_mode & 0o7777, 0o755)
        rc, out, _ = self.run_cli("rollback", m)
        self.assertEqual((rc, out["reason"]), (0, "rolled-back"), out)
        self.assertEqual(tree(self.root), original)

    def test_the_flag_does_not_persist(self):
        self.put("etc/tool.conf", b"mine\n", 0o600)
        self.assertEqual(self.run_cli("apply", self.manifest("demo", "1", self.V1), extra=TAKE)[0], 0)
        self.put("share/new.txt", b"someone else's\n", 0o644)
        m2 = self.manifest("demo", "2", self.V2)
        snap = tree(self.root)
        rc, out, _ = self.run_cli("apply", m2)
        self.assertEqual((rc, out["reason"]), (1, "unmanaged-file"), out)
        self.assertEqual(tree(self.root), snap)
        self.assertEqual(self.actions(m2), {"bin/tool": "replace", "etc/tool.conf": "replace", "share/new.txt": "unmanaged"})

    def test_recorded_cells_keep_their_outcome(self):
        m1 = self.manifest("demo", "1", self.V1)
        self.assertEqual(self.run_cli("apply", m1)[0], 0)
        self.assertEqual(self.actions(m1), {"bin/tool": "unchanged", "etc/tool.conf": "unchanged"})
        (self.root / "bin/tool").chmod(0o700)  # recorded, bytes same, mode differs
        self.assertEqual(self.actions(m1)["bin/tool"], "replace")
        (self.root / "etc/tool.conf").write_bytes(b"edited\n")  # recorded, bytes differ from record
        self.assertEqual(self.actions(m1)["etc/tool.conf"], "conflict")
        self.assertEqual(self.actions(m1, ["--allow-takeover"])["etc/tool.conf"], "conflict")
        rc, out, _ = self.run_cli("apply", m1, extra=TAKE)
        self.assertEqual((rc, out["reason"]), (1, "user-change"), out)

    def test_absent_recorded_file_is_recreated(self):
        m = self.manifest("demo", "1", self.V1)
        self.assertEqual(self.run_cli("apply", m)[0], 0)
        (self.root / "bin/tool").unlink()
        self.assertEqual(self.actions(m)["bin/tool"], "create")
        self.assertEqual(self.run_cli("apply", m)[0], 0)
        self.assertEqual(tree(self.root)["bin/tool"], self.V1["bin/tool"])

    def test_identical_unrecorded_with_flag_stays_unchanged(self):
        self.put("etc/tool.conf", *self.V1["etc/tool.conf"])
        m = self.manifest("demo", "1", self.V1)
        self.assertEqual(self.actions(m, TAKE)["etc/tool.conf"], "unchanged")
        self.assertEqual(self.run_cli("apply", m, extra=TAKE)[0], 0)
        self.assertEqual(self.run_cli("rollback", m)[0], 0)
        self.assertEqual(tree(self.root), {"etc/tool.conf": self.V1["etc/tool.conf"]})

    def test_mode_differs_recorded_apply_restores_mode(self):
        m = self.manifest("demo", "1", self.V1)
        self.assertEqual(self.run_cli("apply", m)[0], 0)
        (self.root / "bin/tool").chmod(0o700)
        rc, out, _ = self.run_cli("apply", m)
        self.assertEqual((rc, out["reason"]), (0, "applied"), out)
        self.assertEqual(os.stat(self.root / "bin/tool").st_mode & 0o7777, 0o755)

    def test_flag_refused_on_status_rollback_recover(self):
        m = self.manifest("demo", "1", self.V1)
        for sub in ("status", "rollback", "recover"):
            with self.subTest(sub=sub):
                rc, out, _ = self.run_cli(sub, m, extra=TAKE)
                self.assertEqual((rc, out["reason"]), (2, "usage"), out)

    def test_identical_unrecorded_file_survives_rollback(self):
        data, mode = b"same\n", 0o600
        self.put("keep/same.txt", data, mode)
        m = self.manifest("demo", "1", {"keep/same.txt": (data, mode), "bin/tool": self.V1["bin/tool"]})
        self.assertEqual(self.actions(m)["keep/same.txt"], "unchanged")
        self.assertEqual(self.run_cli("apply", m)[0], 0)
        self.assertEqual(self.run_cli("rollback", m)[0], 0)
        self.assertEqual(tree(self.root), {"keep/same.txt": (data, mode)})

    def test_identical_unrecorded_file_survives_upgrade_then_rollback(self):
        data, mode = b"same\n", 0o600
        self.put("keep/same.txt", data, mode)
        m1 = self.manifest("demo", "1", {"keep/same.txt": (data, mode), "bin/tool": self.V1["bin/tool"]})
        m2 = self.manifest("demo", "2", {"keep/same.txt": (b"changed\n", 0o644), "bin/tool": self.V1["bin/tool"]})
        self.assertEqual(self.run_cli("apply", m1)[0], 0)
        rc, out, _ = self.run_cli("apply", m2)
        self.assertEqual((rc, out["reason"]), (0, "applied"), out)
        self.assertEqual((self.root / "keep/same.txt").read_bytes(), b"changed\n")
        self.assertEqual(self.run_cli("rollback", m2)[0], 0)
        self.assertEqual(tree(self.root), {"keep/same.txt": (data, mode)})

    def test_interrupted_takeover_recovers(self):
        self.put("etc/tool.conf", b"mine\n", 0o600)
        original = tree(self.root)
        m = self.manifest("demo", "1", self.V1)
        rc, out, r = self.run_cli("apply", m, extra=TAKE, env={**self.env, "AIARCH_INSTALLER_CRASH_AFTER": "3"})
        self.assertEqual(rc, 137, r.stderr)
        rc, out, _ = self.run_cli("recover", m)
        self.assertEqual((rc, out["outcome"]), (0, "reverted"), out)
        self.assertEqual(tree(self.root), original)


class A18BackupAtomicity(Base):
    """A kill while a backup is being written must never let recover restore from an incomplete backup."""

    def test_kill_around_backup_leaves_user_file_untouched(self):
        for point in ("backup:pre-chmod", "backup:unjournaled"):
            with self.subTest(point=point):
                shutil.rmtree(self.root, ignore_errors=True)
                shutil.rmtree(self.state, ignore_errors=True)
                self.root.mkdir()
                (self.root / "etc").mkdir()
                mine = self.root / "etc/tool.conf"
                mine.write_bytes(b"user secret\n")
                mine.chmod(0o600)
                original = tree(self.root)
                m = self.manifest("demo", "1", self.V1, sub="m-" + point.replace(":", "_"))
                rc, _, r = self.run_cli("apply", m, extra=TAKE, env={**self.env, "AIARCH_INSTALLER_FAULT": point})
                self.assertEqual(rc, 137, r.stderr)
                rc, out, _ = self.run_cli("recover", m)
                self.assertEqual((rc, out["outcome"]), (0, "reverted"), out)
                self.assertEqual(tree(self.root), original)

    def test_refused_apply_writes_nothing_with_default_state_dir(self):
        (self.root / "etc").mkdir()
        (self.root / "etc/tool.conf").write_bytes(b"mine\n")
        m = self.manifest("demo", "1", self.V1)
        snap = tree(self.root)
        dirs = sorted(p.name for p in self.root.rglob("*"))
        rc, out, _ = self.run_cli("apply", m, state=None)
        self.assertEqual((rc, out["reason"]), (1, "unmanaged-file"), out)
        self.assertEqual(tree(self.root), snap)
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), dirs)
        self.assertFalse((self.root / ".installer").exists())

    def test_originals_keep_the_source_mode(self):
        (self.root / "etc").mkdir()
        (self.root / "etc/tool.conf").write_bytes(b"mine\n")
        (self.root / "etc/tool.conf").chmod(0o640)
        m = self.manifest("demo", "1", self.V1)
        self.assertEqual(self.run_cli("apply", m, extra=TAKE)[0], 0)
        stored = list((self.state / "originals").iterdir())
        self.assertEqual([p.read_bytes() for p in stored], [b"mine\n"])
        self.assertEqual(stored[0].stat().st_mode & 0o7777, 0o640)


class A16Constraints(Base):
    def test_a16_descriptor_provenance_privacy_static(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT))
        import check_collection as cc
        import privacy_scan as ps
        from modules.contracts import contracts as C
        d = C.check_module_descriptor(MOD, module="installer")
        self.assertEqual(d["claimed_support"], "planned")
        self.assertEqual([x["identifier"] for x in d["dependencies"]], ["core-contracts"])
        cc.provenance(MOD / "PROVENANCE.md")
        self.assertEqual(ps.scan(MOD), [])
        banned = re.compile(r"urllib\.request|http\.client|\bssl\b|\brequests\b|socket\.create_connection|\bnpx\b|\bnpm |pip install")
        for f in MOD.glob("*.py"):
            self.assertIsNone(banned.search(f.read_text()), f.name)
        ls = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", str(MOD)], cwd=ROOT, capture_output=True, text=True).stdout.split()
        for name in ls:
            self.assertNotIn(b"\0", (ROOT / name).read_bytes(), name)
        # the shipped example installs into a temp root and rolls back
        ex = MOD / "example" / "manifest.json"
        rc, out, _ = self.run_cli("apply", ex)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.run_cli("rollback", ex)[0], 0)
        self.assertEqual(list(self.root.rglob("*.*")), [])

    def test_a16_outputs_redact_paths(self):
        m = self.manifest("demo", "1", {"a/b": (b"x", 0o644)})
        rc, out, r = self.run_cli("apply", m)
        for text in (r.stdout, r.stderr):
            self.assertNotIn(str(self.tmp), text)
        rc, out, r = self.run_cli("apply", self.manifest("bad", "1", {"../x": (b"x", 0o644)}, sub="b2"))
        self.assertNotIn(str(self.tmp), r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
