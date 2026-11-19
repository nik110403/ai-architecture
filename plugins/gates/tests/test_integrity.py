#!/usr/bin/env python3
"""tools/integrity.py: manifest/check/install/rollback against a COPY of the plugin in a temp dir."""
import hashlib
import warnings

warnings.simplefilter("ignore", ResourceWarning)
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.realpath(os.path.join(HERE, ".."))
TOOL = os.path.join(PLUGIN, "tools", "integrity.py")
IGNORE = shutil.ignore_patterns("__pycache__", ".pwseam-active", "tests")


def tool(*args):
    return subprocess.run([sys.executable, "-B", TOOL, *map(str, args)], capture_output=True, text=True, timeout=60)


def snap(root):
    out = []
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(base, n)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out.append((os.path.relpath(p, root), "link", os.readlink(p)))
            elif stat.S_ISREG(st.st_mode):
                out.append((os.path.relpath(p, root), stat.S_IMODE(st.st_mode), hashlib.sha256(open(p, "rb").read()).hexdigest()))
    return sorted(out, key=str)


class Integrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p = os.path.join(self.tmp, "plugin")
        shutil.copytree(PLUGIN, self.p, ignore=IGNORE)
        self.m = os.path.join(self.tmp, "manifest.json")
        r = tool("manifest", "--plugin", self.p, "--out", self.m)
        self.assertEqual(r.returncode, 0, r.stderr)

    def check(self):
        return tool("check", "--plugin", self.p, "--manifest", self.m)

    def test_manifest_covers_hooks_json_defaults_plugin_json_and_lib(self):
        import json
        paths = {f["path"] for f in json.load(open(self.m))["files"]}
        for need in ("hooks/hooks.json", "hooks/run-hook.sh", "defaults.json", ".claude-plugin/plugin.json", "hooks/lib/gates_config.py", "hooks/lib/receipts.py", "hooks/review-stop-gate.py"):
            self.assertIn(need, paths)
        self.assertTrue(all("mode" in f and len(f["sha256"]) == 64 for f in json.load(open(self.m))["files"]))
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_tamper_cases_each_named(self):
        cases = {
            "hooks/no-code-guard.py": lambda p: open(p, "ab").write(b"#"),
            "hooks/hooks.json": lambda p: open(p, "ab").write(b" "),
            "defaults.json": lambda p: open(p, "ab").write(b" "),
            ".claude-plugin/plugin.json": lambda p: open(p, "ab").write(b" "),
            "hooks/lib/trackers.py": lambda p: os.unlink(p),
            "hooks/clean-state-check.py": lambda p: (os.unlink(p), os.symlink(os.path.join(self.p, "hooks/no-code-guard.py"), p)),
            "hooks/resume-inject.py": lambda p: os.chmod(p, 0o755 if not os.access(p, os.X_OK) else 0o644),
        }
        for rel, mutate in cases.items():
            with self.subTest(rel):
                full = os.path.join(self.p, rel)
                saved = None if os.path.islink(full) else open(full, "rb").read()
                mode = stat.S_IMODE(os.lstat(full).st_mode)
                mutate(full)
                r = self.check()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(rel, r.stdout)
                if os.path.lexists(full):
                    os.unlink(full)
                open(full, "wb").write(saved)
                os.chmod(full, mode)
                self.assertEqual(self.check().returncode, 0)

    def test_chmod_000_is_named_even_as_root(self):
        full = os.path.join(self.p, "hooks/no-code-guard.py")
        os.chmod(full, 0)
        r = self.check()
        if os.geteuid() == 0:
            print("NOTE: running as root: chmod 000 is readable, the mode change is what is reported", file=sys.stderr)
        self.assertEqual(r.returncode, 1)
        self.assertIn("hooks/no-code-guard.py", r.stdout)

    def test_extra_file_is_named(self):
        open(os.path.join(self.p, "hooks", "evil.py"), "w").write("x")
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("hooks/evil.py", r.stdout)

    def _plant_pyc(self):
        """An UNCHECKED_HASH pyc for lib/gates_config.py whose body prints PWNED: runs if python loads it."""
        import importlib.util
        import py_compile
        lib = os.path.join(self.p, "hooks", "lib")
        evil = os.path.join(self.tmp, "evil_src.py")
        open(evil, "w").write("import sys\nprint('PWNED planted pyc executed in guard')\nsys.exit(0)\n")
        cfile = importlib.util.cache_from_source(os.path.join(lib, "gates_config.py"))
        os.makedirs(os.path.dirname(cfile), exist_ok=True)
        py_compile.compile(evil, cfile=cfile, doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        return cfile

    def test_planted_pycache_is_flagged_by_check(self):
        cfile = self._plant_pyc()
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("__pycache__", r.stdout)
        os.unlink(cfile)
        os.rmdir(os.path.dirname(cfile))
        self.assertEqual(self.check().returncode, 0)

    def test_hook_runner_never_loads_a_planted_pyc(self):
        self._plant_pyc()
        payload = '{"tool_name":"Bash","tool_input":{"command":"git reset --hard"},"cwd":"/tmp"}'
        env = dict(os.environ, HOME=tempfile.mkdtemp())
        r = subprocess.run(["bash", os.path.join(self.p, "hooks", "bash-destructive-guard.sh")], input=payload, capture_output=True, text=True, env=env, timeout=30)
        self.assertNotIn("PWNED", r.stdout + r.stderr)
        self.assertIn('"permissionDecision"', r.stdout)
        self.assertNotIn("failing closed", r.stdout)  # the guard itself ran and decided; it was not replaced by the runner's fail-closed deny
        self.assertIn("git reset --hard", r.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root reads chmod-000 files")
    def test_install_copy_error_leaves_target_untouched(self):
        t = os.path.join(self.tmp, "live")
        shutil.copytree(self.p, t)
        before = snap(t)
        src = os.path.join(self.tmp, "src")
        shutil.copytree(self.p, src)
        bad = os.path.join(src, "hooks", "lib", "trackers.py")
        os.chmod(bad, 0)
        self.addCleanup(os.chmod, bad, 0o644)
        r = tool("install", "--from", src, "--to", t, "--manifest", self.m)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(snap(t), before)
        self.assertEqual(tool("check", "--plugin", t, "--manifest", self.m).returncode, 0)
        self.assertEqual([d for d in os.listdir(self.tmp) if ".new-" in d], [])

    def test_symlinked_directory_in_hooks_is_named(self):
        os.symlink(self.tmp, os.path.join(self.p, "hooks", "sneaky"))
        r = self.check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("hooks/sneaky", r.stdout)

    def test_install_good_source_backs_up_and_replaces(self):
        t = os.path.join(self.tmp, "live")
        shutil.copytree(self.p, t)
        open(os.path.join(t, "OLD"), "w").write("old")
        old = snap(t)
        r = tool("install", "--from", self.p, "--to", t, "--manifest", self.m)
        self.assertEqual(r.returncode, 0, r.stderr)
        baks = [d for d in os.listdir(self.tmp) if d.startswith("live.bak-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(snap(os.path.join(self.tmp, baks[0])), old)
        self.assertFalse(os.path.exists(os.path.join(t, "OLD")))
        self.assertEqual(tool("check", "--plugin", t, "--manifest", self.m).returncode, 0)

    def test_install_corrupt_source_restores_target_byte_identically(self):
        t = os.path.join(self.tmp, "live")
        shutil.copytree(self.p, t)
        open(os.path.join(t, "hooks", "KEEP"), "w").write("keep")
        os.chmod(os.path.join(t, "hooks", "KEEP"), 0o640)
        before = snap(t)
        with open(os.path.join(self.p, "hooks", "no-code-guard.py"), "ab") as fh:  # source flipped AFTER the manifest was made
            fh.write(b"#")
        r = tool("install", "--from", self.p, "--to", t, "--manifest", self.m)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no-code-guard.py", r.stderr)
        self.assertEqual(snap(t), before)

    def test_install_refuses_symlinked_target_and_source(self):
        real = os.path.join(self.tmp, "real")
        shutil.copytree(self.p, real)
        link = os.path.join(self.tmp, "linked")
        os.symlink(real, link)
        before = snap(real)
        r = tool("install", "--from", self.p, "--to", link, "--manifest", self.m)
        self.assertEqual(r.returncode, 2, r.stderr)
        r = tool("install", "--from", link, "--to", os.path.join(self.tmp, "t2"), "--manifest", self.m)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(snap(real), before)
        self.assertEqual(tool("install", "--from", self.p, "--to", real, "--manifest", os.path.join(self.tmp, "none.json")).returncode, 2)

    def test_rollback_restores_byte_identically(self):
        t = os.path.join(self.tmp, "live")
        shutil.copytree(self.p, t)
        bak = os.path.join(self.tmp, "live.bak-x")
        shutil.copytree(t, bak)
        want = snap(bak)
        os.unlink(os.path.join(t, "hooks", "run-hook.sh"))
        open(os.path.join(t, "extra"), "w").write("x")
        os.chmod(os.path.join(t, "hooks", "no-code-guard.py"), 0o600)
        r = tool("rollback", "--to", t, "--backup", bak)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snap(t), want)

    def test_rollback_refuses_symlinked_or_missing_backup_and_symlinked_target(self):
        t = os.path.join(self.tmp, "live")
        shutil.copytree(self.p, t)
        open(os.path.join(t, "marker"), "w").write("m")
        before = snap(t)
        bak = os.path.join(self.tmp, "bak")
        shutil.copytree(self.p, bak)
        blink = os.path.join(self.tmp, "baklink")
        os.symlink(bak, blink)
        for b in (blink, os.path.join(self.tmp, "absent")):
            r = tool("rollback", "--to", t, "--backup", b)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(snap(t), before)
        tlink = os.path.join(self.tmp, "tlink")
        os.symlink(t, tlink)
        self.assertEqual(tool("rollback", "--to", tlink, "--backup", bak).returncode, 2)
        self.assertEqual(snap(t), before)


if __name__ == "__main__":
    unittest.main()
