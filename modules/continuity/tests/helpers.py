"""Shared fixtures: temporary project and state, fake local-command workers on a temporary PATH, a CLI runner.

Real `dispatch.py` subprocesses with fake workers; never a real model, never the network. A test that cannot
run on this platform fails with a message instead of skipping.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONT = HERE.parent
ROOT = CONT.parents[1]
CLI = CONT / "continuity.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules" / "orchestration" / "tests"))

from modules.continuity import continuity, jobs, reconcile  # noqa: E402
from modules.continuity import ledger as L  # noqa: E402
from modules.orchestration import dispatch  # noqa: E402

FAKE = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
sys.stdin.read()
{body}
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_for(check, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return None


def stragglers(root):
    """Live processes (not zombies, not this one) whose cwd or argv points under `root`: whatever a test left behind."""
    root, found, me = str(root), {}, os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            if Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                continue
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        if root in argv or cwd == root or cwd.startswith(root + os.sep):
            found[int(entry)] = argv.strip()[:200]
    return found


def git_env(home):
    return {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


class ContCase(unittest.TestCase):
    """A temporary project with an initialised ledger, a fake `worker`, and a dispatch config."""

    def setUp(self):
        if not L.fcntl or not os.path.exists("/proc/self/stat"):
            self.fail("these tests need fcntl and /proc (Linux)")
        reconcile.reset_acted()  # the flag is per process; a test starts a fresh "command"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.project = self.dir / "proj"
        self.project.mkdir()
        old = os.environ.get("PATH")
        os.environ["PATH"] = f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        self.addCleanup(self.assert_no_stragglers)  # runs after reap (LIFO), before the tmp cleanup
        self.addCleanup(self.reap)  # runs before the tmp cleanup (LIFO)
        self.fake("worker", "print('done')")
        self.cfg = self.config()
        code, out = self.cli("init", project=False, extra=["--project", str(self.project)])
        self.assertEqual(code, 0, out)
        self.state = Path(out["state"])

    # --- fixtures
    def fake(self, name, body="print('ok')"):
        path = self.bin / name
        path.write_text(FAKE.format(python=sys.executable, body=body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def config(self, **over):
        cfg = {"seats": {"w": {"harness": "local-command", "command": [str(self.bin / "worker")]}},
               "roles": {"researcher": ["w"], "builder": ["w"]}, "permit": ["researcher"]}
        cfg.update(over)
        self.cfg_count = getattr(self, "cfg_count", 0) + 1
        path = self.dir / f"dispatch{self.cfg_count}.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(path)

    def repo(self):
        repo = self.dir / "repo"
        repo.mkdir(exist_ok=True)
        home = self.dir / "home"
        home.mkdir(exist_ok=True)
        for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, env=git_env(home))
        return repo

    # --- running the CLI
    def cli(self, *args, project=True, extra=None, env=None, timeout=180):
        argv = [sys.executable, "-B", str(CLI), args[0], *(["--project", str(self.project)] if project else []),
                *args[1:], *(extra or [])]
        proc = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **(env or {})},
                              cwd=self.dir, timeout=timeout)
        self.stderr = proc.stderr
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            self.fail(f"non-JSON output: {proc.stdout!r} {proc.stderr!r}")

    def inproc(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = continuity.main([args[0], "--project", str(self.project), *args[1:]])
        return code, json.loads(buf.getvalue())

    def launch_args(self, role="researcher", intent="x", cfg=None):
        return ["--config", cfg or self.cfg, "--role", role, "--intent", intent, "--workdir", str(self.dir)]

    def start(self, *extra, **kw):
        return self.cli("start", *self.launch_args(**kw), *extra)

    def run_job(self, *extra, **kw):
        return self.cli("run", *self.launch_args(**kw), *extra)

    # --- library access
    def ctx(self):
        return jobs.Context(self.state)

    def doc(self):
        return self.ctx().ledger.read()

    def job(self, job_id):
        return self.doc()["jobs"][job_id]

    def edit(self, change):
        with self.ctx().ledger.transaction() as doc:
            change(doc)

    def ledger_bytes(self):
        return (self.state / "ledger.json").read_bytes()

    def banked(self, job_id, timeout=30):
        return wait_for(lambda: self.job(job_id)["state"] == "banked", timeout)

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def start_sleeper(self, **kw):
        """A background job whose worker sleeps; returns the job id once the worker runs."""
        self.fake("worker", "open(os.path.join(here, 'started'), 'w').write(str(os.getpid()))\ntime.sleep(60)")
        code, out = self.start(**kw)
        self.assertEqual(code, 0, out)
        job_id = out["job_id"]
        self.assertTrue(wait_for(lambda: (self.state / "runs" / job_id / "start.json").exists()
                                 and self.start_json(job_id).get("state") == "running"), "worker never ran")
        return job_id

    def start_json(self, job_id):
        try:
            return json.loads((self.state / "runs" / job_id / "start.json").read_text())
        except (OSError, ValueError):
            return {}

    def reap_proc(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

    def reap(self):
        """Kill whatever a test left running: dispatchers, worker groups, marked descendants."""
        try:
            doc = self.ctx().ledger.read()
        except Exception:  # noqa: BLE001 - a test may have broken the ledger on purpose
            doc = {"jobs": {}}
        for job_id, job in doc["jobs"].items():
            for pid in (job["dispatcher"] or {}).get("pid"), self.start_json(job_id).get("pid"):
                if pid:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGCONT)
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
            pgid = self.start_json(job_id).get("pgid")
            if pgid:
                with contextlib.suppress(OSError):
                    os.killpg(pgid, signal.SIGKILL)
            rd = self.state / "runs" / job_id
            if rd.exists():
                dispatch.sweep_marker(dispatch.job_marker(rd))
        self.kill_stragglers()

    def kill_stragglers(self, timeout=10):
        """SIGKILL anything still living in this test's directory that the job bookkeeping did not name
        (a fake `git` or `sleep` the dispatcher left behind when it was killed mid-call) and wait until it is gone."""
        end = time.time() + timeout
        while True:
            left = stragglers(self.dir)
            if not left or time.time() > end:
                return
            for pid in left:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            time.sleep(0.05)

    def assert_no_stragglers(self):
        """No live process of this test remains when it ends; checked before the temporary directory is removed."""
        left = stragglers(self.dir)
        self.assertEqual(left, {}, f"test left live processes behind: {left}")
