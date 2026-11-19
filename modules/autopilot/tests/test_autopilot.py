"""U16 acceptance A1..A12, A16 (autopilot half) plus the tribune amendments (A11b, A4e, A10 PATH seam).

Offline: a fake `claude` harness (marker AI_ARCH_FAKE_HARNESS) stands in for every model. Every child gets a built-from-
scratch environment and a PATH whose first entries are sentinels that log a call and exit 97. Nothing here starts a
scheduler entry, spends money or touches the network. tearDownClass restores os.environ and the socket patch because
tests/test_module_discovery.py loads this file into the root loop process.
"""

import getpass
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
FIX = HERE / "fixtures"
PY = sys.executable
CLI = MOD / "autopilot.py"
CONT = ROOT / "modules" / "continuity" / "continuity.py"
SANDBOX = ROOT / "modules" / "isolation" / "sandbox.py"
REQUIRE_SANDBOX = os.environ.get("AIARCH_REQUIRE_SANDBOX") == "1"
REQUIRE_RESOURCE = os.environ.get("AIARCH_REQUIRE_RESOURCE") == "1"
ORIG_ENV = dict(os.environ)
ORIG_HOME = os.environ.get("HOME", "")
ORIG_CONNECT = (socket.create_connection, socket.socket.connect)
SENTINELS = ("claude", "opencode", "npx", "npm", "curl", "wget", "crontab", "systemctl", "systemd-run", "paperclipai")
GIT = shutil.which("git") or "/usr/bin/git"
GITENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
DEFAULT_TOKENS = "100000"


