"""U16 A13 (resource control on this host), A14 (adapter contract + assessment) and the adapters half of A16.

Offline. The systemd-scope cells use the real user manager on a supported host (cgroup v2, delegated cpu/memory/pids);
they skip with a printed reason where it is not there, and fail under AIARCH_REQUIRE_RESOURCE=1. No model is started:
the only "worker" is the marked fake harness shipped with the autopilot tests.
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
PY = sys.executable
RES = MOD / "resources.py"
CONF = MOD / "conformance.py"
ADAPTER = MOD / "native_adapter.py"
FIXB = HERE / "fixtures" / "broken_adapter.py"
FIXL = HERE / "fixtures" / "liar_adapter.py"
FIXH = HERE / "fixtures" / "hidden_state_adapter.py"
FIXD = HERE / "fixtures" / "decoy_adapter.py"
FAKE_BIN = HERE / "fixtures" / "fake-bin"
REQUIRE = os.environ.get("AIARCH_REQUIRE_RESOURCE") == "1"
ORIG_ENV = dict(os.environ)
SYSTEMD_RUN = shutil.which("systemd-run") or ""
SYSTEMCTL = shutil.which("systemctl") or ""
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "")
DBUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
GITENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def tearDownModule():
    os.environ.clear()
    os.environ.update(ORIG_ENV)


def host_probe():
    if not (SYSTEMD_RUN and SYSTEMCTL and RUNTIME):
        return "unavailable: systemd-run/systemctl/XDG_RUNTIME_DIR missing"
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": RUNTIME, "DBUS_SESSION_BUS_ADDRESS": DBUS}
    r = subprocess.run([PY, "-B", str(RES), "probe", "--systemctl", SYSTEMCTL], capture_output=True, text=True, env=env)
    try:
        return json.loads(r.stdout)["backends"]["systemd-scope"]
    except (ValueError, KeyError):
        return f"unavailable: probe output unreadable ({r.stderr.strip()[:80]})"


SCOPE = host_probe()
SCOPE_OK = SCOPE == "available"
needs_scope = unittest.skipUnless(SCOPE_OK or REQUIRE, f"systemd-scope backend: {SCOPE}")


def leftover_units():
    if not SYSTEMCTL:
        return []
    r = subprocess.run([SYSTEMCTL, "--user", "list-units", "--all", "--no-legend", "aiarch-*"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": RUNTIME, "DBUS_SESSION_BUS_ADDRESS": DBUS})
    return [l.split()[0] for l in r.stdout.splitlines() if l.strip()]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.sentinel = self.tmp / "sentinel"
        self.sentinel.mkdir()
        self.log = self.tmp / "sentinel.log"
        for name in ("claude", "opencode", "npx", "npm", "curl", "wget", "crontab", "systemctl", "systemd-run", "paperclipai"):
            p = self.sentinel / name
            p.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {self.log}\nexit 97\n")
            p.chmod(0o755)
        self.procs = []
        self.addCleanup(self.cleanup_units)

    def cleanup_units(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        before = set(getattr(self, "units_before", []))
        left = [u for u in leftover_units() if u not in before]
        for u in left:
            subprocess.run([SYSTEMCTL, "--user", "stop", u], capture_output=True, env=self.env(scope=True))
            subprocess.run([SYSTEMCTL, "--user", "reset-failed", u], capture_output=True, env=self.env(scope=True))
        self.assertEqual([u for u in leftover_units() if u not in before], [], "an aiarch-* unit outlived the test")

    def env(self, scope=False, **extra):
        e = {"PATH": f"{self.sentinel}:/usr/bin:/bin", "HOME": str(self.tmp / "home"), "LANG": "C.UTF-8",
             "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_CONFIG_HOME": str(self.tmp / "config"), "TMPDIR": str(self.tmp),
             "PYTHONDONTWRITEBYTECODE": "1", **GITENV}
        if scope:
            e.update({"XDG_RUNTIME_DIR": RUNTIME, "DBUS_SESSION_BUS_ADDRESS": DBUS})
        e.update(extra)
        return e

    def res(self, *args, scope=True, timeout=120, **kw):
        base = ["--systemd-run", SYSTEMD_RUN, "--systemctl", SYSTEMCTL] if "run" == args[0] or args[0] == "probe" else []
        if args[0] == "stop":
            base = ["--systemctl", SYSTEMCTL]
        if args[0] == "probe":
            base = ["--systemctl", SYSTEMCTL]
        r = subprocess.run([PY, "-B", str(RES), args[0], *base, *args[1:]], capture_output=True, text=True, timeout=timeout,
                           env=self.env(scope=scope), cwd=str(self.tmp), stdin=subprocess.DEVNULL, **kw)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        return r

    def evidence(self, name="ev.json"):
        return self.tmp / name

    def run_scope(self, limits, cmd, timeout=90):
        ev = self.evidence()
        if ev.exists():
            ev.unlink()
        r = self.res("run", "--backend", "systemd-scope", *limits, "--evidence-file", str(ev), "--", *cmd, timeout=timeout)
        return r, (json.loads(ev.read_text()) if ev.exists() else None)


class A13Resources(Base):
    def setUp(self):
        super().setUp()
        self.units_before = leftover_units()

    def test_a13_probe_reports_each_backend(self):
        r = self.res("probe")
        self.assertEqual(r.returncode, 0, r.stderr)
        backends = json.loads(r.stdout)["backends"]
        self.assertEqual(set(backends), {"systemd-scope", "rlimit"})
        for v in backends.values():
            self.assertTrue(v == "available" or v.startswith("unavailable: "), v)
        self.assertEqual(backends["rlimit"], "available")
        self.assertEqual(self.log.exists(), False, "probe called a sentinel binary")

    @needs_scope
    def test_a13a_memory_hog_is_oom_killed(self):
        hog = "b = bytearray()\nfor _ in range(512):\n    b.extend(b'x' * 1048576)\nprint('survived')\n"
        r, ev = self.run_scope(["--memory-max-mb", "64"], [PY, "-c", hog])
        self.assertEqual(r.returncode, 125, r.stdout + r.stderr)
        self.assertEqual(ev["reason"], "oom-kill", ev)
        self.assertTrue(ev["killed_by_limit"])
        self.assertEqual(ev["limits_applied"]["memory_swap_max_bytes"], 0)
        self.assertEqual(ev["limits_applied"]["memory_max_bytes"], 64 * 1024 * 1024)
        self.assertGreaterEqual(ev["observed"]["oom_kill"], 1)
        self.assertNotIn("survived", r.stdout)

    @needs_scope
    def test_a13b_fork_loop_is_bounded_by_tasks_max(self):
        loop = ("import os, sys, time\nkids = []\nerr = 0\n"
                "for _ in range(100):\n    try:\n        pid = os.fork()\n    except OSError:\n        err += 1\n        continue\n"
                "    if pid == 0:\n        time.sleep(3)\n        os._exit(0)\n    kids.append(pid)\n"
                "print('EAGAIN', err, 'forked', len(kids))\n"
                "for k in kids:\n    os.kill(k, 9)\n    os.waitpid(k, 0)\n")
        r, ev = self.run_scope(["--tasks-max", "16"], [PY, "-c", loop])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = re.search(r"EAGAIN (\d+) forked (\d+)", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        self.assertGreater(int(m.group(1)), 0, "the fork loop never saw EAGAIN")
        self.assertLessEqual(ev["observed"]["pids_peak"], 16, ev)
        self.assertEqual(ev["limits_applied"]["tasks_max"], 16)

    @needs_scope
    def test_a13c_cpu_quota(self):
        spin = "import time\nend = time.time() + 3\nwhile time.time() < end:\n    pass\n"
        t0 = time.monotonic()
        r, ev = self.run_scope(["--cpu-quota", "20"], [PY, "-c", spin])
        wall = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLessEqual(ev["observed"]["cpu_usage_ns"], 0.35 * wall * 1e9, (ev, wall))
        self.assertEqual(ev["limits_applied"]["cpu_quota_percent"], 20)

    @needs_scope
    def test_a13d_stop_leaves_nothing_and_a_detached_autopilot_resumes(self):
        unit_file = self.tmp / "unit"
        p = subprocess.Popen([PY, "-B", str(RES), "run", "--systemd-run", SYSTEMD_RUN, "--systemctl", SYSTEMCTL, "--backend", "systemd-scope",
                              "--memory-max-mb", "256", "--unit-file", str(unit_file), "--", "/bin/sh", "-c", "sleep 300 & sleep 300 & wait"],
                             env=self.env(scope=True), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        end = time.time() + 30
        while time.time() < end and not unit_file.exists():
            time.sleep(0.05)
        unit = unit_file.read_text().strip()
        self.assertRegex(unit, r"^aiarch-[0-9a-f]+\.scope$")
        cg = subprocess.run([SYSTEMCTL, "--user", "show", unit, "-p", "ControlGroup", "--value"], capture_output=True, text=True, env=self.env(scope=True)).stdout.strip()
        time.sleep(0.5)
        pids = [int(x) for x in Path(f"/sys/fs/cgroup{cg}/cgroup.procs").read_text().split()]
        self.assertGreaterEqual(len(pids), 2)
        r = self.res("stop", "--unit", unit)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        p.wait(timeout=30)

        def live(pid):
            try:
                return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except (OSError, IndexError):
                return False

        def settled():
            # Teardown is asynchronous (a SIGKILLed process can sit in state D; systemd drops the scope after its last process): poll
            # with a deadline rather than one fixed sleep, and report what was still there if it never settles.
            state = {"live": [x for x in pids if live(x)],
                     "is-active": subprocess.run([SYSTEMCTL, "--user", "is-active", unit], capture_output=True, text=True, env=self.env(scope=True)).stdout.strip(),
                     "new-units": [u for u in leftover_units() if u not in self.units_before]}
            return not state["live"] and state["is-active"] != "active" and not state["new-units"], state

        end = time.time() + 15
        done, state = settled()
        while not done and time.time() < end:
            time.sleep(0.1)
            done, state = settled()
        self.assertTrue(done, state)

    @needs_scope
    def test_a13d_detached_controller_killed_with_its_scope_then_resume(self):
        ap = _load("ap_tests_helpers", ROOT / "modules" / "autopilot" / "tests" / "test_autopilot.py")
        t = ap.Base()
        t.setUp()
        try:
            t.items = [ap.item("S1"), ap.item("S2", needs="S1", verify="test -f out/s2.txt")]
            t.make_repo()
            gate = t.hold("S2")
            t.scenario["S1"] = {"writes": [{"path": "out/s1.txt"}]}
            t.scenario["S2"] = dict(t.scenario["S2"], writes=[{"path": "out/s2.txt"}])
            t.set_scenario()
            limits = t.tmp / "limits.json"
            limits.write_text(json.dumps({"backend": "systemd-scope", "memory_max_mb": 4096, "tasks_max": 1024, "systemd_run": SYSTEMD_RUN,
                                          "systemctl": SYSTEMCTL}))
            env = t.env(XDG_RUNTIME_DIR=RUNTIME, DBUS_SESSION_BUS_ADDRESS=DBUS)
            r = t.ap(*t.kick_args(extra=["--detach", "--resource-limits", limits]), env=env)
            self.assertEqual(r.rc, 0, r.text + r.err)
            t.wait_for(gate + ".started")
            st = t.ap("status", "--json", "--plan-id", t.plan_id, env=env).json
            unit = st["resource"]["unit"]
            self.assertRegex(unit, r"^aiarch-[0-9a-f]+\.scope$")
            s = subprocess.run([PY, "-B", str(RES), "stop", "--systemctl", SYSTEMCTL, "--unit", unit], capture_output=True, text=True,
                               env=self.env(scope=True))
            self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
            time.sleep(0.5)
            for f in t.fb.glob("pids/*"):
                pid = int(f.read_text().split()[0])
                self.assertFalse(Path(f"/proc/{pid}").exists() and Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z", f.name)
            self.assertEqual([u for u in leftover_units() if u not in self.units_before], [])
            t.release(gate)
            sj = json.loads((t.sdir / "state.json").read_text())  # the controller banked the killed round before it ended: unknown usage under a budget
            self.assertEqual(sj["stop_reason"], "usage-unknown", sj["stop_reason"])
            self.assertEqual(t.dcount("S2"), 1, "the run must not dispatch again after an unknown-usage round")
            r = t.resume()
            self.assertEqual((r.rc, r.reason), (2, "resource-limits-need-detach"), r.text + r.err)  # limits only apply to a detached controller
            r = t.resume("--detach", "--budget-tokens", "1000000", env=env)  # a person raises the budget; the normal human-gated resume continues
            self.assertEqual((r.rc, r.reason), (0, "detached"), r.text + r.err)
            end = time.time() + 120
            while time.time() < end:
                st = t.ap("status", "--json", "--plan-id", t.plan_id, env=env).json
                if st and not st["controller"]["alive"] and st["stop_reason"] == "done":
                    break
                time.sleep(0.3)
            self.assertEqual(st["stop_reason"], "done", st)
            rec = json.loads((t.sdir / "receipt.json").read_text())
            self.assertEqual([x["outcome"] for x in rec["rounds"]], ["accepted", "interrupted", "accepted"], rec["rounds"])
            self.assertEqual(t.dcount("S1"), 1)
            self.assertEqual(t.dcount("S2"), 2)
            c = subprocess.run([PY, "-B", str(ROOT / "modules/continuity/continuity.py"), "status", "--project", str(t.sdir / "work")],
                               capture_output=True, text=True, env=t.env())
            causes = sorted(j["outcome"] for j in json.loads(c.stdout)["jobs"].values())
            self.assertEqual(len(causes), 3)
            self.assertEqual(causes, ["failed", "succeeded", "succeeded"], causes)  # the killed worker is banked once as failed, never rerun
        finally:
            t.reap()
            t._tmp.cleanup()

    def test_a13e_rlimit_backend(self):
        hog = "import sys\ntry:\n    b = bytearray(600 * 1024 * 1024)\nexcept MemoryError:\n    sys.stderr.write('MemoryError\\n')\n    sys.exit(7)\nprint('survived')\n"
        ev = self.evidence()
        r = self.res("run", "--backend", "rlimit", "--address-space-mb", "300", "--evidence-file", str(ev), "--", PY, "-c", hog, scope=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("MemoryError", r.stderr)
        self.assertNotIn("survived", r.stdout)
        spin = "while True:\n    pass\n"
        r = self.res("run", "--backend", "rlimit", "--cpu-seconds", "1", "--evidence-file", str(ev), "--", PY, "-c", spin, scope=False, timeout=60)
        self.assertEqual(r.returncode, 125, r.stdout + r.stderr)
        e = json.loads(ev.read_text())
        self.assertEqual(e["reason"], "rlimit-cpu")
        self.assertEqual(e["backend"], "rlimit")
        self.assertIn("tasks", e["unbounded"], "rlimit must declare that it has no per-tree task bound")

    def test_a13f_unavailable_never_runs_the_command(self):
        canary = self.tmp / "canary"
        cmd = ["/bin/sh", "-c", f"touch {canary}"]
        r = subprocess.run([PY, "-B", str(RES), "run", "--systemd-run", "/nonexistent/systemd-run", "--systemctl", SYSTEMCTL or "/nonexistent/systemctl",
                            "--backend", "systemd-scope", "--memory-max-mb", "64", "--", *cmd], capture_output=True, text=True,
                           env=self.env(scope=True), stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(canary.exists(), "an unbounded fallback ran the command")
        e = self.env(scope=True, XDG_RUNTIME_DIR=str(self.tmp / "no-runtime-dir"))  # the user manager cannot be reached
        r = subprocess.run([PY, "-B", str(RES), "run", "--systemd-run", SYSTEMD_RUN or "/nonexistent", "--systemctl", SYSTEMCTL or "/nonexistent",
                            "--backend", "systemd-scope", "--memory-max-mb", "64", "--", *cmd], capture_output=True, text=True, env=e,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(canary.exists())
        r = subprocess.run([PY, "-B", str(RES), "run", "--backend", "systemd-scope", "--", *cmd], capture_output=True, text=True,
                           env=self.env(scope=True), stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2, "a run without any limit is refused, not run unbounded")
        self.assertFalse(canary.exists())

    @needs_scope
    def test_a13g_telemetry_alone_is_not_a_bound(self):
        tele = self.tmp / "tele.json"
        r = subprocess.run([PY, "-B", str(ROOT / "modules/observability/observe.py"), "resources", "--pid", str(os.getpid())],
                           capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        tele.write_text(r.stdout)
        v = self.res("verdict", "--evidence", str(tele), scope=False)
        self.assertEqual(json.loads(v.stdout)["verdict"], "not-bounded")
        r, ev = self.run_scope(["--memory-max-mb", "64", "--tasks-max", "64"], [PY, "-c", "print(1)"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        v = self.res("verdict", "--evidence", str(self.evidence()), scope=False)
        self.assertEqual(json.loads(v.stdout)["verdict"], "bounded")
        self.assertNotEqual(json.loads(tele.read_text()).get("schema"), ev["schema"])


class A14Contract(Base):
    @classmethod
    def setUpClass(cls):
        cls.conf = _load("conformance_under_test", CONF)

    def conformance(self, adapter, *extra, allow=True, timeout=600):
        a = [PY, "-B", str(CONF), "--adapter", str(adapter), "--fake-bin", str(FAKE_BIN), "--json", *extra]
        if allow:
            a.append("--allow-adapter")
        r = subprocess.run(a, capture_output=True, text=True, env=self.env(), timeout=timeout, cwd=str(self.tmp), stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        return r, (json.loads(r.stdout) if r.stdout.strip().startswith("{") else None)

    def test_a14_contract_lists_the_fourteen_cells(self):
        contract = json.loads((MOD / "contract.json").read_text())
        self.assertEqual(contract["cells"], ["budget", "unknown-cost", "gate-human", "blocked-by", "freeze-drift", "touches", "corrupt-state",
                                             "double-kickoff", "interruption", "queued-drain", "status-readonly", "pause", "stop", "resume-once"])
        for verb in ("kickoff", "status", "pause", "stop", "resume", "state"):
            self.assertIn(verb, contract["protocol"])
        self.assertNotIn("inject", contract["protocol"], "the harness performs the injections, not the adapter")
        self.assertIn("never counts", contract["evidence_rule"])

    def test_a14_native_autopilot_passes_every_cell(self):
        r, rep = self.conformance(ADAPTER)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
        self.assertEqual({k: v["result"] for k, v in rep["cells"].items()}, {k: "pass" for k in rep["cells"]}, rep)
        self.assertEqual(len(rep["cells"]), 14)

    def test_a14_broken_adapter_fails_exactly_pause_and_unknown_cost(self):
        r, rep = self.conformance(FIXB, "--adapter-env", f"AIARCH_BROKEN_BASE={ADAPTER}")
        self.assertEqual(r.returncode, 1, r.stdout[-3000:])
        failed = sorted(k for k, v in rep["cells"].items() if v["result"] == "fail")
        self.assertEqual(failed, ["pause", "unknown-cost"], rep)
        self.assertEqual(len([v for v in rep["cells"].values() if v["result"] == "pass"]), 12)

    def test_a14_liar_adapter_fails_the_cells_that_used_to_trust_its_report(self):
        """F2: misbehaviour behind clean-looking status/rounds_accepted/stop_reason fails on harness evidence; a crash is not a refusal."""
        r, rep = self.conformance(FIXL, "--adapter-env", f"AIARCH_BROKEN_BASE={ADAPTER}")
        self.assertEqual(r.returncode, 1, r.stdout[-3000:])
        res = {k: v["result"] for k, v in rep["cells"].items()}
        for cell in ("touches", "interruption", "resume-once", "pause", "stop", "freeze-drift", "corrupt-state"):
            self.assertEqual(res[cell], "fail", (cell, rep["cells"][cell]))

    def test_a14_refusal_cells_need_the_specific_reason_not_any_nonzero_exit(self):
        r, rep = self.conformance(FIXL, "--adapter-env", f"AIARCH_BROKEN_BASE={ADAPTER}", "--adapter-env", "AIARCH_LIAR_REFUSAL=wrong-reason")
        for cell in ("freeze-drift", "corrupt-state"):
            self.assertEqual(rep["cells"][cell]["result"], "fail", (cell, rep["cells"][cell]))
            self.assertIn("internal-error", rep["cells"][cell]["note"])

    def decoy(self, mode):
        return self.conformance(FIXD, "--adapter-env", f"AIARCH_BROKEN_BASE={ADAPTER}", "--adapter-env", f"AIARCH_DECOY_SLEEPER={mode}")

    def check_decoy(self, mode, expect):
        """N2: an adapter-published pid / stop_reason / state file does not decide a cell; each cell fails or is not-run, with the reason."""
        r, rep = self.decoy(mode)
        self.assertEqual(r.returncode, 1, r.stdout[-3000:])
        res = {k: v["result"] for k, v in rep["cells"].items()}
        for cell in ("interruption", "resume-once", "corrupt-state", "stop"):
            self.assertIn(res[cell], ("fail", "not-run"), (cell, rep["cells"][cell]))
            self.assertNotEqual(res[cell], "pass")
        for cell, (result, text) in expect.items():
            self.assertEqual(res[cell], result, (cell, rep["cells"][cell]))
            self.assertIn(text, rep["cells"][cell]["note"], (cell, rep["cells"][cell]))
        self.assertEqual(res["corrupt-state"], "not-run", rep["cells"]["corrupt-state"])

    def test_n2_decoy_with_a_pid_outside_the_launched_tree(self):
        self.check_decoy("outside", {"interruption": ("fail", "outside the launched process tree"), "resume-once": ("fail", "outside the launched process tree"),
                                     "stop": ("fail", "dispatch leftovers")})

    def test_n2_decoy_with_a_pid_inside_the_tree_but_a_controller_that_escaped_it(self):
        self.check_decoy("forge", {"interruption": ("fail", "work continued after the launched controller was killed"),
                                   "resume-once": ("fail", "work continued after the launched controller was killed"), "stop": ("fail", "dispatch leftovers")})

    def test_n2_decoy_state_is_never_what_a_cell_corrupts(self):
        r, rep = self.decoy("outside")
        self.assertIn("cannot prove which state file", rep["cells"]["corrupt-state"]["note"])

    def test_a14_state_the_harness_cannot_find_is_not_run_never_pass(self):
        r, rep = self.conformance(FIXH, "--adapter-env", f"AIARCH_BROKEN_BASE={ADAPTER}")
        res = {k: v["result"] for k, v in rep["cells"].items()}
        for cell in ("corrupt-state", "interruption", "resume-once"):
            self.assertEqual(res[cell], "not-run", (cell, rep["cells"][cell]))
        for cell in ("pause", "stop"):  # decided on the dispatch log, git and process liveness: no state file needed
            self.assertEqual(res[cell], "pass", (cell, rep["cells"][cell]))

    def test_a14_refused_without_allow_and_spawns_nothing(self):
        marker = self.tmp / "ran"
        script = self.tmp / "adapter.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
        r, rep = self.conformance(script, allow=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(rep["reason"], "adapter-not-allowed")
        self.assertFalse(marker.exists())
        self.assertFalse((FAKE_BIN / "dispatch.log").exists())
        r, rep = self.conformance("adapter-by-name")
        self.assertEqual((r.returncode, rep["reason"]), (2, "adapter-not-absolute"))
        for name in ("npx", "npm"):
            r, rep = self.conformance(name)
            self.assertEqual((r.returncode, rep["reason"]), (2, "adapter-not-absolute"))
        self.assertFalse(self.log.exists())

    def test_a14_empty_slot_is_not_run_never_pass(self):
        r, rep = self.conformance(self.tmp / "no-such-adapter")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual({v["result"] for v in rep["cells"].values()}, {"not-run"})
        self.assertEqual(len(rep["cells"]), 14)
        self.assertFalse((FAKE_BIN / "dispatch.log").exists())

    def test_a14_not_run_is_never_mapped_to_pass(self):
        self.assertEqual(self.conf.fold("not-run"), "not-run")
        self.assertNotEqual(self.conf.fold(None), "pass")
        self.assertEqual(self.conf.fold(True), "pass")
        self.assertEqual(self.conf.fold(False), "fail")

    def test_a14_assessment_validates_and_is_complete(self):
        doc = json.loads((MOD / "assessment.json").read_text())
        self.conf.validate_assessment(doc, root=ROOT)
        ids = {r["id"] for r in doc["records"]}
        for need in ("native-autopilot", "paperclip", "worker-containment", "background-round-runner", "ready-frontier-report",
                     "sandboxed-verify-runner", "plan-binding-runner", "pilot-instance-installer", "network-relay", "trusted-pilot",
                     "trusted-process-adapter", "trusted-launch-entrypoint", "guard-installer", "sandbox-installer"):
            self.assertIn(need, ids)
        rec = {r["id"]: r for r in doc["records"]}
        pc = rec["paperclip"]
        self.assertEqual(pc["kind"], "third-party")
        self.assertEqual(pc["disposition"], "not-ported")
        self.assertEqual(set(pc["cells"].values()), {"not-run"})
        req = " ".join(pc["requirements"]).lower()
        for word in ("node.js", "embedded database", "network"):
            self.assertIn(word, req)
        self.assertEqual(rec["worker-containment"]["disposition"], "incomplete")
        self.assertIn("orchestration outside the fence", rec["worker-containment"]["blocker"])
        for r in doc["records"]:
            self.assertTrue(r["evidence"], r["id"])
            self.assertTrue(r.get("equivalent") or r.get("blocker"), r["id"])
        md = (MOD / "ASSESSMENT.md").read_text()
        for r in doc["records"]:
            self.assertIn(r["id"], md)

    def test_a14_validation_rejects_bad_records(self):
        doc = json.loads((MOD / "assessment.json").read_text())

        def bad(mutate):
            d = json.loads(json.dumps(doc))
            mutate(d)
            with self.assertRaises(ValueError):
                self.conf.validate_assessment(d, root=ROOT)
        rec = lambda d, i: next(r for r in d["records"] if r["id"] == i)
        bad(lambda d: rec(d, "native-autopilot")["cells"].update({"pause": "not-run"}))  # supported with a not-run cell
        bad(lambda d: rec(d, "paperclip").update({"evidence": []}))
        bad(lambda d: rec(d, "paperclip").update({"equivalent": None, "blocker": None}))
        bad(lambda d: rec(d, "paperclip").update({"evidence": ["/" + "home/someone/private.md"]}))
        bad(lambda d: rec(d, "paperclip").update({"evidence": ["test:modules/autopilot/tests/test_autopilot.py::test_does_not_exist"]}))
        bad(lambda d: rec(d, "paperclip").update({"disposition": "supported"}))
        bad(lambda d: d["records"].remove(rec(d, "trusted-pilot")))


class A16Constraints(Base):
    def test_a16_descriptor_provenance_privacy_static(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT))
        import check_collection as cc
        import privacy_scan as ps
        from modules.contracts import contracts as C
        d = C.check_module_descriptor(MOD, module="control-plane-adapters")
        self.assertEqual(d["claimed_support"], "planned")
        self.assertEqual({x["identifier"] for x in d["dependencies"]}, {"core-contracts", "autopilot"})
        self.assertTrue(all(x["status"] == "declared-untested" for x in d["dependencies"]))
        cc.provenance(MOD / "PROVENANCE.md")
        self.assertEqual(ps.scan(MOD), [])
        banned = re.compile(r"urllib\.request|http\.client|\bssl\b|\brequests\b|socket\.create_connection|\bnpx\b|\bnpm |pip install")
        for f in MOD.glob("*.py"):
            self.assertIsNone(banned.search(f.read_text()), f.name)
        ls = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", str(MOD)], cwd=ROOT, capture_output=True, text=True).stdout.split()
        for name in ls:
            self.assertNotIn(b"\0", (ROOT / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
