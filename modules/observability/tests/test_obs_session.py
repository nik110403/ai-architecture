"""A6 (continuity contract), A13 (read-only probes), AM7 (static: no network, subprocess only where allowed)."""

import builtins
import os
import re
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from obs_helpers import MOD, ROOT, Isolated, sha, tree_state  # noqa: E402
from modules.observability import probes  # noqa: E402

CONT = ROOT / "modules" / "continuity" / "continuity.py"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def continuity(*argv):
    env = dict(os.environ, PATH=str(Path(os.environ["HOME"]) / "empty"))
    return subprocess.run([sys.executable, "-B", str(CONT), *map(str, argv)], capture_output=True, text=True, env=env)


class SessionReport(Isolated):  # A6
    def test_unknown_usage_stays_unknown_and_ledger_is_untouched(self):
        proj = self.t / "proj"
        proj.mkdir()
        self.assertEqual(continuity("init", "--project", proj).returncode, 0)
        self.assertEqual(continuity("usage", "record", "--session", "claude-code:s1", "--tokens", "unknown",
                                    "--project", proj).returncode, 0)
        ledger = next((proj / ".ai-continuity").rglob("ledger.json"))
        before = (sha(ledger), __import__("json").loads(ledger.read_text())["revision"])
        opened = []
        real_open, real_os_open = builtins.open, os.open

        def rec_open(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)

        def rec_os_open(path, *a, **k):
            opened.append(str(path))
            return real_os_open(path, *a, **k)

        with mock.patch.object(builtins, "open", rec_open), mock.patch.object(os, "open", rec_os_open):
            code, out = self.cli("session", "--project", proj, "--session", "claude-code:s1")
        self.assertEqual(code, 0)
        s = out["sessions"][0]
        self.assertIsNone(s["usage_tokens"])
        self.assertEqual(s["phase"], "UNKNOWN")
        self.assertEqual((sha(ledger), __import__("json").loads(ledger.read_text())["revision"]), before)
        self.assertFalse([p for p in opened if p.endswith("ledger.json")])

    def test_session_is_read_only_by_construction(self):
        text = (MOD / "reports.py").read_text()
        self.assertNotIn("usage record", text)
        self.assertNotIn('"record"', text)
        self.assertEqual(len(re.findall(r"subprocess\.run\(", text)), 1)  # the single status call


class Probes(Isolated):  # A13
    def fixture_proc(self):
        root = self.t / "proc"
        d = root / "4242"
        d.mkdir(parents=True)
        (d / "stat").write_text("4242 (fake proc) S 1 4242 4242 0 -1 4194560 100 0 0 0 7 3 0 0 20 0 2 0 99 8192000 250 18446744073709551615\n")
        (d / "status").write_text("Name:\tfake proc\nState:\tS (sleeping)\nThreads:\t2\nVmRSS:\t1000 kB\n")
        for p in (d / "stat", d / "status"):
            p.chmod(0o400)
        d.chmod(0o500)
        root.chmod(0o500)
        self.addCleanup(lambda: (root.chmod(0o700), d.chmod(0o700)))
        return root

    def guarded(self, fn):
        opens, calls = [], []
        real_open, real_os_open = builtins.open, os.open

        def rec_open(file, mode="r", *a, **k):
            opens.append((str(file), any(c in mode for c in "wax+") and "w" or "r"))
            return real_open(file, mode, *a, **k)

        def rec_os_open(path, flags, *a, **k):
            opens.append((str(path), "w" if flags & WRITE_FLAGS else "r"))
            return real_os_open(path, flags, *a, **k)

        def trap(*a, **k):
            calls.append(1)
            raise AssertionError("forbidden call")

        with mock.patch.object(builtins, "open", rec_open), mock.patch.object(os, "open", rec_os_open), \
                mock.patch.object(socket.socket, "connect", trap), mock.patch.object(socket, "create_connection", trap), \
                mock.patch.object(socket, "getaddrinfo", trap), mock.patch.object(subprocess, "Popen", trap):
            result = fn()
        return result, opens, calls

    def test_fixture_proc_root_is_read_only_and_parsed(self):
        root = self.fixture_proc()
        before = tree_state(root)
        rep, opens, calls = self.guarded(lambda: probes.resource_report([4242], proc_root=str(root)))
        self.assertEqual(tree_state(root), before)
        self.assertEqual(calls, [])
        self.assertEqual([o for o in opens if o[1] == "w"], [])
        self.assertFalse([o for o in opens if re.search(r"cmdline|environ|/fd", o[0])])
        p = rep["processes"][0]
        self.assertEqual((p["pid"], p["state"], p["threads"], p["rss_kb"]), (4242, "S", 2, 1000))
        self.assertEqual((p["utime_ticks"], p["stime_ticks"]), (7, 3))

    def test_unreadable_and_absent_fields_are_null(self):
        root = self.fixture_proc()
        (root / "4242").chmod(0o700)
        (root / "4242" / "status").chmod(0)
        (root / "4242").chmod(0o500)
        rep, _, _ = self.guarded(lambda: probes.resource_report([4242, 999], proc_root=str(root)))
        p, gone = rep["processes"]
        self.assertIsNone(p["rss_kb"])
        self.assertEqual(p["state"], "S")  # stat is still readable
        self.assertTrue(all(v is None for k, v in gone.items() if k != "pid"))

    def test_own_pid_under_real_proc(self):
        before = (Path("/proc") / str(os.getpid()) / "status").stat().st_mtime_ns
        rep, opens, calls = self.guarded(lambda: probes.resource_report([os.getpid()]))
        self.assertEqual(calls, [])
        self.assertEqual([o for o in opens if o[1] == "w"], [])
        self.assertIsNotNone(rep["processes"][0]["rss_kb"])
        self.assertEqual((Path("/proc") / str(os.getpid()) / "status").stat().st_mtime_ns, before)

    def test_bad_pids_are_refused(self):
        code, _ = self.cli("resources", "--pid", "0")
        self.assertEqual(code, 2)

    def test_probes_module_has_no_subprocess(self):
        self.assertNotIn("subprocess", (MOD / "probes.py").read_text())


class NoNetwork(unittest.TestCase):  # AM7 static
    def test_static_scan(self):
        for path in sorted(MOD.glob("*.py")):
            text = path.read_text()
            for bad in ("urllib", "http.client", "import socket", "requests", ".credentials"):
                self.assertNotIn(bad, text, f"{path.name}: {bad}")
            if path.name != "reports.py":
                self.assertNotIn("subprocess", text, path.name)

    def test_reports_subprocess_runs_only_continuity_with_sys_executable(self):
        text = (MOD / "reports.py").read_text()
        self.assertIn("sys.executable", text)
        self.assertIn("continuity.py", text)
        self.assertIn("timeout=5", text)


if __name__ == "__main__":
    unittest.main()