def sandbox_available():
    r = subprocess.run([PY, "-B", str(SANDBOX), "probe"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    return r.returncode == 0


SANDBOX_OK = sandbox_available()


def _no_external(orig):
    def guard(*args, **kw):
        target = args[0] if args else None
        host = target[0] if isinstance(target, tuple) else target
        if isinstance(host, str) and host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"non-loopback network attempt: {host}")
        return orig(*args, **kw)
    return guard


def setUpModule():
    socket.create_connection = _no_external(ORIG_CONNECT[0])


def tearDownModule():
    socket.create_connection, socket.socket.connect = ORIG_CONNECT
    os.environ.clear()
    os.environ.update(ORIG_ENV)


def item(id_, **kw):
    f = {"verify": f"test -f out/{id_.lower()}.txt", "touches": f"out/{id_.lower()}.txt", "needs": "none", "gate": "auto",
         "stakes": "HIGH", "acceptance": f"out/{id_.lower()}.txt exists"}
    f.update(kw)
    return dict(f, id=id_)


def spec_text(items):
    out = ["# Demo spec", "", "## Items", ""]
    for it in items:
        out += [f"### {it['id']} — item {it['id']}", "", f"- What: make {it['id']}", f"- Acceptance: {it['acceptance']}",
                f"- Verify: `{it['verify']}`", f"- Stakes: {it['stakes']}", f"- Needs: {it['needs']}",
                f"- Touches: {it['touches']}", f"- Gate: {it['gate']}"]
        if it.get("blocked"):
            out.append(f"- Blocked-by: {it['blocked']}")
        if it.get("lineage"):
            out.append(f"- Lineage: {it['lineage']}")
        out.append("")
    return "\n".join(out)


def tracker_text(items, done=()):
    rows = ["# Demo tracker", "", "| Item | Lane | State | Evidence |", "|---|---|---|---|"]
    rows += [f"| {it['id']} | build | {'done' if it['id'] in done else 'pending'} | |" for it in items]
    return "\n".join(rows) + "\n"


def questions_text(open_ids=(), closed_ids=()):
    out = ["# Questions", ""]
    for q in open_ids:
        out += [f"## {q} — question", "", "Status: open", ""]
    for q in closed_ids:
        out += [f"## {q} — question", "", "Status: closed", ""]
    return "\n".join(out)


DEFAULT_ITEMS = [item("S1", verify="test -f out/s1.txt"),
                 item("S2", needs="S1", verify="test -f out/s1.txt && test -f out/s2.txt"),
                 item("S3", gate="human — scope", verify="true", touches="out/s3.txt")]
PRICES = {"unit": "usd_per_million_tokens", "models": {"fake-m": {"input": 1000, "output": 2000}}}


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


class Res:
    def __init__(self, rc, out, err):
        self.rc, self.err = rc, err
        self.text = out
        self.json = None
        for line in reversed(out.splitlines()):
            if line.startswith("{"):
                try:
                    self.json = json.loads(line)
                except ValueError:
                    pass
                break
        if self.json is None:
            try:
                self.json = json.loads(out)
            except ValueError:
                pass

    @property
    def reason(self):
        return (self.json or {}).get("reason")


class Base(unittest.TestCase):
    ALL_OUT = []

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        for name in ("home", "state", "config", "tmpdir", "sentinel", "fake-bin"):
            (self.tmp / name).mkdir()
        self.sent_log = self.tmp / "sentinel.log"
        for name in SENTINELS:
            p = self.tmp / "sentinel" / name
            p.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {self.sent_log}\nexit 97\n")
            p.chmod(0o755)
        shutil.copy(FIX / "claude", self.tmp / "fake-bin" / "claude")
        self.fb = self.tmp / "fake-bin"
        self.repo = self.tmp / "repo"
        self.children = []
        self.addCleanup(self.reap)
        self.items = list(DEFAULT_ITEMS)
        self.scenario = {"S1": {"writes": [{"path": "out/s1.txt"}]}, "S2": {"writes": [{"path": "out/s2.txt"}]}}

    def reap(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
                p.wait()
        base = self.tmp / "state" / "ai-architecture" / "autopilot"
        for pf in list(self.fb.glob("pids/*")) + list(base.glob("**/pgid")):
            try:
                for pid in pf.read_text().split()[:2]:
                    os.killpg(int(pid), signal.SIGKILL) if int(pid) > 1 else None
            except (OSError, ValueError, ProcessLookupError, PermissionError):
                pass
        for p in (self.tmp / "state").glob("**/runs/*/start.json"):
            try:
                os.killpg(json.loads(p.read_text())["pgid"], signal.SIGKILL)
            except (OSError, ValueError, KeyError, TypeError, ProcessLookupError, PermissionError):
                pass

    # --- environment ----------------------------------------------------------------------------------------------
    def env(self, fake=True, **extra):
        path = [str(self.tmp / "sentinel"), "/usr/bin", "/bin"]
        if fake:
            path.insert(0, str(self.fb))
        env = {"PATH": ":".join(path), "HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "state"),
               "XDG_CONFIG_HOME": str(self.tmp / "config"), "TMPDIR": str(self.tmp / "tmpdir"), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", **GITENV}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def git(self, *args, cwd=None):
        r = subprocess.run([GIT, "-c", "commit.gpgsign=false", *args], cwd=cwd or self.repo, capture_output=True, text=True, env=GITENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def make_repo(self, items=None, questions=None, done=(), plan="plans/demo"):
        items = items if items is not None else self.items
        self.repo.mkdir(exist_ok=True)
        self.git("init", "-q", "-b", "main")
        d = self.repo / plan
        d.mkdir(parents=True)
        (d / "SPEC.md").write_text(spec_text(items))
        (d / "TRACKER.md").write_text(tracker_text(items, done))
        if questions is not None:
            (d / "QUESTIONS.md").write_text(questions)
        (self.repo / "README.md").write_text("demo\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.plan = plan
        self.head0 = self.git("rev-parse", "HEAD")
        return self.repo

    def set_scenario(self, scenario=None):
        (self.fb / "scenario.json").write_text(json.dumps(scenario if scenario is not None else self.scenario))

    def prices(self):
        p = self.tmp / "prices.json"
        p.write_text(json.dumps(PRICES))
        return str(p)

    def ap(self, *args, env=None, timeout=240, cwd=None):
        r = subprocess.run([PY, "-B", str(CLI), *map(str, args)], capture_output=True, text=True, env=env or self.env(),
                           timeout=timeout, cwd=cwd or str(self.tmp), stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        res = Res(r.returncode, r.stdout, r.stderr)
        Base.ALL_OUT.append(r.stdout + r.stderr)
        return res

    def popen(self, *args, env=None):
        p = subprocess.Popen([PY, "-B", str(CLI), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env or self.env(), cwd=str(self.tmp), stdin=subprocess.DEVNULL)
        self.children.append(p)
        return p

    def kick_args(self, provider="fake", budget=DEFAULT_TOKENS, extra=()):
        a = ["kickoff", "--repo", self.repo, "--plan", self.plan, "--provider", provider]
        if provider != "offline":
            a += ["--fake-bin", self.fb, "--model", "fake-m"] if provider == "fake" else []
        if budget is not None:
            a += ["--budget-tokens", budget]
        a += list(extra)
        return a

    def kick(self, provider="fake", budget=DEFAULT_TOKENS, extra=(), **kw):
        return self.ap(*self.kick_args(provider, budget, extra), **kw)

    @property
    def plan_id(self):
        base = self.tmp / "state" / "ai-architecture" / "autopilot"
        dirs = sorted(base.iterdir()) if base.exists() else []
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0].name

    @property
    def sdir(self):
        return self.tmp / "state" / "ai-architecture" / "autopilot" / self.plan_id

    def status(self, **kw):
        return self.ap("status", "--json", "--plan-id", self.plan_id, **kw)

    def resume(self, *extra, **kw):
        return self.ap("resume", "--plan-id", self.plan_id, *extra, **kw)

    def dlog(self):
        p = self.fb / "dispatch.log"
        return [l.split() for l in p.read_text().splitlines()] if p.exists() else []

    def dcount(self, item_id=None):
        return len([l for l in self.dlog() if item_id is None or l[0] == item_id])

    def sentinel_calls(self):
        return self.sent_log.read_text().splitlines() if self.sent_log.exists() else []

    def runner_tip(self):
        return self.git("rev-parse", f"refs/heads/autopilot/{self.plan_id}")

    def runner_show(self, path):
        return self.git("show", f"autopilot/{self.plan_id}:{path}")

    def runner_commit(self, path, find, replace):
        w = self.sdir / "work"
        f = w / path
        f.write_text(f.read_text().replace(find, replace))
        self.git("add", "--", path, cwd=w)
        self.git("commit", "-q", "-m", "outside edit", cwd=w)

    def typed(self, args, text, env=None, ancestor=False):
        """Run autopilot with a pty as stdin and stdout and `text` typed at it."""
        master, slave = pty.openpty()
        cmd = [PY, "-B", str(CLI), *map(str, args)]
        p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=subprocess.PIPE, env=env or self.env(), close_fds=True,
                             cwd=str(self.tmp), start_new_session=True)
        os.close(slave)
        self.children.append(p)
        os.write(master, (text + "\n").encode())
        out = b""
        end = time.time() + 120
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif p.poll() is not None:
                break
        p.wait(timeout=60)
        os.close(master)
        err = p.stderr.read().decode()
        self.assertNotIn("Traceback", err, err)
        Base.ALL_OUT.append(out.decode("utf-8", "replace") + err)
        return Res(p.returncode, out.decode("utf-8", "replace").replace("\r", ""), err)

    def wait_for(self, path, timeout=60):
        self.assertTrue(CLI.exists(), "autopilot.py is missing")
        end = time.time() + timeout
        while time.time() < end:
            if Path(path).exists():
                return True
            time.sleep(0.05)
        self.fail(f"timed out waiting for {path}")

    def hold(self, item_id, **extra):
        gate = str(self.tmp / f"gate-{item_id}")
        self.scenario.setdefault(item_id, {})["hold"] = gate
        self.scenario[item_id].update(extra)
        return gate

    def release(self, gate):
        Path(gate).write_text("go")

    def baseline_ready(self):
        self.make_repo()
        self.set_scenario()


# --------------------------------------------------------------------------------------------------------------------
class A1Journey(Base):
    """A1 + A2 + A12 status schema. One baseline run is shared by the checks in this class."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inst = Base()
        cls.inst.setUp()
        t = cls.inst
        t.make_repo()
        t.set_scenario()
        t.branch0 = t.git("rev-parse", "refs/heads/main")
        t.res = t.kick()
        t.st = t.status()

    @classmethod
    def tearDownClass(cls):
        cls.inst.reap()
        cls.inst._tmp.cleanup()
        super().tearDownClass()

    def setUp(self):  # the shared instance owns the baseline; per-test setUp stays cheap
        self.base = type(self).inst

    def tearDown(self):
        pass

    def test_a1_journey(self):
        t = self.base
        self.assertEqual(t.res.rc, 0, t.res.err + t.res.text)
        self.assertEqual(t.res.json["stop_reason"], "waiting-human")
        self.assertEqual(t.dcount("S1"), 1)
        self.assertEqual(t.dcount("S2"), 1)
        self.assertEqual(t.dcount("S3"), 0)
        self.assertEqual(t.sentinel_calls(), [])
        st = t.st.json
        self.assertEqual(t.st.rc, 0, t.st.text)
        self.assertEqual(st["rounds"], 2)
        self.assertEqual(st["spent"]["tokens"], 300)
        self.assertEqual(st["stop_reason"], "waiting-human")
        self.assertEqual(t.git("rev-parse", "refs/heads/main"), t.branch0, "the user's branch moved")
        tip = t.runner_tip()
        self.assertNotEqual(tip, t.head0)
        self.assertEqual(t.git("show", f"{tip}:out/s2.txt"), "ok")
        self.assertIn("| S2 | build | done |", t.runner_show("plans/demo/TRACKER.md"))
        # S2's worker worktree contained S1's integrated file (its Verify needs both)
        rec = json.loads((t.sdir / "receipt.json").read_text())
        self.assertEqual([r["outcome"] for r in rec["rounds"]], ["accepted", "accepted"])
        self.assertTrue(all(r["touches_ok"] and r["verify_rc"] == 0 and r["sandbox"] for r in rec["rounds"]))

    def test_a1_status_schema(self):
        t = self.base
        sys.path.insert(0, str(ROOT))
        from modules.autopilot import state as S
        schema = json.loads((MOD / "schemas" / "status.json").read_text())
        S.check_shape(t.st.json, schema)
        schema = json.loads((MOD / "schemas" / "receipt.json").read_text())
        S.check_shape(json.loads((t.sdir / "receipt.json").read_text()), schema)
        schema = json.loads((MOD / "schemas" / "state.json").read_text())
        S.check_shape(json.loads((t.sdir / "state.json").read_text()), schema)

    def test_a16_stable_sha(self):
        sys.path.insert(0, str(ROOT))
        from modules.autopilot import autopilot as AP
        t = self.base
        rec = json.loads((t.sdir / "receipt.json").read_text())
        other = Base()
        other.setUp()
        try:
            other.make_repo()
            other.set_scenario()
            other.kick()
            rec2 = json.loads((other.sdir / "receipt.json").read_text())
        finally:
            other.reap()
            other._tmp.cleanup()
        self.assertEqual(AP.stable_sha256(rec), AP.stable_sha256(rec2))
        changed = json.loads(json.dumps(rec))
        changed["rounds"][0]["verify_rc"] = 7
        self.assertNotEqual(AP.stable_sha256(rec), AP.stable_sha256(changed))
        changed = json.loads(json.dumps(rec))
        changed["budget"]["spent"] = 999
        self.assertNotEqual(AP.stable_sha256(rec), AP.stable_sha256(changed))
        self.assertIn("job_id", AP.STABLE_EXCLUDE)


# --------------------------------------------------------------------------------------------------------------------
class A2OffByDefault(Base):
    def test_a2_import_and_help_and_refusals_start_nothing(self):
        for mod in ("autopilot", "spec", "state", "budget", "verify"):
            r = subprocess.run([PY, "-B", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import modules.autopilot.{mod}"],
                               capture_output=True, text=True, env=self.env())
            self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([PY, "-B", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import modules.control_plane_adapters"],
                           capture_output=True, text=True, env=self.env())
        self.assertNotEqual(r.returncode, 0)  # a hyphenated directory is not importable; the adapters are plain scripts
        self.assertEqual(self.sentinel_calls(), [])
        self.make_repo()
        self.assertEqual(self.ap("--help").rc, 0)
        self.assertEqual(self.ap("kickoff", "--help").rc, 0)
        r = self.ap("status", "--json", "--plan-id", "p-000000000000")
        self.assertEqual((r.rc, r.reason), (2, "no-state"))
        # a refused input (plan dir not tracked) creates no state dir and starts nothing
        (self.repo / "plans" / "other").mkdir()
        (self.repo / "plans" / "other" / "SPEC.md").write_text(spec_text(self.items))
        r = self.ap("kickoff", "--repo", self.repo, "--plan", "plans/other", "--provider", "offline")
        self.assertEqual((r.rc, r.reason), (2, "plan-untracked"))
        self.assertFalse((self.tmp / "state" / "ai-architecture").exists())
        self.assertEqual(self.sentinel_calls(), [])
        self.assertEqual(list((self.tmp / "config").rglob("*")), [])
        desc = json.loads((MOD / "descriptor.json").read_text())
        self.assertEqual(desc["claimed_support"], "planned")

    def test_a2_detach_creates_no_scheduler_entry_and_records_pid(self):
        quick = [item("S1", verify="true", touches="out/s1.txt")]
        self.make_repo(quick)
        self.set_scenario({})
        r = self.ap("kickoff", "--repo", self.repo, "--plan", self.plan, "--provider", "offline", "--detach")
        self.assertEqual(r.rc, 0, r.err + r.text)
        pid = r.json.get("detached_pid")
        self.assertIsInstance(pid, int)
        end = time.time() + 90
        while time.time() < end:
            st = self.status()
            if st.json and not st.json["controller"]["alive"] and st.json["stop_reason"]:
                break
            time.sleep(0.3)
        self.assertEqual(st.json["stop_reason"], "done", st.text)
        self.assertEqual(st.json["detached_pid"], pid)
        self.assertEqual(self.sentinel_calls(), [])
        self.assertEqual(list((self.tmp / "config").rglob("*")), [])
        self.assertEqual(list((self.tmp / "home").rglob("*.service")) + list((self.tmp / "home").rglob("*.timer")), [])


# --------------------------------------------------------------------------------------------------------------------
class A3Budgets(Base):
    def test_a3a_budget_exceeded_stops_before_s2(self):
        self.make_repo()
        self.scenario["S1"]["usage"] = {"input": 3000, "output": 2000, "cache_read": 0, "cache_write": 0}
        self.set_scenario()
        r = self.kick(budget="4000")
        self.assertEqual((r.rc, r.reason), (1, "budget"), r.text + r.err)
        self.assertEqual(self.dcount("S2"), 0)
        self.assertEqual(self.status().json["spent"]["tokens"], 5000)

    def test_a3b_below_reserve_starts_no_round(self):
        self.make_repo()
        self.set_scenario()
        r = self.kick(budget="500")
        self.assertEqual((r.rc, r.reason), (1, "budget"), r.text + r.err)
        self.assertEqual(self.dcount(), 0)

    def test_a3c_usd_budget(self):
        self.make_repo()
        self.set_scenario()
        r = self.kick(budget=None, extra=["--budget-usd", "1.0", "--prices", self.prices()])
        self.assertEqual((r.rc, r.reason), (0, "waiting-human"), r.text + r.err)
        st = self.status().json
        self.assertAlmostEqual(st["spent"]["usd"], 0.4, places=6)
        self.assertEqual(st["spent"]["tokens"], 300)

    def test_a3c_usd_exceeded(self):
        self.make_repo()
        self.set_scenario()
        r = self.kick(budget=None, extra=["--budget-usd", "0.3", "--prices", self.prices()])
        self.assertEqual((r.rc, r.reason), (1, "budget"), r.text + r.err)
        self.assertEqual(self.dcount("S2"), 1)  # S2 ran (0.2 left > 0), then 0.4 > 0.3
        self.assertAlmostEqual(self.status().json["spent"]["usd"], 0.4, places=6)

    def test_a3d_model_missing_from_price_table(self):
        self.make_repo()
        self.set_scenario()
        a = self.kick_args(budget=None, extra=["--budget-usd", "5", "--prices", self.prices()])
        a[a.index("--model") + 1] = "other-model"
        r = self.ap(*a)
        self.assertEqual((r.rc, r.reason), (1, "usage-unknown"), r.text + r.err)
        self.assertEqual(self.dcount("S1"), 1)
        self.assertNotIn("S1", [l[0] for l in []])
        self.assertEqual(self.runner_tip(), self.head0, "an unpriced round was accepted")

    def test_a3e_round_without_usage_is_not_accepted(self):
        self.make_repo()
        self.scenario["S1"]["usage"] = None
        self.set_scenario()
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (1, "usage-unknown"), r.text + r.err)
        self.assertEqual(self.runner_tip(), self.head0)
        self.assertNotIn("| S1 | build | done |", self.runner_show("plans/demo/TRACKER.md"))
        self.assertEqual(self.status().json["spent"]["unknown"], 1)

    def test_a3f_budgeted_offline_stops_unknown_at_round_one(self):
        self.make_repo([item("S1", verify="true")])
        self.set_scenario({})
        r = self.ap("kickoff", "--repo", self.repo, "--plan", self.plan, "--provider", "offline", "--budget-tokens", "50000")
        self.assertEqual((r.rc, r.reason), (1, "usage-unknown"), r.text + r.err)
        self.assertEqual(self.status().json["rounds"], 1)

    def test_a3g_bad_budgets(self):
        self.make_repo()
        self.set_scenario()
        r = self.kick(budget=None)
        self.assertEqual((r.rc, r.reason), (2, "budget-required"))
        r = self.ap("kickoff", "--repo", self.repo, "--plan", self.plan, "--provider", "fake", "--fake-bin", self.fb, "--model", "fake-m")
        self.assertEqual((r.rc, r.reason), (2, "budget-required"))
        for bad in ("nan", "inf", "0", "-5", "abc"):
            r = self.kick(budget=bad)
            self.assertEqual((r.rc, r.reason), (2, "budget-invalid"), (bad, r.text, r.err))
            r = self.kick(budget=None, extra=["--budget-usd", bad, "--prices", self.prices()])
            self.assertEqual((r.rc, r.reason), (2, "budget-invalid"), (bad, r.text, r.err))
        r = self.kick(budget=None, extra=["--budget-usd", "1"])
        self.assertEqual((r.rc, r.reason), (2, "prices-required"))
        self.assertEqual(self.dcount(), 0)
        self.assertFalse((self.tmp / "state" / "ai-architecture").exists())

    def test_a3h_spent_is_cumulative_across_resume(self):
        self.make_repo()
        self.scenario["S1"]["usage"] = {"input": 3000, "output": 2000, "cache_read": 0, "cache_write": 0}
        self.set_scenario()
        self.assertEqual(self.kick(budget="4000").reason, "budget")
        r = self.resume("--budget-tokens", "20000")
        self.assertEqual((r.rc, r.reason), (0, "waiting-human"), r.text + r.err)
        st = self.status().json
        self.assertEqual(st["spent"]["tokens"], 5150)
        self.assertEqual(st["rounds"], 2)


# --------------------------------------------------------------------------------------------------------------------
class A4Gates(Base):
    def test_a4a_human_gate_never_dispatched(self):
        self.items = [item("H1", gate="human — direction", verify="true"), item("S2")]
        self.make_repo()
        self.scenario = {"S2": {"writes": [{"path": "out/s2.txt"}]}}
        self.set_scenario()
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (0, "waiting-human"), r.text + r.err)
        self.assertEqual(self.dcount("H1"), 0)
        self.assertEqual(self.dcount("S2"), 1)

    def test_a4b_blocked_by_open_question_and_gate_changed_on_close(self):
        self.items = [item("B1", blocked="Q1", verify="true"), item("S2")]
        self.make_repo(questions=questions_text(open_ids=["Q1"]))
        self.scenario = {"S2": {"writes": [{"path": "out/s2.txt"}]}}
        self.set_scenario()
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (0, "waiting-human"), r.text + r.err)
        self.assertEqual(self.dcount("B1"), 0)
        n = self.dcount()
        self.runner_commit("plans/demo/QUESTIONS.md", "Status: open", "Status: closed")  # outside any round
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (1, "gate-changed"), r.text + r.err)
        self.assertEqual(self.dcount(), n)

    def _worker_edit(self, edits, expect, items=None):
        self.items = items or [item("S1"), item("S3", gate="human — scope", verify="true", touches="out/s3.txt")]
        self.make_repo(questions=questions_text(open_ids=["Q1"]))
        self.scenario = {"S1": {"writes": [{"path": "out/s1.txt"}], "edits": edits}}
        self.set_scenario()
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (1, expect), r.text + r.err)
        self.assertEqual(self.runner_tip(), self.head0, "a gate-editing round was integrated")
        self.assertNotIn("| S1 | build | done |", self.runner_show("plans/demo/TRACKER.md"))

    def test_a4c_worker_edits_gate_line(self):
        self._worker_edit([{"path": "plans/demo/SPEC.md", "find": "Gate: human — scope", "replace": "Gate: auto"}], "gate-changed")

    def test_a4c_worker_edits_questions(self):
        self._worker_edit([{"path": "plans/demo/QUESTIONS.md", "find": "Status: open", "replace": "Status: closed"}], "gate-changed")

    def test_a4c_worker_edits_acceptance(self):
        self._worker_edit([{"path": "plans/demo/SPEC.md", "find": "Verify: `test -f out/s1.txt`", "replace": "Verify: `true`"}], "verify-drift")

    def test_a4c_worker_lifts_blocked_by(self):
        items = [item("S1"), item("B1", blocked="Q1", verify="true", touches="out/b1.txt")]
        self._worker_edit([{"path": "plans/demo/SPEC.md", "find": "- Blocked-by: Q1\n", "replace": ""}], "gate-changed", items)

    def test_a4d_fix_round_cap_survives_resume_and_refreeze(self):
        self.items = [item("S1", verify="test -f out/s1.txt")]
        self.make_repo()
        self.scenario = {"S1": {}}  # the worker changes nothing: Verify fails every time
        self.set_scenario()
        r = self.kick(extra=["--max-rounds", "2"])
        self.assertEqual((r.rc, r.reason), (1, "budget"), r.text + r.err)
        self.assertEqual(self.status().json["fix_rounds"], {"S1": 2})
        r = self.typed(["refreeze", "--plan-id", self.plan_id], self.plan_id)
        self.assertEqual((r.rc, r.reason), (0, "refrozen"), r.text + r.err)
        self.assertEqual(self.status().json["fix_rounds"], {"S1": 2})
        r = self.resume("--max-rounds", "9")
        self.assertEqual((r.rc, r.reason), (1, "verify-failed"), r.text + r.err)
        self.assertEqual(self.dcount("S1"), 3)  # two fix rounds, then stop
        self.assertEqual(self.status().json["fix_rounds"], {"S1": 3})
        r = self.resume("--max-rounds", "20")
        self.assertEqual((r.rc, r.reason), (1, "verify-failed"))
        self.assertEqual(self.dcount("S1"), 3)

    def test_a4e_repair_item_shares_its_roots_budget(self):
        self.items = [item("S1"), item("S1a", needs="S1", lineage="S1", stakes="STANDARD", touches="out/s1a.txt",
                                       verify="test -f out/s1a.txt")]
        self.make_repo()
        self.scenario = {"S1": {"attempts": {"1": {}, "2": {"writes": [{"path": "out/s1.txt"}]}}}, "S1a": {}}
        self.set_scenario()
        r = self.kick()
        # S1: attempt 1 fails (fix round 1 of the root), attempt 2 passes; S1a fails: root count 2, then 3 > cap 2
        self.assertEqual((r.rc, r.reason), (1, "verify-failed"), r.text + r.err)
        self.assertEqual(self.dcount("S1a"), 2, "the repair item got a fresh budget")
        self.assertEqual(self.status().json["fix_rounds"], {"S1": 3})


# --------------------------------------------------------------------------------------------------------------------
class A5Freeze(Base):
    def test_a5a_drift_outside_a_round(self):
        for find, replace, label in (("- Touches: out/s2.txt", "- Touches: out/", "touches"),
                                     ("Acceptance: out/s2.txt exists", "Acceptance: anything", "acceptance"),
                                     ("- Verify: `test -f out/s1.txt && test -f out/s2.txt`", "- Verify: `true`", "verify")):
            with self.subTest(label):
                self.setUp()
                self.make_repo()
                self.set_scenario()
                r = self.kick(extra=["--max-rounds", "1"])
                self.assertEqual(r.reason, "budget", r.text + r.err)
                n = self.dcount()
                self.runner_commit("plans/demo/SPEC.md", find, replace)
                r = self.resume("--max-rounds", "9")
                self.assertEqual((r.rc, r.reason), (1, "verify-drift"), r.text + r.err)
                self.assertEqual(self.dcount(), n)
                self.reap()
                self._tmp.cleanup()

    def test_a5a_item_list_change_is_drift(self):
        self.make_repo()
        self.set_scenario()
        self.assertEqual(self.kick(extra=["--max-rounds", "1"]).reason, "budget")
        w = self.sdir / "work" / "plans/demo/SPEC.md"
        w.write_text(w.read_text() + "\n" + spec_text([item("S9")]).split("## Items\n")[1])
        self.git("add", "--", "plans/demo/SPEC.md", cwd=self.sdir / "work")
        self.git("commit", "-q", "-m", "add item", cwd=self.sdir / "work")
        r = self.resume("--max-rounds", "9")
        self.assertEqual((r.rc, r.reason), (1, "verify-drift"), r.text + r.err)

    def test_a5a_user_branch_moving_is_branch_moved(self):
        self.make_repo()
        self.set_scenario()
        self.assertEqual(self.kick(extra=["--max-rounds", "1"]).reason, "budget")
        (self.repo / "later.txt").write_text("x")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "user work")
        n = self.dcount()
        r = self.resume("--max-rounds", "9")
        self.assertEqual((r.rc, r.reason), (1, "branch-moved"), r.text + r.err)
        self.assertEqual(self.dcount(), n)

    def test_a5b_write_outside_touches(self):
        self.make_repo()
        self.scenario["S1"]["writes"] = [{"path": "out/s1.txt"}, {"path": "other/x.txt"}]
        self.set_scenario()
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (1, "touches-violation"), r.text + r.err)
        self.assertEqual(self.runner_tip(), self.head0)

    def test_a5c_nested_repo_and_untracked_plan(self):
        self.items = [item("S1", touches="nested/src/")]
        self.make_repo()
        (self.repo / ".gitignore").write_text("nested/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "ignore")
        (self.repo / "nested" / "src").mkdir(parents=True)
        subprocess.run([GIT, "init", "-q"], cwd=self.repo / "nested", env=GITENV, check=True)
        self.set_scenario()
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (2, "touches-outside-repo"), r.text + r.err)
        self.assertIn("nested/src/", r.json["message"])
        self.assertFalse((self.tmp / "state" / "ai-architecture").exists())
        self.assertEqual(self.dcount(), 0)
        (self.repo / "plans" / "u").mkdir()
        (self.repo / "plans" / "u" / "SPEC.md").write_text(spec_text([item("S1")]))
        r = self.ap("kickoff", "--repo", self.repo, "--plan", "plans/u", "--provider", "offline")
        self.assertEqual((r.rc, r.reason), (2, "plan-untracked"))

    def test_a5d_typed_confirmation(self):
        self.make_repo()
        self.set_scenario()
        self.assertEqual(self.kick(extra=["--max-rounds", "1"]).reason, "budget")
        pid = self.plan_id
        for cmd in ("refreeze", "clear-corrupt"):
            r = self.ap(cmd, "--plan-id", pid)
            self.assertEqual((r.rc, r.reason), (3, "tty-required"), (cmd, r.text, r.err))
            r = self.typed([cmd, "--plan-id", pid], "wrong-id")
            self.assertEqual((r.rc, r.reason), (3, "confirmation-mismatch"), (cmd, r.text, r.err))
        r = self.typed(["refreeze", "--plan-id", pid], pid)
        self.assertEqual((r.rc, r.reason), (0, "refrozen"), r.text + r.err)


# --------------------------------------------------------------------------------------------------------------------
class A6Corrupt(Base):
    CASES = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inst = Base()
        cls.inst.setUp()
        t = cls.inst
        t.make_repo()
        t.set_scenario()
        t.kick()
        cls.state_copy = t.tmp / "state-baseline"
        shutil.copytree(t.tmp / "state", cls.state_copy, symlinks=True)

    @classmethod
    def tearDownClass(cls):
        cls.inst.reap()
        cls.inst._tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.base = type(self).inst
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        shutil.copytree(self.state_copy, self.work / "state", symlinks=True)
        self.statedir = next((self.work / "state" / "ai-architecture" / "autopilot").iterdir())
        self.pid = self.statedir.name

    def tearDown(self):
        pass

    def run_cell(self, label, mutate):
        sf = self.statedir / "state.json"
        mutate(sf)
        t = self.base
        env = t.env(XDG_STATE_HOME=str(self.work / "state"))
        before = t.dcount()
        for cmd in (["resume", "--plan-id", self.pid], ["kickoff", "--repo", t.repo, "--plan", t.plan, "--provider", "fake",
                                                         "--fake-bin", t.fb, "--model", "fake-m", "--budget-tokens", "5000"]):
            r = t.ap(*cmd, env=env)
            self.assertEqual((r.rc, r.reason), (1, "state-corrupt"), (label, cmd[0], r.text, r.err))
        self.assertEqual(t.dcount(), before, f"{label}: a corrupt state spawned a worker")
        self.assertEqual(t.sentinel_calls(), [])
        r = t.ap("status", "--json", "--plan-id", self.pid, env=env)
        self.assertEqual((r.rc, r.reason), (1, "state-corrupt"), (label, r.text, r.err))
        self.assertTrue((self.statedir / "state-corrupt").exists())
        r = t.ap("clear-corrupt", "--plan-id", self.pid, env=env)
        self.assertEqual((r.rc, r.reason), (3, "tty-required"))

    def test_truncated(self):
        self.run_cell("truncated", lambda f: f.write_text(f.read_text()[:40]))

    def test_non_json(self):
        self.run_cell("non-json", lambda f: f.write_text("not json at all"))

    def test_duplicate_key(self):
        self.run_cell("dup", lambda f: f.write_text(f.read_text().replace('"schema"', '"schema": "x", "schema"', 1)))

    def test_nan(self):
        self.run_cell("nan", lambda f: f.write_text(f.read_text().replace('"rounds":', '"nan_field": NaN, "rounds":', 1)))

    def test_wrong_schema(self):
        def m(f):
            d = json.loads(f.read_text())
            d["schema"] = "something-else/9"
            f.write_text(json.dumps(d))
        self.run_cell("schema", m)

    def test_missing_field(self):
        def m(f):
            d = json.loads(f.read_text())
            del d["spent"]
            f.write_text(json.dumps(d))
        self.run_cell("missing", m)

    def test_oversize(self):
        self.run_cell("oversize", lambda f: f.write_text(" " * (3 * 1024 * 1024) + f.read_text()))

    def test_broken_chain(self):
        def m(f):
            d = json.loads(f.read_text())
            d["rounds"][0]["verify_rc"] = 99
            f.write_text(json.dumps(d))
        self.run_cell("chain", m)

    def test_symlinked_state_json(self):
        def m(f):
            real = f.with_name("real.json")
            real.write_text(f.read_text())
            f.unlink()
            f.symlink_to(real)
        self.run_cell("symlink", m)

    def test_foreign_owner_seam(self):
        sys.path.insert(0, str(ROOT))
        from unittest import mock
        from modules.autopilot import state as S
        with mock.patch.object(S, "expected_uid", lambda: os.geteuid() + 1):
            with self.assertRaises(S.Corrupt) as cm:
                S.load(self.statedir)
        self.assertIn("owner", str(cm.exception))


# --------------------------------------------------------------------------------------------------------------------
class A7Ownership(Base):
    def test_a7_double_kickoff_exactly_one_controller(self):
        self.make_repo()
        gate = self.hold("S1")
        self.set_scenario()
        barrier = threading.Barrier(2)
        procs = []

        def go():
            barrier.wait()
            procs.append(self.popen(*self.kick_args()))

        threads = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        end = time.time() + 60
        while time.time() < end and not any(p.poll() is not None for p in procs):
            time.sleep(0.05)
        done = [p for p in procs if p.poll() is not None]
        self.assertEqual(len(done), 1, "exactly one kickoff must be refused while the other holds the lock")
        loser = done[0]
        out = loser.stdout.read()
        self.assertEqual(loser.returncode, 3, out + loser.stderr.read())
        self.assertEqual(json.loads(out.splitlines()[-1])["reason"], "busy")
        self.wait_for(gate + ".started")
        self.assertEqual(self.dcount("S1"), 1)
        self.release(gate)
        winner = next(p for p in procs if p is not loser)
        winner.wait(timeout=120)
        self.assertEqual(winner.returncode, 0, winner.stderr.read())
        self.assertEqual(self.dcount("S1"), 1)

    def _killed_controller(self):
        self.make_repo([item("S1")])
        self.scenario = {"S1": {"writes": [{"path": "out/s1.txt"}]}}
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        return gate, p

    def test_a7_stale_owner_adopted_after_reconcile(self):
        gate, p = self._killed_controller()
        st = self.status()
        self.assertEqual(st.json["controller"]["alive"], False, st.text)
        self.assertEqual(st.json["state"], "interrupted")
        self.release(gate)
        self.wait_for(next(self.sdir.glob("work/.ai-continuity/ledger/runs/*")) / "result.json")
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (0, "done"), r.text + r.err)
        self.assertTrue(r.json["adopted_stale_owner"])
        self.assertTrue(r.json["reconciled_before_adopt"])
        self.assertEqual(self.dcount("S1"), 1)

    def test_a7_liveness_needs_ticks_and_boot_id(self):
        gate, p = self._killed_controller()
        sleeper = subprocess.Popen(["sleep", "300"])
        self.children.append(sleeper)
        sf = self.sdir / "state.json"
        d = json.loads(sf.read_text())
        d["owner"] = {"pid": sleeper.pid, "start_ticks": 1, "boot_id": d["owner"]["boot_id"], "kickoff_id": d["owner"]["kickoff_id"]}
        sf.write_text(json.dumps(d))
        self.assertFalse(self.status().json["controller"]["alive"], "same pid with other start ticks is not the controller")
        r = self.ap("stop", "--plan-id", self.plan_id)
        self.assertEqual((r.rc, r.reason), (0, "not-running"), r.text + r.err)
        self.assertTrue(alive(sleeper.pid), "stop signalled a process whose start ticks differ")
        d["owner"]["start_ticks"] = int(Path(f"/proc/{sleeper.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        d["owner"]["boot_id"] = "00000000-0000-0000-0000-000000000000"
        sf.write_text(json.dumps(d))
        self.assertFalse(self.status().json["controller"]["alive"], "a different boot id is stale")
        r = self.ap("stop", "--plan-id", self.plan_id)
        self.assertEqual(r.reason, "not-running")
        self.assertTrue(alive(sleeper.pid))
        self.release(gate)


# --------------------------------------------------------------------------------------------------------------------
class A8Interruption(Base):
    PITEMS = [item("P1"), item("P2"), item("P3")]

    def pack3(self):
        self.items = self.PITEMS
        self.make_repo()
        self.scenario = {k: {"writes": [{"path": f"out/{k.lower()}.txt"}]} for k in ("P1", "P2", "P3")}

    def pids(self, name):
        out = []
        for f in self.fb.glob(f"pids/{name}*"):
            out += [int(x) for x in f.read_text().split()[:1]]
        return out

    def test_a8a_stop_during_round_drains_and_cancels(self):
        self.make_repo([item("S1")])
        self.scenario = {"S1": {"writes": [{"path": "out/s1.txt"}], "spawn_child": True}}
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        r = self.ap("stop", "--plan-id", self.plan_id)
        self.assertEqual((r.rc, r.reason), (0, "stopped"), r.text + r.err)
        p.wait(timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.read())
        for pid in self.pids("S1"):
            self.assertFalse(alive(pid), f"worker process {pid} survived stop")
        st = self.status().json
        self.assertEqual(st["stop_reason"], "stopped")
        rec = json.loads((self.sdir / "receipt.json").read_text())
        self.assertEqual([x["outcome"] for x in rec["rounds"]], ["cancelled"])
        c = subprocess.run([PY, "-B", str(CONT), "status", "--project", str(self.sdir / "work")], capture_output=True, text=True, env=self.env())
        jobs = json.loads(c.stdout)["jobs"]
        self.assertEqual([j["cause"] for j in jobs.values()], ["cancelled"])
        self.assertEqual(self.runner_tip(), self.head0)

    def test_a8b_pause_banks_running_and_queues_the_rest(self):
        self.pack3()
        g1, g2 = self.hold("P1"), self.hold("P2")
        self.set_scenario()
        p = self.popen(*self.kick_args(extra=["--max-workers", "2"]))
        self.wait_for(g1 + ".started")
        self.wait_for(g2 + ".started")
        r = self.ap("pause", "--plan-id", self.plan_id)
        self.assertEqual(r.rc, 0, r.text + r.err)
        self.release(g1)
        self.release(g2)
        p.wait(timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.read())
        st = self.status().json
        self.assertEqual(st["stop_reason"], "paused")
        self.assertEqual(st["queued"], ["P3"])
        self.assertEqual(self.dcount("P3"), 0)
        self.assertEqual(sorted(st["items"]["done"]), ["P1", "P2"])
        r = self.resume("--max-workers", "2")
        self.assertEqual((r.rc, r.reason), (0, "done"), r.text + r.err)
        self.assertEqual(self.dcount("P3"), 1)
        self.assertEqual(self.dcount("P1"), 1)
        for name in ("P1", "P2", "P3"):
            self.assertIn(f"| {name} | build | done |", self.runner_show("plans/demo/TRACKER.md"))

    def test_a8b_stop_with_two_workers_and_a_queue(self):
        self.pack3()
        g1, g2 = self.hold("P1", spawn_child=True), self.hold("P2", spawn_child=True)
        self.set_scenario()
        p = self.popen(*self.kick_args(extra=["--max-workers", "2"]))
        self.wait_for(g1 + ".started")
        self.wait_for(g2 + ".started")
        r = self.ap("stop", "--plan-id", self.plan_id)
        self.assertEqual((r.rc, r.reason), (0, "stopped"), r.text + r.err)
        p.wait(timeout=120)
        for pid in self.pids("P1") + self.pids("P2"):
            self.assertFalse(alive(pid), f"orphan {pid}")
        st = self.status().json
        self.assertEqual(st["queued"], ["P3"])
        self.assertEqual(self.dcount("P3"), 0)
        rec = json.loads((self.sdir / "receipt.json").read_text())
        self.assertEqual(sorted(x["outcome"] for x in rec["rounds"]), ["cancelled", "cancelled"])
        self.release(g1)
        self.release(g2)
        r = self.resume("--max-workers", "2")
        self.assertEqual((r.rc, r.reason), (0, "done"), r.text + r.err)
        self.assertEqual(self.dcount("P3"), 1, "the queued item must launch exactly once")

    def test_a8c_stop_without_controller(self):
        self.make_repo()
        self.set_scenario()
        self.kick()
        r = self.ap("stop", "--plan-id", self.plan_id)
        self.assertEqual((r.rc, r.reason), (0, "not-running"), r.text + r.err)


# --------------------------------------------------------------------------------------------------------------------
class A9Resume(Base):
    def test_a9_killed_round_is_banked_once_and_not_rerun(self):
        self.make_repo([item("S1")])
        self.scenario = {"S1": {"writes": [{"path": "out/s1.txt"}]}}
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        self.release(gate)
        self.wait_for(next(self.sdir.glob("work/.ai-continuity/ledger/runs/*")) / "result.json")
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (0, "done"), r.text + r.err)
        self.assertEqual(self.dcount("S1"), 1, "the completed round was rerun")
        st = self.status().json
        self.assertEqual(st["rounds"], 1)
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (0, "done"))
        self.assertEqual(self.dcount("S1"), 1)
        self.assertEqual(self.status().json["rounds"], 1)
        sid = f"cli:autopilot-{self.plan_id}"
        c = subprocess.run([PY, "-B", str(CONT), "resume", "consume", "--project", str(self.sdir / "work"), "--session", sid,
                            "--cwd", str(self.sdir / "work"), "--source", "resume"], capture_output=True, text=True, env=self.env())
        out = json.loads(c.stdout)
        self.assertFalse(out["delivered"], "the round's Next was not consumed by the runner's resume")
        self.assertIn(f"autopilot {self.plan_id} round 1", out["next"])


# --------------------------------------------------------------------------------------------------------------------
class A10Isolation(Base):
    def one_round(self, verify, extra_env=None, writes=True, args=()):
        self.items = [item("S1", verify=verify)]
        self.make_repo()
        self.scenario = {"S1": {"writes": [{"path": "out/s1.txt"}] if writes else []}}
        self.set_scenario()
        env = self.env(**(extra_env or {}))
        return self.ap(*self.kick_args(extra=["--max-rounds", "1", *args]), env=env)

    def rounds(self):
        return json.loads((self.sdir / "receipt.json").read_text())["rounds"]

    @unittest.skipUnless(SANDBOX_OK or REQUIRE_SANDBOX, "sandbox.py probe: bwrap unavailable")
    def test_a10_write_outside_rw_fails(self):
        r = self.one_round("echo x > /usr/ap-canary")
        self.assertEqual(r.reason, "budget", r.text + r.err)
        self.assertNotEqual(self.rounds()[0]["verify_rc"], 0)
        self.assertFalse(Path("/usr/ap-canary").exists())

    @unittest.skipUnless(SANDBOX_OK or REQUIRE_SANDBOX, "sandbox.py probe: bwrap unavailable")
    def test_a10_tcp_fails(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        srv.settimeout(0.5)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        r = self.one_round(f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 3)\"")
        self.assertEqual(r.reason, "budget", r.text + r.err)
        self.assertNotEqual(self.rounds()[0]["verify_rc"], 0)
        with self.assertRaises(socket.timeout):
            srv.accept()

    @unittest.skipUnless(SANDBOX_OK or REQUIRE_SANDBOX, "sandbox.py probe: bwrap unavailable")
    def test_a10_hooks_and_filters_do_not_run(self):
        canary = self.tmp / "canary-hook"
        self.items = [item("S1", verify="true", touches="out/,.gitattributes")]
        self.make_repo()
        hooks = self.repo / ".git" / "hooks"
        for name in ("post-checkout", "post-commit", "pre-commit", "post-merge", "reference-transaction"):
            (hooks / name).write_text(f"#!/bin/sh\ntouch {canary}\n")
            (hooks / name).chmod(0o755)
        self.scenario = {"S1": {"writes": [{"path": ".gitattributes", "content": "out/* filter=evil\n"}, {"path": "out/s1.txt"}]}}
        self.set_scenario()
        self.git("config", "filter.evil.smudge", f"touch {canary}-filter; cat")
        r = self.ap(*self.kick_args(extra=["--max-rounds", "1"]))
        self.assertIn(r.reason, ("done", "waiting-human", "budget"), r.text + r.err)
        self.assertFalse(canary.exists(), "a git hook ran")
        self.assertFalse(Path(f"{canary}-filter").exists(), "a smudge filter ran while materialising the verify tree")

    def test_a10_no_bwrap_is_unverifiable_and_never_unsandboxed(self):
        empty = self.tmp / "empty-path"
        empty.mkdir()
        canary = self.tmp / "canary-unsandboxed"
        r = self.one_round(f"touch {canary}", extra_env={"AIARCH_AUTOPILOT_SANDBOX_PATH": str(empty)})
        self.assertEqual((r.rc, r.reason), (1, "unverifiable"), r.text + r.err)
        self.assertFalse(canary.exists(), "Verify ran without a sandbox")
        self.assertEqual(self.dcount("S1"), 1, "the worker round must have completed first")
        self.assertEqual(self.runner_tip(), self.head0)


# --------------------------------------------------------------------------------------------------------------------
class A11LiveNeedsAHuman(Base):
    def live_args(self, *extra, spend=True, budget=True, model=True):
        a = ["kickoff", "--repo", self.repo, "--plan", self.plan, "--provider", "live"]
        if spend:
            a.append("--i-accept-spend")
        if budget:
            a += ["--budget-usd", "1", "--prices", self.prices()]
        if model:
            a += ["--model", "fake-m"]
        return a + list(extra)

    def setUp(self):
        super().setUp()
        self.make_repo()
        self.set_scenario()

    def assert_untouched(self, label):
        self.assertEqual(self.sentinel_calls(), [], label)
        self.assertEqual(self.dcount(), 0, label)
        self.assertFalse((self.tmp / "state" / "ai-architecture").exists(), label)

    def test_a11_refusals(self):
        r = self.ap(*self.live_args(spend=False), env=self.env(fake=False))
        self.assertEqual((r.rc, r.reason), (2, "spend-not-accepted"), r.text + r.err)
        r = self.ap(*self.live_args(budget=False), env=self.env(fake=False))
        self.assertEqual((r.rc, r.reason), (2, "budget-required"), r.text + r.err)
        r = self.ap(*self.live_args(), env=self.env(fake=False))  # stdin is not a TTY
        self.assertEqual((r.rc, r.reason), (3, "tty-required"), r.text + r.err)
        for var in ("CLAUDECODE", "AI_ARCH_JOB_MARKER", "CLAUDE_CODE_ENTRYPOINT", "OPENCODE_X"):
            r = self.typed(self.live_args(), self.plan_id_for(), env=self.env(fake=False, **{var: "1"}))
            self.assertEqual((r.rc, r.reason), (3, "agent-environment"), (var, r.text, r.err))
        r = self.typed(self.live_args(), "not-the-plan-id", env=self.env(fake=False))
        self.assertEqual((r.rc, r.reason), (3, "confirmation-mismatch"), r.text + r.err)
        self.assert_untouched("live refusals")

    def plan_id_for(self):
        sys.path.insert(0, str(ROOT))
        from modules.autopilot import autopilot as AP
        return AP.plan_id_of(str(self.repo), self.plan)

    def test_a11_binary_with_the_fake_marker_is_refused_as_live(self):
        env = self.env(fake=True)  # PATH has the marked fake claude first
        r = self.typed(self.live_args(), self.plan_id_for(), env=env)
        self.assertEqual((r.rc, r.reason), (2, "fake-binary-as-live"), r.text + r.err)
        self.assert_untouched("marker")

    def test_a11_agent_ancestor_is_refused(self):
        # the ancestor must carry /proc comm "claude": a symlinked interpreter named claude, not a script whose comm is python3
        wrapper = self.tmp / "ancestor" / "claude"
        wrapper.parent.mkdir()
        script = self.tmp / "ancestor" / "wrap.py"
        os.symlink(PY, wrapper)
        script.write_text(
            "import os, pty, subprocess, sys, select, time\n"
            "master, slave = pty.openpty()\n"
            "p = subprocess.Popen(sys.argv[1:], stdin=slave, stdout=slave, close_fds=True)\n"
            "os.close(slave)\nos.write(master, (os.environ['TYPED'] + '\\n').encode())\nout = b''\n"
            "while True:\n    r, _, _ = select.select([master], [], [], 0.2)\n"
            "    if r:\n        try:\n            c = os.read(master, 65536)\n        except OSError:\n            break\n"
            "        if not c: break\n        out += c\n    elif p.poll() is not None: break\n"
            "p.wait()\nsys.stdout.write(out.decode().replace('\\r', ''))\nsys.exit(p.returncode)\n")
        env = self.env(fake=False, TYPED=self.plan_id_for())
        r = subprocess.run([str(wrapper), "-B", str(script), PY, "-B", str(CLI), *map(str, self.live_args())], capture_output=True, text=True, env=env,
                           cwd=str(self.tmp), stdin=subprocess.DEVNULL)
        res = Res(r.returncode, r.stdout, r.stderr)
        self.assertEqual((res.rc, res.reason), (3, "agent-ancestor"), r.stdout + r.stderr)
        self.assert_untouched("ancestor")

    # A11b: the fake path is the easiest route to unattended spend
    def test_a11b_unmarked_fake_claude(self):
        (self.fb / "claude").write_text("#!/bin/sh\necho real-looking\n")
        r = self.kick()
        self.assertEqual((r.rc, r.reason), (2, "fake-binary-unmarked"), r.text + r.err)
        self.assert_untouched("unmarked")

    def test_a11b_fake_bin_resolves_elsewhere_on_path(self):
        env = self.env(fake=False)  # sentinel claude first, fake-bin not on PATH at all
        r = self.ap(*self.kick_args(), env=env)
        self.assertEqual((r.rc, r.reason), (2, "fake-binary-shadowed"), r.text + r.err)
        self.assert_untouched("shadowed")

    def test_a11b_pin_changes_before_round_two_and_on_resume(self):
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        claude = self.fb / "claude"
        claude.write_text(claude.read_text() + "\n# swapped\n")
        self.release(gate)
        p.wait(timeout=120)
        out = p.stdout.read()
        self.assertEqual(p.returncode, 2, out + p.stderr.read())
        self.assertEqual(json.loads(out.splitlines()[-1])["reason"], "fake-binary-changed")
        n = self.dcount()
        self.assertEqual(self.sentinel_calls(), [])
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (2, "fake-binary-changed"), r.text + r.err)
        self.assertEqual(self.dcount(), n)
        self.assertEqual(self.sentinel_calls(), [])


# --------------------------------------------------------------------------------------------------------------------
class A12StatusPauseStop(Base):
    def snapshot(self):
        out = {}
        for f in sorted(self.sdir.iterdir()):
            if f.is_file():
                st = f.stat()
                out[f.name] = (st.st_mtime_ns, st.st_size, f.read_bytes() if f.stat().st_size < 1 << 20 else b"")
        return out

    def test_a12_status_is_read_only_and_lockless(self):
        self.make_repo()
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        time.sleep(1.5)
        before = self.snapshot()
        r = self.status()
        self.assertEqual((r.rc, r.json["state"], r.json["controller"]["alive"]), (0, "running", True), r.text + r.err)
        self.assertEqual(self.snapshot(), before, "status wrote into the state dir")
        self.assertNotIn(str(self.tmp), r.text)
        self.assertFalse(re.search(r"(?<![\w.:-])/(?:tmp|home|var|usr|run)\b", r.text), r.text)
        t = self.ap("status", "--plan-id", self.plan_id)  # human text form is also lockless
        self.assertEqual(t.rc, 0, t.text + t.err)
        self.release(gate)
        p.wait(timeout=120)

    def test_a12_pause_finishes_the_round_then_resume_continues(self):
        self.make_repo()
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        r = self.ap("pause", "--plan-id", self.plan_id)
        self.assertEqual(r.rc, 0, r.text + r.err)
        self.release(gate)
        p.wait(timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.read())
        st = self.status().json
        self.assertEqual(st["stop_reason"], "paused")
        self.assertEqual(self.dcount("S2"), 0)
        self.assertEqual(st["rounds"], 1)
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (0, "waiting-human"), r.text + r.err)
        self.assertEqual(self.dcount("S2"), 1)
        self.assertEqual(self.dcount("S1"), 1)


# --------------------------------------------------------------------------------------------------------------------
class A16Constraints(Base):
    def test_a16_descriptor_provenance_privacy_static(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT))
        import check_collection as cc
        import privacy_scan as ps
        from modules.contracts import contracts as C
        d = C.check_module_descriptor(MOD, module="autopilot")
        self.assertEqual(d["claimed_support"], "planned")
        self.assertEqual({x["identifier"] for x in d["dependencies"]},
                         {"core-contracts", "continuity", "orchestration", "isolation", "observability"})
        self.assertTrue(all(x["status"] == "declared-untested" for x in d["dependencies"]))
        cc.provenance(MOD / "PROVENANCE.md")
        units = [l for l in (MOD / "PROVENANCE.md").read_text().splitlines() if l.startswith("## ") and not re.match(r"## (Deltas|Not ported|Notes)", l)]
        self.assertGreaterEqual(len(units), 6)
        self.assertEqual(ps.scan(MOD), [])
        banned = re.compile(r"urllib\.request|http\.client|\bssl\b|\brequests\b|socket\.create_connection|\bnpx\b|\bnpm |pip install")
        for f in MOD.glob("*.py"):
            self.assertIsNone(banned.search(f.read_text()), f.name)
        ls = subprocess.run([GIT, "ls-files", "-co", "--exclude-standard", "--", str(MOD)], cwd=ROOT, capture_output=True, text=True).stdout.split()
        for name in ls:
            self.assertNotIn(b"\0", (ROOT / name).read_bytes(), f"{name} contains NUL (privacy_scan skips it)")

    def test_a16_outputs_carry_no_path_user_or_host(self):
        self.make_repo()
        self.set_scenario()
        outs = []
        r = self.kick()
        outs.append(r.text + r.err)
        outs.append(self.status().text)
        outs.append(self.ap("status", "--plan-id", self.plan_id).text)
        outs.append((self.sdir / "receipt.json").read_text())
        outs.append(self.ap("kickoff", "--repo", self.repo, "--plan", "plans/nope", "--provider", "offline").text)
        outs.append(self.resume().text)
        joined = "\n".join(outs)
        for secret in (str(self.sdir), str(self.sdir.parent.parent.parent), str(self.repo), ORIG_HOME, getpass.getuser(), socket.gethostname()):
            if len(secret) > 2:
                self.assertNotIn(secret, joined, secret)


# --------------------------------------------------------------------------------------------------------------------
class F1Round1Fixes(Base):
    """Fix round 1: S1 hidden controller, S2 resource limits, F1 unknown-usage rounds."""
    NONCE_ENV = "AIARCH_AUTOPILOT_LAUNCH_NONCE"

    def paused_run(self, **kick):
        self.make_repo()
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args(**kick))
        self.wait_for(gate + ".started")
        self.assertEqual(self.ap("pause", "--plan-id", self.plan_id).rc, 0)
        self.release(gate)
        p.wait(timeout=120)
        self.assertEqual(self.status().json["stop_reason"], "paused")
        self.assertEqual(self.dcount(), 1)
        (self.sdir / "control.json").unlink()  # what a resume would clear: the pause request

    def plant_nonce(self, nonce):
        sf = self.sdir / "state.json"
        d = json.loads(sf.read_text())
        d["launch"] = {"nonce_sha": __import__("hashlib").sha256(nonce.encode()).hexdigest()}
        sf.write_text(json.dumps(d))

    def test_s1_hidden_controller_without_the_launch_nonce_is_refused(self):
        self.paused_run()
        rounds = self.status().json["rounds"]
        for env in (self.env(), self.env(**{self.NONCE_ENV: "0" * 32})):
            r = self.ap("_controller", "--plan-id", self.plan_id, "--resumed", env=env)
            self.assertEqual((r.rc, r.reason), (3, "controller-not-authorised"), r.text + r.err)
            self.assertEqual(self.dcount(), 1, "the hidden controller dispatched a worker")
            self.assertEqual(self.status().json["rounds"], rounds)

    def test_s1_launch_nonce_is_single_use(self):
        self.paused_run()
        self.plant_nonce("n" * 32)
        env = self.env(**{self.NONCE_ENV: "n" * 32})
        r = self.ap("_controller", "--plan-id", self.plan_id, "--resumed", env=env)
        self.assertEqual(r.rc, 0, r.text + r.err)  # the launcher's nonce is accepted once
        n = self.dcount()
        self.assertGreater(n, 1)
        self.assertNotIn("nonce", (self.sdir / "state.json").read_text().replace("nonce_sha", ""))
        r = self.ap("_controller", "--plan-id", self.plan_id, "--resumed", env=env)
        self.assertEqual((r.rc, r.reason), (3, "controller-not-authorised"), r.text + r.err)
        self.assertEqual(self.dcount(), n)

    def test_s1_live_controller_rechecks_the_harness(self):
        self.paused_run()
        sf = self.sdir / "state.json"
        d = json.loads(sf.read_text())
        d["provider"] = "live"
        sf.write_text(json.dumps(d))
        self.plant_nonce("n" * 32)
        r = self.ap("_controller", "--plan-id", self.plan_id, "--resumed", env=self.env(**{self.NONCE_ENV: "n" * 32}))  # PATH: marked fake first
        self.assertEqual((r.rc, r.reason), (2, "fake-binary-as-live"), r.text + r.err)
        self.assertEqual(self.dcount(), 1)

    def test_s2_resource_limits_without_detach_is_refused(self):
        self.make_repo()
        self.set_scenario()
        limits = self.tmp / "limits.json"
        limits.write_text(json.dumps({"backend": "rlimit", "address_space_mb": 4096}))
        r = self.kick(extra=["--resource-limits", limits])
        self.assertEqual((r.rc, r.reason), (2, "resource-limits-need-detach"), r.text + r.err)
        self.assertEqual(self.dcount(), 0)
        self.assertFalse((self.tmp / "state" / "ai-architecture").exists())

    def test_s2_foreground_resume_of_a_limited_state_is_refused(self):
        self.paused_run()
        sf = self.sdir / "state.json"
        d = json.loads(sf.read_text())
        d["limits"]["resource_limits"] = str(self.tmp / "limits.json")
        sf.write_text(json.dumps(d))
        r = self.resume()
        self.assertEqual((r.rc, r.reason), (2, "resource-limits-need-detach"), r.text + r.err)
        self.assertEqual(self.dcount(), 1)
        self.assertEqual(self.status().json["stop_reason"], "paused")

    def cancelled_run(self, **kick):
        """Cancel the running job through continuity (not through autopilot stop): the controller banks it cancelled."""
        self.make_repo([item("S1"), item("S2", needs="S1", verify="test -f out/s2.txt")])
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args(**kick))
        self.wait_for(gate + ".started")
        job = next(self.sdir.glob("work/.ai-continuity/ledger/runs/*")).name
        c = subprocess.run([PY, "-B", str(CONT), "cancel", "--project", str(self.sdir / "work"), "--job", job], capture_output=True, text=True,
                           env=self.env())
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.release(gate)  # a run that wrongly continues must not hang on the held retry
        out, err = p.communicate(timeout=120)
        self.assertNotIn("Traceback", err)
        return p.returncode, json.loads(out.splitlines()[-1])

    def test_f1_cancelled_round_under_a_token_budget_stops_usage_unknown(self):
        rc, out = self.cancelled_run(budget="100000")
        self.assertEqual((rc, out["reason"]), (1, "usage-unknown"), out)
        st = self.status().json
        self.assertEqual(self.dcount(), 1, "a cancelled round let the run dispatch again")
        self.assertGreaterEqual(st["spent"]["tokens"], 100000, "the reserve must cover the per-round cap (the whole remaining budget)")
        self.assertEqual(st["spent"]["unknown"], 1)
        rec = json.loads((self.sdir / "receipt.json").read_text())
        self.assertEqual([x["outcome"] for x in rec["rounds"]], ["cancelled"])

    def test_f1_a_busy_ledger_during_a_round_is_not_a_banked_round(self):
        """`wait` exit 3 (ledger busy) is transient: the round keeps waiting and is banked as what it really was."""
        import fcntl
        self.make_repo([item("S1")])
        gate = self.hold("S1")
        self.set_scenario()
        p = self.popen(*self.kick_args())
        self.wait_for(gate + ".started")
        state = self.sdir / "work" / ".ai-continuity" / "ledger"
        cfg = json.loads((state / "config.json").read_text())
        tmp = state / "config.json.tmp"
        tmp.write_text(json.dumps(dict(cfg, lock_wait_seconds=1)))
        os.replace(tmp, state / "config.json")  # atomic: a polling `wait` never reads a half-written config
        time.sleep(2)  # a `wait` started under the old 10 s lock wait has returned: every call from here on gives up after 1 s
        fd = os.open(state / "ledger.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            time.sleep(4)  # every `wait` of the controller now exits 3 after 1 s
        finally:
            os.close(fd)
        job = next(state.glob("runs/*")).name
        c = subprocess.run([PY, "-B", str(CONT), "cancel", "--project", str(self.sdir / "work"), "--job", job], capture_output=True,
                           text=True, env=self.env())
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.release(gate)
        p.communicate(timeout=120)
        rec = json.loads((self.sdir / "receipt.json").read_text())
        self.assertEqual([x["outcome"] for x in rec["rounds"]], ["cancelled"])

    def test_f1_cancelled_round_under_a_usd_budget_stops_usage_unknown(self):
        rc, out = self.cancelled_run(budget=None, extra=["--budget-usd", "5", "--prices", self.prices()])
        self.assertEqual((rc, out["reason"]), (1, "usage-unknown"), out)
        self.assertEqual(self.dcount(), 1, "a $0 cancelled round let the run continue")
        self.assertEqual(self.status().json["spent"]["unknown"], 1)

    def pack3(self):
        self.items = [item("P1"), item("P2"), item("P3")]
        self.make_repo()
        self.scenario = {k: {"writes": [{"path": f"out/{k.lower()}.txt"}]} for k in ("P1", "P2", "P3")}

    def sibling_run(self, **kick):
        """Two workers, P1 cancelled through continuity while P2 completes: P2 must still be recorded."""
        self.pack3()
        g1, g2 = self.hold("P1"), self.hold("P2")
        self.set_scenario()
        p = self.popen(*self.kick_args(extra=["--max-workers", "2", *kick.pop("extra", [])], **kick))
        self.wait_for(g1 + ".started")
        self.wait_for(g2 + ".started")
        for d in sorted(self.sdir.glob("work/.ai-continuity/ledger/runs/*")):
            if "P1" in d.name:
                job = d.name
        c = subprocess.run([PY, "-B", str(CONT), "cancel", "--project", str(self.sdir / "work"), "--job", job], capture_output=True, text=True,
                           env=self.env())
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.release(g1)
        self.release(g2)
        out, err = p.communicate(timeout=120)
        self.assertNotIn("Traceback", err)
        return p.returncode, json.loads(out.splitlines()[-1]), (g1, g2)

    def check_sibling(self, rc, out):
        self.assertEqual((rc, out["reason"]), (1, "usage-unknown"), out)
        rec = json.loads((self.sdir / "receipt.json").read_text())
        by = {x["item"]: x["outcome"] for x in rec["rounds"]}
        self.assertEqual(by, {"P1": "cancelled", "P2": "accepted"}, "a completed sibling round was dropped when its neighbour stopped the run")
        self.assertIn("| P2 | build | done |", self.runner_show("plans/demo/TRACKER.md"))
        st = self.status().json
        self.assertIn("P2", st["items"]["done"])
        w = self.sdir / "work"
        self.assertEqual(self.git("branch", "--list", "dispatch/*", cwd=w), "", "orphan dispatch branch")
        wl = self.git("worktree", "list", "--porcelain", cwd=w)
        self.assertNotIn("dispatch/", wl, "orphan dispatch worktree")
        n = self.dcount("P2")
        self.assertEqual(n, 1)
        return st

    def test_n1_sibling_round_survives_a_stopping_round_under_a_usd_budget(self):
        rc, out, _ = self.sibling_run(budget=None, extra=["--budget-usd", "5", "--prices", self.prices()])
        st = self.check_sibling(rc, out)
        self.assertGreater(st["spent"]["usd"], 0, "the completed sibling's cost was not charged")
        r = self.resume("--max-workers", "2")
        self.assertNotIn("Traceback", r.err)
        self.assertEqual(self.dcount("P2"), 1, "P2 was dispatched again on resume")

    def test_n1_sibling_round_survives_a_stopping_round_under_a_token_budget(self):
        rc, out, _ = self.sibling_run(budget="100000")
        st = self.check_sibling(rc, out)
        self.assertGreater(st["spent"]["tokens"], 100000, "cap charge for P1 plus P2's tokens")
        r = self.resume("--max-workers", "2")
        self.assertNotIn("Traceback", r.err)
        self.assertEqual(self.dcount("P2"), 1, "P2 was dispatched again on resume")

    def test_n1_settle_inflight_records_the_sibling_of_a_cancelled_round(self):
        self.pack3()
        g1, g2 = self.hold("P1"), self.hold("P2")
        self.set_scenario()
        args = self.kick_args(budget=None, extra=["--max-workers", "2", "--budget-usd", "5", "--prices", self.prices()])
        p = self.popen(*args)
        self.wait_for(g1 + ".started")
        self.wait_for(g2 + ".started")
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        job = next(d.name for d in sorted(self.sdir.glob("work/.ai-continuity/ledger/runs/*")) if "P1" in d.name)
        c = subprocess.run([PY, "-B", str(CONT), "cancel", "--project", str(self.sdir / "work"), "--job", job], capture_output=True, text=True,
                           env=self.env())
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.release(g1)
        self.release(g2)
        for d in self.sdir.glob("work/.ai-continuity/ledger/runs/*"):
            self.wait_for(d / "result.json")
        r = self.resume("--max-workers", "2")
        self.assertEqual((r.rc, r.reason), (1, "usage-unknown"), r.text + r.err)
        rec = json.loads((self.sdir / "receipt.json").read_text())
        self.assertEqual({x["item"]: x["outcome"] for x in rec["rounds"]}, {"P1": "cancelled", "P2": "accepted"})
        self.assertGreater(self.status().json["spent"]["usd"], 0)
        self.assertEqual(self.dcount("P2"), 1)

    def test_f1_unbudgeted_offline_interruption_still_continues(self):
        self.make_repo([item("S1", verify="true")])
        self.set_scenario({})
        r = self.ap("kickoff", "--repo", self.repo, "--plan", self.plan, "--provider", "offline")
        self.assertEqual(r.rc, 0, r.text + r.err)


if __name__ == "__main__":
    unittest.main()
