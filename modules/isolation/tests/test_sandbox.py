import importlib.util
import os
import pwd
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
CLI = os.environ.get("AIARCH_SANDBOX_CLI") or str(ROOT / "modules" / "isolation" / "sandbox.py")  # mutation seam
REQUIRE = os.environ.get("AIARCH_REQUIRE_SANDBOX") == "1"
PY = sys.executable
PW_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def sandbox(*args, env=None, timeout=60):
    return subprocess.run([PY, "-B", CLI, *map(str, args)], capture_output=True, text=True, timeout=timeout, env=env)


def host_has_token(token):
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if token.encode() in (entry / "cmdline").read_bytes():
                    return True
            except OSError:
                pass
    return False


def load_cli():
    spec = importlib.util.spec_from_file_location("sandbox_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Unavailable(unittest.TestCase):
    """A10: no sandbox means no run. Needs no bwrap."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_probe_without_bwrap_on_path(self):
        r = sandbox("probe", env={"PATH": str(self.tmp)})  # an empty directory: no bwrap there
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout.strip(), "unavailable: bwrap not found on PATH")

    def test_run_without_bwrap_exits_3_and_never_runs_the_command(self):
        marker = self.tmp / "marker"
        r = sandbox("run", "--rw", self.tmp, "--timeout", "5", "--", "/usr/bin/touch", marker, env={"PATH": str(self.tmp)})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("unavailable", r.stderr)
        self.assertFalse(marker.exists(), "the command ran without a sandbox")

    def test_unsupported_platform_names_the_platform(self):
        mod = load_cli()
        with mock.patch.object(sys, "platform", "darwin"):
            status = mod.probe()
        self.assertTrue(status.startswith("unavailable: "))
        self.assertIn("darwin", status)

    def test_setup_failure_is_unavailable_not_the_commands_status(self):
        fake = self.tmp / "bin"
        fake.mkdir()
        bw = fake / "bwrap"
        bw.write_text("#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n")
        bw.chmod(0o755)
        r = sandbox("probe", env={"PATH": str(fake)})
        self.assertEqual(r.returncode, 3)
        self.assertIn("cannot create a sandbox", r.stdout)
        # a bwrap that passes the probe but fails afterwards must be exit 3, never the command's rc
        bw.write_text('#!/bin/sh\ncase "$*" in *" -- true") exit 0;; esac\nexit 1\n')
        marker = self.tmp / "marker"
        r = sandbox("run", "--rw", self.tmp, "--timeout", "5", "--", "/usr/bin/touch", marker, env={"PATH": str(fake)})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertFalse(marker.exists())

    def test_rw_refusals(self):
        link = self.tmp / "ln"
        link.symlink_to(self.tmp)
        fake = self.tmp / "bin"
        fake.mkdir()
        for bad in ("/", str(PW_HOME), str(PW_HOME.parent), str(link), str(self.tmp / "missing"), "relative"):
            r = sandbox("run", "--rw", bad, "--timeout", "5", "--", "true")
            self.assertEqual(r.returncode, 2, (bad, r.stderr))
        self.assertEqual(sandbox("run", "--rw", self.tmp, "--timeout", "5").returncode, 2)  # no command
        self.assertEqual(sandbox("run", "--rw", self.tmp, "--timeout", "0", "--", "true").returncode, 2)


class Sandboxed(unittest.TestCase):
    """A9: asserts probe on this host first; skips with a reason only when bwrap cannot work (never with AIARCH_REQUIRE_SANDBOX=1)."""

    @classmethod
    def setUpClass(cls):
        p = sandbox("probe")
        cls.status = p.stdout.strip()
        if cls.status != "available":
            if REQUIRE:
                raise AssertionError(f"AIARCH_REQUIRE_SANDBOX=1 but probe says: {cls.status}")
            print(f"SKIPPED sandbox tests: {cls.status}", file=sys.stderr)

    def setUp(self):
        if self.status != "available":
            self.skipTest(self.status)
        self.dir = Path(tempfile.mkdtemp(prefix="aiarch-sbx-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(shutil.rmtree, str(getattr(self, "outside", "")) or "/nonexistent", True)

    def run_in(self, script, timeout="20", env=None):
        return sandbox("run", "--rw", self.dir, "--timeout", timeout, "--", PY, "-c", script, env=env)

    def test_probe_available_is_exit_0(self):
        self.assertEqual(sandbox("probe").returncode, 0)
        self.assertEqual(sandbox("probe", "--format", "json").stdout.count("available"), 2)

    def test_write_inside_ok_outside_fails_and_exit_status_propagates(self):
        outside = Path(tempfile.mkdtemp(prefix="aiarch-out-", dir=PW_HOME if os.access(PW_HOME, os.W_OK) else None))
        self.outside = outside
        r = self.run_in(f"open({str(self.dir / 'in.txt')!r}, 'w').write('x')\n"
                        f"try:\n open({str(outside / 'out.txt')!r}, 'w').write('x')\nexcept OSError: raise SystemExit(7)\nraise SystemExit(0)")
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertTrue((self.dir / "in.txt").exists())
        self.assertFalse((outside / "out.txt").exists())
        r = self.run_in("import os;open('/usr/leak','w')")
        self.assertNotEqual(r.returncode, 0)  # / is read-only

    def test_private_tmp(self):
        mine = Path(tempfile.mkdtemp(prefix="aiarch-tmp-"))
        self.addCleanup(shutil.rmtree, mine, True)
        (mine / "host.txt").write_text("host")
        r = self.run_in(f"import os;raise SystemExit(0 if not os.path.exists({str(mine / 'host.txt')!r}) else 9)")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_network_even_to_loopback(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        r = self.run_in(f"import socket\ntry:\n socket.create_connection(('127.0.0.1', {port}), 2)\nexcept OSError: raise SystemExit(0)\nraise SystemExit(9)")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_host_env_is_not_inherited(self):
        env = dict(os.environ, AIARCH_LEAK_CHECK="leaked")
        r = self.run_in("import os;print(os.environ.get('AIARCH_LEAK_CHECK'), sorted(os.environ))", env=env)
        self.assertIn("None", r.stdout.split()[0])
        self.assertNotIn("leaked", r.stdout)

    def test_proc_shows_only_sandbox_pids_and_pid_namespace_differs(self):
        r = self.run_in("import os;print(len([e for e in os.listdir('/proc') if e.isdigit()]), os.getpid(), os.readlink('/proc/self/ns/pid'))")
        count, pid, ns = r.stdout.split()
        self.assertLess(int(count), 10, r.stdout)
        self.assertLess(int(pid), 10)  # we are not in the host pid namespace
        self.assertNotEqual(ns, os.readlink("/proc/self/ns/pid"))

    def test_unix_socket_outside_dir_is_not_reachable_and_home_is_masked(self):
        base = Path(tempfile.mkdtemp(prefix="aiarch-u-", dir=PW_HOME if os.access(PW_HOME, os.W_OK) else None))
        self.outside = base
        if str(base).startswith(("/tmp", "/run")):
            self.skipTest("home directory is under /tmp or /run: nothing to prove outside the masks")
        (base / "planted.txt").write_text("secret")
        sockpath = base / "s.sock"
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(str(sockpath))
        srv.listen(1)
        self.addCleanup(srv.close)
        r = self.run_in(f"import socket, os\ns = socket.socket(socket.AF_UNIX)\n"
                        f"try:\n s.connect({str(sockpath)!r})\nexcept OSError: pass\nelse: raise SystemExit(9)\n"
                        f"raise SystemExit(8 if os.path.exists({str(base / 'planted.txt')!r}) else 0)")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_read_exposure_outside_home_is_documented_behaviour(self):
        # `/` is bound read-only, so world-readable system files stay readable (README: Exposure).
        r = self.run_in("print(len(open('/etc/os-release').read()) > 0)")
        self.assertEqual(r.stdout.strip(), "True", r.stderr)

    def test_timeout_kills_and_reaps_a_daemonised_child(self):
        token = "300." + secrets.token_hex(4)
        started = self.dir / "started"
        script = (f"import subprocess, time, pathlib\n"
                  f"subprocess.Popen(['sleep', {token!r}], start_new_session=True)\n"
                  f"pathlib.Path({str(started)!r}).write_text('1')\ntime.sleep(300)")
        t0 = time.monotonic()
        r = self.run_in(script, timeout="3")
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertTrue(started.exists())
        deadline = time.monotonic() + 5
        while host_has_token(token) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(host_has_token(token), "daemonised child survived the sandbox")

    def test_normal_exit_also_leaves_nothing_behind(self):
        token = "400." + secrets.token_hex(4)
        r = self.run_in(f"import subprocess\nsubprocess.Popen(['sleep', {token!r}], start_new_session=True)")
        self.assertEqual(r.returncode, 0, r.stderr)
        deadline = time.monotonic() + 5
        while host_has_token(token) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(host_has_token(token))


if __name__ == "__main__":
    unittest.main()
