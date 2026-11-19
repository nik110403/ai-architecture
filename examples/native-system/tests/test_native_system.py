"""Acceptance tests A1..A13 for examples/native-system (offline and fake harnesses only).

Safety: every run.py and continuity process gets PATH=<tmp>/bin and HOME=<tmp>/home, so no real `claude` or
`opencode` can be reached. Before each A4 case the test proves that with `dispatch.find_exe`.
"""

import argparse
import ast
import contextlib
import getpass
import hashlib
import importlib.util
import io
import json
import os
import types
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
EX = HERE.parent
ROOT = EX.parents[1]
RUN, VERIFY = EX / "run.py", EX / "verify_receipt.py"
CONT = ROOT / "modules" / "continuity" / "continuity.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from modules.orchestration import dispatch  # noqa: E402

_spec = importlib.util.spec_from_file_location("privacy_scan", ROOT / "scripts" / "privacy_scan.py")
privacy_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(privacy_scan)

_run_spec = importlib.util.spec_from_file_location("ns_run_under_test", RUN)
run_mod = importlib.util.module_from_spec(_run_spec)
_run_spec.loader.exec_module(run_mod)

MARKER = "AI_ARCH_FAKE_HARNESS"
HARNESS_SID = "ses" + "_FAKE0001"
HOME_FRAGMENT = "/" + "home" + "/"
PREFIX_SES = "ses" + "_"
COMPLETE = "complete, nothing to do"

FAKE = """#!{py}
# AI_ARCH_FAKE_HARNESS: a test double that never reaches a model
import json, os, re, sys, time
NAME, GATE = {name!r}, {gate!r}
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, NAME + ".log"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("fake 1.0")
    sys.exit(0)
prompt = sys.stdin.read()
sid = "ses" + "_FAKE0001"
if "Claimed sum:" in prompt:
    nums = [int(x) for x in re.search(r"Numbers: (.*)", prompt).group(1).split()]
    claim = int(re.search(r"Claimed sum: (-?\\d+)", prompt).group(1))
    text = "CONFIRMED" if sum(nums) == claim else "REJECTED"
    if os.path.exists(os.path.join(here, "verdict-mode")):  # refuter-style reply
        text = "VERDICT: %s\\nREASON: r\\nEVIDENCE: e" % open(os.path.join(here, "verdict-mode")).read().strip()
else:
    while GATE and not os.path.exists(GATE):
        time.sleep(0.05)
    text = str(sum(int(x) for x in open("input.txt").read().split()))
if NAME == "claude":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                      "terminal_reason": "completed", "result": text, "session_id": sid,
                      "usage": {{"input_tokens": 10, "output_tokens": 2}}}}))
else:
    tokens = {{"total": 12, "input": 10, "output": 2, "reasoning": 0, "cache": {{"write": 0, "read": 0}}}}
    for event in ({{"type": "step_start", "sessionID": sid, "part": {{"type": "step-start"}}}},
                  {{"type": "text", "sessionID": sid, "part": {{"type": "text", "text": text}}}},
                  {{"type": "step_finish", "sessionID": sid,
                    "part": {{"type": "step-finish", "reason": "stop", "tokens": tokens, "cost": 0}}}}):
        print(json.dumps(event))
"""


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@contextlib.contextmanager
def deadline(seconds=10):
    """Fail fast (not hang) if the block blocks, e.g. on a FIFO open or read."""
    def boom(signum, frame):
        raise AssertionError(f"blocked for more than {seconds} s")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class Sandbox:
    """One temporary world: PATH holds only <tmp>/bin, HOME is empty."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.bin, self.home, self.fakebin = self.dir / "bin", self.dir / "home", self.dir / "fakebin"
        for d in (self.bin, self.home, self.fakebin):
            d.mkdir()
        self.env = {"PATH": str(self.bin), "HOME": str(self.home), "XDG_STATE_HOME": str(self.dir / "xdg")}
        self.outs = []

    def close(self):
        for out in self.outs:
            for name in ("gate", "hold-never"):
                for base in (Path(out), *Path(out).glob("runs/*")):
                    try:
                        (base / name).touch()
                    except OSError:
                        pass
        self.tmp.cleanup()

    def find_exe_here(self, name):
        old = os.environ.get("PATH")
        os.environ["PATH"] = self.env["PATH"]
        try:
            return dispatch.find_exe(name)
        finally:
            if old is None:
                del os.environ["PATH"]
            else:
                os.environ["PATH"] = old

    def fake(self, name, gate=None, directory=None, marker=True):
        d = Path(directory or self.fakebin)
        d.mkdir(exist_ok=True)
        path = d / name
        text = FAKE.format(py=sys.executable, name=name, gate=str(gate) if gate else "")
        path.write_text(text if marker else text.replace(MARKER, "plain"), encoding="utf-8")
        path.chmod(0o755)
        return path

    def log(self, directory, name):
        path = Path(directory) / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def run(self, *args, script=RUN, timeout=120):
        return subprocess.run([sys.executable, "-B", str(script), *map(str, args)], env=self.env,
                              capture_output=True, text=True, timeout=timeout, cwd=str(self.dir))

    def cont(self, out, *args):
        argv = [sys.executable, "-B", str(CONT), args[0], "--project", str(Path(out) / "project"), *args[1:]]
        proc = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=120, cwd=str(self.dir))
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            raise AssertionError(f"non-JSON from continuity: {proc.stdout!r} {proc.stderr!r}")

    def offline(self, name="out", *extra):
        out = self.dir / name
        self.outs.append(out)
        return out, self.run("--offline", "--out", out, *extra)


def ledger(out):
    return json.loads((Path(out) / "project/.ai-continuity/ledger/ledger.json").read_text())


def receipt(out):
    return json.loads((Path(out) / "receipt.json").read_text())


def lines(path):
    path = Path(path)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class Shared(unittest.TestCase):
    """setUpClass reuses one offline run for A1/A2/A3/A5/A6/A11/A12."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox()
        cls.out, cls.proc = cls.sb.offline()

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def verify(self, *args):
        return self.sb.run(*args, script=VERIFY)

    def test_a1_offline_passes_and_verifies(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)
        v = self.verify("--receipt", self.out / "receipt.json")
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIn("PASS", self.proc.stdout)

    def test_a1b_bare_spec_command_twice(self):
        sb = Sandbox()
        self.addCleanup(sb.close)
        base = sb.dir / "xdg" / "ai-architecture" / "native-system"
        sb.outs.append(base)
        for _ in range(2):
            r = sb.run("--offline")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            v = sb.run(script=VERIFY)
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        latest = base / "latest.json"
        self.assertTrue(latest.is_file() and not latest.is_symlink())
        self.assertEqual(len(list((base / "runs").iterdir())), 1)

    def test_a2_offline_mode(self):
        self.assertEqual(receipt(self.out)["mode"], "offline")

    def test_a3_verifier_rejects_relabelled_receipts(self):
        r = receipt(self.out)
        edited = self.out / "receipt-edited.json"
        edited.write_text(json.dumps(dict(r, mode="live")))
        self.assertEqual(self.verify("--receipt", edited).returncode, 1)
        v = self.verify("--receipt", self.out / "receipt.json", "--expect-mode", "live")
        self.assertEqual(v.returncode, 1, v.stdout)

    def test_a5_kill_record_and_no_redispatch(self):
        r = receipt(self.out)
        self.assertEqual(r["kill"]["signal"], 9)
        self.assertTrue(r["kill"]["pid_gone"])
        self.assertFalse(alive(r["kill"]["pid"]))
        self.assertNotEqual(r["sessions"]["coordinator"], r["sessions"]["restart"])
        workers = [j for j in ledger(self.out)["jobs"].values() if j["role"] == "researcher"]
        self.assertEqual(len(workers), 1)
        self.assertEqual((workers[0]["state"], workers[0]["result"]["outcome"]), ("banked", "succeeded"))
        calls = lines(self.out / "calls.jsonl")
        starts = [c for c in calls if c["argv"][0] == "start" and "researcher" in c["argv"]]
        waits = [c for c in calls if c["argv"][0] == "wait"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["who"], r["sessions"]["coordinator"])
        self.assertEqual(waits[0]["who"], r["sessions"]["restart"])
        self.assertLess(calls.index(starts[0]), calls.index(waits[0]))

    def test_a6_next_consumed_by_restart_and_second_session_gets_notice(self):
        r = receipt(self.out)
        mine = [c for c in ledger(self.out)["checkpoints"] if c["resume"]["consumed_by"]]
        self.assertTrue(mine)
        self.assertTrue(all(c["resume"]["consumed_by"] == r["sessions"]["restart"] for c in mine))
        self.assertIn(COMPLETE, mine[-1]["next"])
        actions = (self.out / "actions.jsonl").read_text()
        p = self.sb.run("--resume", "--out", self.out, "--session", "cli:other-1")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        shown = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertIs(shown["delivered"], False)
        self.assertIn("notice", shown)
        self.assertEqual((self.out / "actions.jsonl").read_text(), actions)

    def test_a11_every_call_names_the_project(self):
        project = str(self.out / "project")
        calls = lines(self.out / "calls.jsonl")
        self.assertTrue(calls)
        for c in calls:
            argv = c["argv"]
            self.assertEqual(argv[argv.index("--project") + 1], project, argv)
            if "--state" in argv:
                self.assertIn(argv[0], ("checkpoint", "handoff"), argv)
        done = [c for c in calls if c["argv"][0] == "checkpoint" and c["code"] == 0]
        self.assertEqual(len(done), len(ledger(self.out)["checkpoints"]))

    def test_a11_single_call_site(self):
        """By call shape, not by name: every process-spawning call in run.py is a `subprocess.*` call, and there
        are exactly two: `cont` (the one continuity call site) and the coordinator re-exec of this very file."""
        tree = ast.parse(RUN.read_text())
        spawners = {"system", "popen", "execv", "execve", "execl", "execlp", "execvp", "spawnv", "spawnl", "fork"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "subprocess", "from-imports hide the call shape")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, spawners, ast.unparse(node))
        found = []
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                    found.append((fn.name, ast.unparse(node.args[0]) if node.args else ""))
        self.assertEqual(sorted(name for name, _ in found), ["cont", "kill_coordinator"], found)
        shape = dict(found)
        self.assertTrue(shape["cont"].startswith("[sys.executable, '-B', "), shape["cont"])
        self.assertIn("continuity", shape["cont"].lower())
        self.assertIn("__file__", shape["kill_coordinator"])
        self.assertNotIn("continuity", shape["kill_coordinator"].lower())

    def test_f7_finished_run_resumed_by_its_own_session_exits_zero(self):
        out = self.out
        r = self.sb.run("--resume", "--out", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already delivered to this session", r.stdout)


    def test_f8_verifier_checks_checker_independence(self):
        out = self.out
        r = receipt(out)
        worker, checker = r["jobs"][0]["id"], r["jobs"][1]["id"]
        rpath = out / "receipt.json"
        led = out / "project/.ai-continuity/ledger/ledger.json"
        prompt = out / "project/.ai-continuity/ledger/spool" / f"{checker}.prompt"
        self.assertEqual(self.verify("--receipt", rpath).returncode, 0)
        cases = {
            "ledger": (led, lambda t: t.replace('"role": "refuter"', '"role": "researcher"'), "is not a refuter"),
            "prompt": (prompt, lambda t: t.replace("Claimed sum:", "Claimed sum: 999 #"), "lacks the claimed sum"),
            "same job": (rpath, lambda t: t.replace(f'"id": "{checker}"', f'"id": "{worker}"'),
                         "is the worker's job"),
            "receipt role": (rpath, lambda t: t.replace('"role": "refuter"', '"role": "researcher"'),
                             "is not a refuter"),
        }
        for label, (path, edit, message) in cases.items():
            original = path.read_text()
            self.assertNotEqual(edit(original), original, label)
            path.write_text(edit(original))
            try:
                v = self.verify("--receipt", rpath)
                self.assertEqual(v.returncode, 1, (label, v.stdout))
                self.assertIn("checker independence", v.stdout, label)
                self.assertIn(message, v.stdout, label)
            finally:
                path.write_text(original)
        self.assertEqual(self.verify("--receipt", rpath).returncode, 0)

    def refuter_run(self, label, mode, harness="claude-code"):
        name = {"claude-code": "claude", "opencode": "opencode"}[harness]
        d, gate, out = self.sb.dir / f"fb-{label}", self.sb.dir / f"gate-{label}", self.sb.dir / label
        self.sb.fake("claude", gate if name == "claude" else None, d)
        self.sb.fake("opencode", gate if name == "opencode" else None, d)
        (d / "verdict-mode").write_text(mode)
        self.sb.outs.append(out)
        p = self.sb.run("--fake-harness", harness, "--fake-bin", d, "--gate", gate, "--out", out)
        return p, out

    def test_f12_verifier_normalizes_refuter_verdict(self):
        for harness in ("claude-code", "opencode"):
            p, out = self.refuter_run(f"v-stands-{harness}", "STANDS", harness)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            v = self.verify("--receipt", out / "receipt.json")
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        for mode in ("REFUTED", "UNVERIFIABLE"):
            p, out = self.refuter_run(f"v-{mode.lower()}", mode)
            self.assertNotEqual(p.returncode, 0, mode)
            v = self.verify("--receipt", out / "receipt.json")
            self.assertEqual(v.returncode, 1, (mode, v.stdout))
            self.assertIn("not CONFIRMED", v.stdout, mode)
        # a receipt edited to CONFIRMED over a REFUTED envelope
        rpath = out.parent / "v-refuted" / "receipt.json"
        original = rpath.read_text()
        edited = original.replace('"checker_verdict": "REJECTED"', '"checker_verdict": "CONFIRMED"')
        self.assertNotEqual(edited, original)
        rpath.write_text(edited)
        v = self.verify("--receipt", rpath)
        self.assertEqual(v.returncode, 1, v.stdout)
        self.assertIn("receipt checker verdict differs from the envelope", v.stdout)
        # the verifier recomputes the sum itself: a CONFIRMED run over a changed input.txt fails
        p, out = self.refuter_run("v-stands-sum", "STANDS")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (out / "project" / "input.txt").write_text("1 2 3\n")
        v = self.verify("--receipt", out / "receipt.json")
        self.assertEqual(v.returncode, 1, v.stdout)
        self.assertIn("answer is not the sum of input.txt", v.stdout)

    def test_f10_prefix_hash_survives_a_resume_append(self):
        r = receipt(self.out)
        calls = self.out / "calls.jsonl"
        original = calls.read_bytes()
        try:
            p = self.sb.run("--resume", "--out", self.out, "--session", "cli:other-10")
            self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
            grown = calls.read_bytes()
            self.assertGreater(grown.count(b"\n"), r["calls"]["lines"])
            v = self.verify("--receipt", self.out / "receipt.json")
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
            lines_ = grown.splitlines(keepends=True)
            lines_[0] = lines_[0].replace(b'"who"', b'"WHO"')
            calls.write_bytes(b"".join(lines_))
            v = self.verify("--receipt", self.out / "receipt.json")
            self.assertEqual(v.returncode, 1, v.stdout)
            self.assertIn("calls.jsonl differs", v.stdout)
        finally:
            calls.write_bytes(original)

    def test_a12_no_private_values(self):
        text = (self.out / "receipt.json").read_text()
        for needle in (HARNESS_SID, PREFIX_SES, HOME_FRAGMENT, os.path.realpath(self.out),
                       str(self.sb.home), getpass.getuser(), socket.gethostname()):
            self.assertNotIn(needle, text, needle)
        readme = (EX / "README.md").read_text()
        for needle in (PREFIX_SES, HOME_FRAGMENT):
            self.assertNotIn(needle, readme)
        self.assertEqual(privacy_scan.scan(EX), [])


class Variants(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def verify(self, *args):
        return self.sb.run(*args, script=VERIFY)

    # --- A2 fake harnesses
    def test_a2_fake_harnesses(self):
        for name in ("claude", "opencode"):
            harness = {"claude": "claude-code", "opencode": "opencode"}[name]
            d = self.sb.dir / f"fb-{name}"
            gate = self.sb.dir / f"gate-{name}"
            self.sb.fake("claude", gate if name == "claude" else None, d)
            self.sb.fake("opencode", gate if name == "opencode" else None, d)
            out = self.sb.dir / f"out-{name}"
            self.sb.outs.append(out)
            p = self.sb.run("--fake-harness", harness, "--fake-bin", d, "--gate", gate, "--out", out)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            r = receipt(out)
            self.assertEqual((r["mode"], r["harness"]), ("fake", harness))
            self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 0)
            self.assertTrue(self.sb.log(d, name))
            text = (out / "receipt.json").read_text()  # F7: no private value reaches a fake receipt
            for needle in (HARNESS_SID, PREFIX_SES, HOME_FRAGMENT, os.path.realpath(out), os.path.realpath(d),
                           str(self.sb.dir), str(self.sb.home), getpass.getuser(), socket.gethostname()):
                self.assertNotIn(needle, text, needle)
            # A3: a fake receipt relabelled live is refused
            edited = out / "receipt-edited.json"
            edited.write_text(json.dumps(dict(r, mode="live")))
            self.assertEqual(self.verify("--receipt", edited).returncode, 1)

    # --- A4 live refusals (F1)
    def test_a4_live_refusals_never_reach_a_binary(self):
        live = ("--live", "--harness", "claude-code", "--model", "MODEL_NAME", "--out", self.sb.dir / "o4")
        stub = self.sb.bin / "claude"
        stub.write_text(f"#!/bin/sh\necho called >> {self.sb.dir}/stub.log\n")
        stub.chmod(0o755)
        self.assertEqual(self.sb.find_exe_here("opencode"), None)
        self.assertTrue(self.sb.find_exe_here("claude").startswith(str(self.sb.dir)))
        p = self.sb.run(*live)  # case 1: no consent flag
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((self.sb.dir / "stub.log").exists())
        stub.unlink()
        self.assertIsNone(self.sb.find_exe_here("claude"))
        p = self.sb.run(*live, "--i-accept-spend")  # case 2: no binary
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.sb.fake("claude", directory=self.sb.bin)
        self.assertTrue(self.sb.find_exe_here("claude").startswith(str(self.sb.dir)))
        p = self.sb.run(*live, "--i-accept-spend")  # case 3: the binary carries the fake marker
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(self.sb.log(self.sb.bin, "claude"), [])
        p = self.sb.run("--live", "--harness", "claude-code", "--i-accept-spend", "--out", self.sb.dir / "o5")
        self.assertEqual(p.returncode, 2, "--model is required with --live")

    # --- A4c resume re-checks the spend guard (E1)
    def fake_killed(self, name="o4c"):
        d, gate, out = self.sb.dir / f"fb-{name}", self.sb.dir / f"gate-{name}", self.sb.dir / name
        self.sb.fake("claude", gate, d)
        self.sb.outs.append(out)
        p = self.sb.run("--fake-harness", "claude-code", "--fake-bin", d, "--gate", gate, "--out", out,
                        "--stop-after", "kill")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        seen, end = None, time.time() + 10  # let the released worker finish logging
        while time.time() < end:
            now = self.sb.log(d, "claude")
            if now and now == seen:
                break
            seen = now
            time.sleep(0.4)
        return d, out, self.sb.log(d, "claude")

    def test_a4c_resume_refuses_a_stripped_marker(self):
        d, out, before = self.fake_killed()
        (d / "claude").write_text((d / "claude").read_text().replace(MARKER, "plain"))
        p = self.sb.run("--resume", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(self.sb.log(d, "claude"), before)

    def test_a4c_resume_refuses_a_stub_later_on_the_path(self):
        d, out, before = self.fake_killed("o4d")
        stubs = self.sb.dir / "stubs"
        stubs.mkdir()
        stub = stubs / "claude"
        stub.write_text(f"#!/bin/sh\n# plain\necho called >> {self.sb.dir}/stub.log\n")
        stub.chmod(0o755)
        (d / "claude").unlink()
        plan = json.loads((out / "plan.json").read_text())
        plan["child_path"] = f"{d}:{stubs}"
        (out / "plan.json").write_text(json.dumps(plan))
        p = self.sb.run("--resume", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((self.sb.dir / "stub.log").exists())

    def live_plan(self, name):
        """A hand-written live plan whose pinned, marker-free, logging `claude` stub sits on the sandbox PATH."""
        stub = self.sb.bin / "claude"
        stub.write_text(f"#!/bin/sh\necho called >> {self.sb.dir}/stub.log\n")
        stub.chmod(0o755)
        real = os.path.realpath(stub)
        out = self.sb.dir / name
        out.mkdir(mode=0o700)
        (out / "plan.json").write_text(json.dumps({
            "mode": "live", "harness": "claude-code", "spend": True, "model": "MODEL_NAME", "fake_bin": None,
            "child_path": None, "run_id": "ns-live", "release": None,
            "sessions": {"restart": "cli:x", "coordinator": "cli:c"},
            "exe": {"path": real, "sha256": hashlib.sha256(Path(real).read_bytes()).hexdigest()}}))
        return out

    def no_stub_call(self, out):
        self.assertFalse((self.sb.dir / "stub.log").exists())
        self.assertFalse((out / "calls.jsonl").exists())

    def test_a4c_live_resume_needs_consent_again(self):
        out = self.live_plan("o4e")
        p = self.sb.run("--resume", "--model", "MODEL_NAME", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--i-accept-spend", p.stderr)
        self.no_stub_call(out)

    def test_a4c_live_resume_needs_the_model_again(self):
        out = self.live_plan("o4f")
        p = self.sb.run("--resume", "--i-accept-spend", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("needs --model", p.stderr)
        self.no_stub_call(out)

    def test_a4c_live_resume_refuses_another_model(self):
        out = self.live_plan("o4g")
        p = self.sb.run("--resume", "--i-accept-spend", "--model", "OTHER", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("differs", p.stderr)
        self.no_stub_call(out)

    def test_g1_internal_coordinator_rechecks_a_stripped_marker(self):
        d, out, before = self.fake_killed("o4h")
        (d / "claude").write_text((d / "claude").read_text().replace(MARKER, "plain"))
        p = self.sb.run("--internal-coordinator", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(self.sb.log(d, "claude"), before)

    def test_g1_internal_coordinator_live_needs_consent(self):
        out = self.live_plan("o4i")
        p = self.sb.run("--internal-coordinator", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.no_stub_call(out)
        p = self.sb.run("--internal-coordinator", out, "--i-accept-spend")
        self.assertEqual(p.returncode, 2, "--model is required too")
        self.no_stub_call(out)

    def test_a4d_receipt_holds_no_absolute_path(self):
        d = self.sb.dir / "home" / "u" / "fb"
        d.parent.mkdir()
        gate, out = self.sb.dir / "gate-e2", self.sb.dir / "out-e2"
        self.sb.fake("claude", gate, d)
        self.sb.outs.append(out)
        p = self.sb.run("--fake-harness", "claude-code", "--fake-bin", d, "--gate", gate, "--out", out)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = receipt(out)
        for job in r["jobs"]:
            self.assertNotIn("/", job["executable"])
        self.assertNotIn(str(self.sb.dir), (out / "receipt.json").read_text())
        self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 0)

    def test_a4b_fake_bin_must_hold_a_marked_binary(self):
        marked, plain, empty = self.sb.dir / "marked", self.sb.dir / "plain", self.sb.dir / "empty"
        empty.mkdir()
        self.sb.fake("claude", directory=plain, marker=False)
        for d in (plain, empty):
            p = self.sb.run("--fake-harness", "claude-code", "--fake-bin", d, "--out", self.sb.dir / "o")
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertEqual(self.sb.log(d, "claude"), [])

    # --- A7
    def test_a7_resume_twice_does_nothing_more(self):
        out, p = self.sb.offline("o7", "--stop-after", "action")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        answer, njobs = sha(out / "project/answer.md"), len(ledger(out)["jobs"])
        for _ in range(2):
            r = self.sb.run("--resume", "--out", out)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # unfinished: no receipt yet
            self.assertIn("already delivered to this session", r.stdout)
            self.assertEqual(len((out / "actions.jsonl").read_text().splitlines()), 1)
            self.assertEqual(sha(out / "project/answer.md"), answer)
            self.assertEqual(len(ledger(out)["jobs"]), njobs)

    # --- A8
    def test_a8_failed_worker_is_not_ready(self):
        out, p = self.sb.offline("o8", "--fail-always", "worker")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        r = receipt(out)
        job = r["jobs"][0]["id"]
        self.assertTrue(any(job in reason for reason in r["ready"]["reasons"]), r["ready"])
        self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 1)

    def test_a8_held_worker_is_not_banked(self):
        out, p = self.sb.offline("o8h", "--hold", "worker", "--wait-timeout", "2")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("is not banked", p.stdout + p.stderr)
        job = json.loads((out / "coordinator.json").read_text())["worker_job"]
        pid = json.loads((out / "project/.ai-continuity/ledger/runs" / job / "start.json").read_text())["pid"]
        code, _ = self.sb.cont(out, "cancel", "--job", job, "--force")
        self.assertIn(code, (0, 1), code)
        end = time.time() + 20
        while alive(pid) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(alive(pid))

    # --- A9
    def test_a9a_bystander_next_is_left_alone(self):
        out, p = self.sb.offline("o9a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = receipt(out)
        code, _ = self.sb.cont(out, "checkpoint", "--state", "bystander state", "--next", "native-system ns-other: check worker job jother and publish answer",
                               "--evidence", "input.txt", "--session", "cli:bystander-1")
        self.assertEqual(code, 0)
        p = self.sb.run("--resume", "--out", out)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        _, shown = self.sb.cont(out, "resume", "show", "--session", r["sessions"]["restart"])
        self.assertIsNone(shown["consumed_by"])

    def test_a9b_taken_next_means_no_action(self):
        out, p = self.sb.offline("o9b", "--stop-after", "kill")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        code, taken = self.sb.cont(out, "resume", "consume", "--session", "cli:bystander-1", "--cwd",
                                   str(out / "project"), "--source", "startup")
        self.assertTrue(taken["delivered"])
        p = self.sb.run("--resume", "--out", out)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((out / "project/answer.md").exists())
        self.assertFalse((out / "actions.jsonl").exists())
        self.assertFalse((out / "receipt.json").exists())

    def test_a9c_complete_text_cannot_pass_a_rejected_run(self):
        out, p = self.sb.offline("o9c", "--wrong-worker")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        r = receipt(out)
        code, _ = self.sb.cont(out, "checkpoint", "--state", "bystander state",
                               "--next", f"native-system {r['run_id']}: {COMPLETE}", "--evidence", "input.txt",
                               "--session", "cli:bystander-1")
        self.assertEqual(code, 0)
        p = self.sb.run("--resume", "--out", out)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("result: PASS", p.stdout)

    # --- A10
    def test_a10_accepted_failure_ages_out_and_is_re_accepted(self):
        out, p = self.sb.offline("o10", "--fail-first", "checker")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = receipt(out)
        checkers = [j for j in ledger(out)["jobs"].values() if j["role"] == "refuter"]
        self.assertEqual(sorted(j["result"]["outcome"] for j in checkers), ["failed", "succeeded"])
        self.assertEqual(len(r["accepted_failed"]), 1)
        nxt = f"native-system {r['run_id']}: {COMPLETE}"
        for i in range(21):
            code, _ = self.sb.cont(out, "checkpoint", "--state", f"b{i}", "--next", nxt, "--evidence",
                                   "input.txt", "--session", "cli:bystander-1")
            self.assertEqual(code, 0)
        code, ready = self.sb.cont(out, "ready", "--session", r["sessions"]["restart"])  # control: no re-accept
        self.assertEqual(code, 1)
        self.assertTrue(any("is not accepted" in x for x in ready["reasons"]), ready)
        p = self.sb.run("--resume", "--out", out)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        code, ready = self.sb.cont(out, "ready", "--session", r["sessions"]["restart"])
        self.assertEqual(code, 0, ready)
        now = receipt(out)  # F10: the receipt rewritten by the resume hashes the grown calls log
        self.assertEqual(now["calls"]["lines"], len((out / "calls.jsonl").read_bytes().splitlines()))
        self.assertGreater(now["calls"]["lines"], r["calls"]["lines"])
        v = self.verify("--receipt", out / "receipt.json")
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    # --- A13
    def test_a13_wrong_worker_is_rejected_by_an_independent_checker(self):
        out, p = self.sb.offline("o13", "--wrong-worker")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("PASS", p.stdout)
        r = receipt(out)
        worker = next(j for j in r["jobs"] if j["step"] == "worker")
        checker = next(j for j in r["jobs"] if j["step"] == "checker")
        self.assertEqual((checker["role"], worker["role"]), ("refuter", "researcher"))
        self.assertNotEqual(checker["id"], worker["id"])
        self.assertEqual(r["checker_verdict"], "REJECTED")
        prompt = (out / "project/.ai-continuity/ledger/spool" / f"{checker['id']}.prompt").read_text()
        claimed = (out / "project/input.txt").read_text().split()
        self.assertIn(f"Claimed sum: {sum(map(int, claimed)) + 1}", prompt)
        self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 1)

    # --- U05-F follow-ups
    def args(self, **kw):
        return argparse.Namespace(**dict(dict(i_accept_spend=False, model=None, stop_after=None), **kw))

    def in_process(self, fn, *a):
        """Call a run.py function in this process under the sandbox env; return (result, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.sb.env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            result = fn(*a)
        return result, out.getvalue(), err.getvalue()

    def test_f1_act_rechecks_the_pin_before_the_checker_run(self):
        d, out, before = self.fake_killed("of1")
        plan = json.loads((out / "plan.json").read_text())
        with (d / "claude").open("a") as handle:  # still marked and runnable, but no longer the pinned bytes
            handle.write("\n# swapped after the entry check\n")
        rc, so, se = self.in_process(run_mod.restart, out, plan["sessions"]["restart"], self.args())
        self.assertEqual(rc, 2, so + se)
        self.assertIn("differs", se)
        self.assertEqual(self.sb.log(d, "claude"), before)  # the checker never ran

    def test_f2_fake_resume_refuses_a_changed_pinned_binary(self):
        d, out, before = self.fake_killed("of2")
        with (d / "claude").open("a") as handle:
            handle.write("\n# one more byte, marker intact\n")
        p = self.sb.run("--resume", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("differs from the one pinned", p.stderr)
        self.assertEqual(self.sb.log(d, "claude"), before)

    def test_f2_live_resume_refuses_a_changed_pinned_binary(self):
        out = self.live_plan("of2l")
        with (self.sb.bin / "claude").open("a") as handle:
            handle.write("# changed\n")
        p = self.sb.run("--resume", "--i-accept-spend", "--model", "MODEL_NAME", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("differs from the one pinned", p.stderr)
        self.no_stub_call(out)

    def test_f2_live_resume_refuses_a_marked_binary(self):
        out = self.live_plan("of2m")
        stub = self.sb.bin / "claude"
        stub.write_text(f"#!/bin/sh\n# {MARKER}\necho called >> {self.sb.dir}/stub.log\n")
        plan = json.loads((out / "plan.json").read_text())
        plan["exe"]["sha256"] = sha(stub)  # the pin matches: only the marker check can refuse
        (out / "plan.json").write_text(json.dumps(plan))
        p = self.sb.run("--resume", "--i-accept-spend", "--model", "MODEL_NAME", "--out", out)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("is a fake harness", p.stderr)
        self.no_stub_call(out)

    def test_f3_readme_names_what_to_share_and_the_live_check(self):
        text = " ".join((EX / "README.md").read_text().split())
        self.assertIn("share `<out>/receipt.json` only", text)
        self.assertIn("verify_receipt.py --expect-mode live", text)
        self.assertIn("same-user", text)

    def fake_with(self, name, old, new):
        d = self.sb.dir / f"fb-{name}"
        gate, out = self.sb.dir / f"gate-{name}", self.sb.dir / name
        path = self.sb.fake("claude", gate, d)
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))
        self.sb.outs.append(out)
        return self.sb.run("--fake-harness", "claude-code", "--fake-bin", d, "--gate", gate, "--out", out), out

    def test_f4_harness_version_is_printable_and_slashless(self):
        p, out = self.fake_with("of4v", 'print("fake 1.0")', 'print("v1 /opt/x\\x07tail")')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        versions = [j["harness_version"] for j in receipt(out)["jobs"]]
        self.assertTrue(versions and all(v and v.isprintable() and "/" not in v for v in versions), versions)
        self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 0)

    def test_f4_a_non_integer_answer_is_not_printed(self):
        p, out = self.fake_with("of4a", 'text = str(sum(int(x) for x in open("input.txt").read().split()))',
                                'text = "evil /etc/shadow"')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("evil", p.stdout + p.stderr)
        self.assertIn("answer: sha256:", p.stdout)
        self.assertNotIn("evil", (out / "receipt.json").read_text())

    def test_f4_ready_reasons_with_a_slash_are_redacted(self):
        got = run_mod.clean_reasons(["job j1 failed and is not accepted", "see /srv/u/run/x", 7])
        self.assertEqual(got[0], "job j1 failed and is not accepted")
        self.assertTrue(all("/" not in g for g in got) and len(got) == 3, got)

    def test_f5_latest_run_must_be_exactly_runs_id(self):
        out = self.sb.dir / "of5"
        out.mkdir(mode=0o700)
        for value in ("../x", "/etc", "runs/../..", "runs/a/b", "runs/", "", 7):
            (out / "latest.json").write_text(json.dumps({"run": value}))
            r = self.sb.run("--resume", "--out", out)
            self.assertEqual(r.returncode, 2, (value, r.stdout, r.stderr))
            self.assertIn("latest.json", r.stderr)
            v = self.verify("--out", out)
            self.assertEqual(v.returncode, 1, (value, v.stdout))

    def test_f6_out_that_is_not_an_earlier_run_is_refused(self):
        junk = self.sb.dir / "junk"
        junk.mkdir(mode=0o755)
        (junk / "keep.txt").write_text("mine")
        p = self.sb.run("--offline", "--out", junk)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual((junk / "keep.txt").read_text(), "mine")
        self.assertEqual(sorted(x.name for x in junk.iterdir()), ["keep.txt"])
        self.assertEqual(junk.stat().st_mode & 0o777, 0o755)  # not even chmod'ed

    def test_f6_out_below_a_symlink_is_refused(self):
        real, link = self.sb.dir / "real", self.sb.dir / "link"
        real.mkdir()
        link.symlink_to(real)
        p = self.sb.run("--offline", "--out", link / "sub")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(list(real.iterdir()), [])

    def test_f6_write_atomic_never_follows_a_planted_symlink(self):
        d = self.sb.dir / "wa"
        d.mkdir()
        victim = self.sb.dir / "victim"
        (d / f".f.json.{os.getpid()}.tmp").symlink_to(victim)  # the old, predictable temp name
        run_mod.write_atomic(d / "f.json", "payload")
        self.assertFalse(victim.exists())
        self.assertEqual((d / "f.json").read_text(), "payload")
        self.assertEqual((d / "f.json").stat().st_mode & 0o777, 0o600)

    def test_f7_losing_the_consume_race_exits_non_zero(self):
        out, p = self.sb.offline("of7b", "--stop-after", "kill")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rid = json.loads((out / "plan.json").read_text())["run_id"]

        def cont(rd, plan, who, *a):
            if a[:2] == ("resume", "show"):
                return 0, {"delivered": False, "consumed_by": None,
                           "next": f"native-system {rid}: check worker job j1 and publish answer"}
            if a[:2] == ("resume", "consume"):
                return 0, {"delivered": False, "consumed_by": "cli:rival", "notice": "taken"}
            raise AssertionError(a)

        with mock.patch.object(run_mod, "cont", cont):
            rc, so, se = self.in_process(run_mod.restart, out, "cli:z", self.args())
        self.assertEqual(rc, 3, so + se)


    def test_r9f3_plan_json_fifo_or_oversize_is_refused_not_read(self):
        out = self.sb.dir / "ofifo"
        out.mkdir(mode=0o700)
        os.mkfifo(out / "plan.json")
        # These runs are refused by the lstat-based regular() check before the bounded reader is reached; the
        # reader itself is covered by test_r10f4_the_bounded_reader_refuses_non_regular_files_on_the_descriptor.
        r = self.sb.run("--resume", "--out", out, timeout=10)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        (out / "plan.json").unlink()
        (out / "plan.json").write_text(" " * (run_mod.MAX_JSON + 1) + "{}")
        r = self.sb.run("--resume", "--out", out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        (out / "plan.json").unlink()
        (out / "plan.json").symlink_to("/dev/zero")
        r = self.sb.run("--resume", "--out", out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_r9f6_symlinked_runs_or_run_directory_is_refused(self):
        elsewhere = self.sb.dir / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        # runs/ itself a link: a second run into an out dir that already holds a run
        out, p = self.sb.offline("of6a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (out / "runs").symlink_to(elsewhere)
        r = self.sb.run("--offline", "--out", out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])
        # runs/<id> a link, named by latest.json
        out2 = self.sb.dir / "of6b"
        out2.mkdir(mode=0o700)
        (out2 / "runs").mkdir(mode=0o700)
        (out2 / "runs" / "ns-abcd1234").symlink_to(elsewhere)
        (out2 / "latest.json").write_text(json.dumps({"run": "runs/ns-abcd1234"}))
        rd, code = run_mod.resolve_run(out2)
        self.assertIsNone(rd)
        self.assertEqual(code, 2)
        # runs/ a link, named by latest.json
        out3 = self.sb.dir / "of6c"
        out3.mkdir(mode=0o700)
        (out3 / "runs").symlink_to(elsewhere)
        (out3 / "latest.json").write_text(json.dumps({"run": "runs/ns-abcd1234"}))
        rd, code = run_mod.resolve_run(out3)
        self.assertIsNone(rd)
        self.assertEqual(code, 2)

    def test_r9f7_runs_as_a_regular_file_is_a_clean_refusal(self):
        out, p = self.sb.offline("of7a")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (out / "runs").write_text("x")
        r = self.sb.run("--offline", "--out", out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_r9f7_dangling_project_symlink_is_a_clean_refusal(self):
        out, p = self.sb.offline("of7b")  # an earlier run, so --out passes the "holds an earlier run" check
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        shutil.rmtree(out / "project")
        (out / "receipt.json").unlink(missing_ok=True)
        (out / "project").symlink_to(self.sb.dir / "nowhere")
        r = self.sb.run("--offline", "--out", out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_r9f7_new_run_refuses_a_planted_run_id_entry(self):
        out, p = self.sb.offline("of7c")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        elsewhere = self.sb.dir / "of7c-elsewhere"
        elsewhere.mkdir(mode=0o700)
        (out / "runs").mkdir(mode=0o700)
        real = run_mod.uuid.uuid4
        class Fixed:
            hex = "abcd1234" + "0" * 24
        (out / "runs" / "ns-abcd1234").symlink_to(elsewhere)
        run_mod.uuid.uuid4 = lambda: Fixed()
        try:
            args = types.SimpleNamespace(out=str(out), gate=None, hold=False, fail_always=False,
                                         wrong_worker=False, fail_first=False, wait_timeout=1)
            with self.assertRaises(SystemExit) as cm:
                run_mod.new_run(args, {"mode": "offline", "spend": False})
        finally:
            run_mod.uuid.uuid4 = real
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_r9f3_latest_json_alone_must_have_the_shape_this_script_writes(self):
        junk = self.sb.dir / "jl"
        junk.mkdir(mode=0o700)
        (junk / "keep.txt").write_text("mine")
        (junk / "latest.json").write_text(json.dumps({"hello": "world"}))
        p = self.sb.run("--offline", "--out", junk)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(sorted(x.name for x in junk.iterdir()), ["keep.txt", "latest.json"])

    def test_r9f3_failure_text_never_carries_a_path(self):
        self.assertEqual(run_mod.safe_text({"error": "boom at /srv/u/x"}), "[redacted: contains a path]")
        self.assertEqual(run_mod.safe_text({"error": "plain"}), "plain")
        self.assertEqual(run_mod.safe_text({"x": 1}), "no detail")

    def test_r9f3_restart_without_a_next_prints_no_raw_shown(self):
        out, p = self.sb.offline("of9a", "--stop-after", "kill")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

        def cont(rd, plan, who, *a):
            return 0, {"delivered": False, "consumed_by": "cli:rival", "state": HOME_FRAGMENT + "x/secret", "notice": "n"}

        with mock.patch.object(run_mod, "cont", cont):
            rc, so, se = self.in_process(run_mod.restart, out, "cli:z", self.args())
        self.assertEqual(rc, 3)
        self.assertNotIn(HOME_FRAGMENT + "x", so + se)

    def test_r9f3_coordinator_json_keeps_only_redacted_reasons(self):
        out, p = self.sb.offline("of9b", "--stop-after", "kill")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        plan = json.loads((out / "plan.json").read_text())
        co = json.loads((out / "coordinator.json").read_text())

        def cont(rd, plan_, who, *a):
            if a[0] == "ready":
                return 1, {"ready": False, "reasons": ["blocked at /srv/u/secret", "plain reason"]}
            return 0, {}

        with mock.patch.object(run_mod, "cont", cont), mock.patch.object(run_mod, "build_receipt"):
            self.in_process(run_mod.finish, out, plan, co, True, plan["sessions"]["restart"])
        saved = json.loads((out / "coordinator.json").read_text())
        self.assertEqual(saved["ready"]["reasons"], ["[redacted: contains a path]", "plain reason"])

    def test_r10f4_resume_with_a_fifo_plan_in_a_named_run_does_not_hang(self):
        out = self.sb.dir / "ofifo2"
        (out / "runs" / "r1").mkdir(parents=True, mode=0o700)
        out.chmod(0o700)
        (out / "latest.json").write_text(json.dumps({"run": "runs/r1"}))
        os.mkfifo(out / "runs" / "r1" / "plan.json")
        r = self.sb.run("--resume", "--out", out, timeout=10)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not a regular file", r.stderr)
        (out / "runs" / "r1" / "plan.json").unlink()
        (out / "latest.json").write_text(json.dumps({"run": "."}))
        os.mkfifo(out / "plan.json")
        r = self.sb.run("--resume", "--out", out, timeout=10)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not a regular file", r.stderr)

    def test_r9f5_a_symlinked_plan_json_is_refused_even_when_it_points_at_a_regular_file(self):
        out = self.sb.dir / "osym"
        out.mkdir(mode=0o700)
        (self.sb.dir / "real-plan.json").write_text("{}")
        (out / "plan.json").symlink_to(self.sb.dir / "real-plan.json")
        (out / "latest.json").write_text(json.dumps({"run": "."}))
        r = self.sb.run("--resume", "--out", out, timeout=10)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("plan.json is not a regular file", r.stderr)

    def test_r10f4_the_bounded_reader_refuses_non_regular_files_on_the_descriptor(self):
        d = self.sb.dir / "rb"
        d.mkdir()
        os.mkfifo(d / "f")
        (d / "l").symlink_to("/dev/zero")
        for name in ("f", "l", "missing"):
            with self.assertRaises(ValueError, msg=name), deadline():
                run_mod.read_json_bytes(d / name)
        (d / "big").write_text(" " * (run_mod.MAX_JSON + 1))
        with self.assertRaises(ValueError):
            run_mod.read_json_bytes(d / "big")
        (d / "ok").write_text("{}")
        self.assertEqual(run_mod.read_json_bytes(d / "ok"), b"{}")
        # the descriptor check is the real guard: a FIFO opened non-blocking is refused by fstat, not read as empty
        with self.assertRaisesRegex(ValueError, "not a regular file"), deadline():
            run_mod.read_json_bytes(d / "f")

    def test_r10f4_earlier_run_never_blocks_on_a_fifo_latest_json(self):
        d = self.sb.dir / "ef"
        d.mkdir(mode=0o700)
        os.mkfifo(d / "latest.json")
        with deadline():
            self.assertFalse(run_mod.earlier_run(d))

    def test_r10f4_a_latest_json_that_is_not_a_regular_file_is_refused(self):
        out = self.sb.dir / "olatest"
        out.mkdir(mode=0o700)
        (out / "plan.json").write_text("{}")  # so --out counts as an earlier run
        os.mkfifo(out / "latest.json")
        r = self.sb.run("--resume", "--out", out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("latest.json is not a regular file", r.stdout + r.stderr)
        (out / "latest.json").unlink()
        (out / "latest.json").symlink_to(self.sb.dir)
        rd, code = run_mod.resolve_run(out)
        self.assertIsNone(rd)
        self.assertEqual(code, 2)

    def test_r10f4_safe_text_filters_control_characters_and_path_forms(self):
        for bad in ("at ~/x", "C:\\Users\\u", "a/b", "x\x1b[31m/etc", "tilde ~ alone", "back\\slash"):
            self.assertEqual(run_mod.safe_text({"error": bad}), "[redacted: contains a path]", bad)
        self.assertEqual(run_mod.safe_text({"error": "a\x1b[31mb\x00c\nd"}), "a[31mbcd")
        self.assertEqual(len(run_mod.safe_text("x" * 500)), 200)

    def test_r9f5_clean_reasons_parity_on_controls_and_paths(self):
        got = run_mod.clean_reasons(["at ~/x", "at ~bob/x", "C:\\Users\\u", "x\x1b[31m/etc", "usage ~80% of cap",
                                     "a\x1b[31mb\x00c\nd", "two\nlines", 7, None])
        self.assertEqual(got[:4], ["[redacted: contains a path]"] * 4)
        self.assertEqual(got[4], "usage ~80% of cap")
        self.assertEqual(got[5], "a[31mbc d")
        self.assertEqual(got[6], "two lines")
        self.assertEqual(got[7:], ["[redacted: contains a path]"] * 2)
        self.assertEqual(run_mod.clean_reasons(None), [])
        self.assertEqual(run_mod.clean_reasons(["a\tb", "c\rd", "e\r\nf"]), ["a b", "c d", "e  f"])
        self.assertEqual(run_mod.clean_reasons(["~", "~bob"]), ["~", "~bob"])  # bare tilde kept, unlike safe_text
        self.assertEqual(len(run_mod.clean_reasons(["x" * 500])[0]), 200)

    def test_r9f5_the_real_receipt_holds_no_path_bearing_reason(self):
        out, p = self.sb.offline("of11")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        plan = json.loads((out / "plan.json").read_text())
        co = json.loads((out / "coordinator.json").read_text())
        co["ready"] = {"exit": 1, "ready": False, "reasons": ["blocked at /srv/u/secret", "C:\\x\\y", "plain reason"]}
        self.in_process(run_mod.build_receipt, out, plan, co)  # the real one: no mock
        text = (out / "receipt.json").read_text()
        rec = json.loads(text)
        self.assertEqual(rec["ready"]["reasons"], ["[redacted: contains a path]"] * 2 + ["plain reason"])
        self.assertNotIn("/srv/u", text)
        self.assertNotIn("C:", text)

    def test_r10f4_failure_print_sites_print_a_fixed_message_and_exit_class(self):
        out, p = self.sb.offline("of10", "--stop-after", "kill")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        plan = json.loads((out / "plan.json").read_text())
        nasty = {"error": "boom ~ C:\\x \x1b[31mred"}
        cases = [(label, n, calls(n)) for n in (1, 3) for label, calls in
                 (("start", lambda n: [(n, nasty)]), ("checkpoint", lambda n: [(0, {"job_id": "j1"}), (n, nasty)]))]
        for label, n, calls in cases:
            queue = list(calls)

            def cont(rd, plan_, who, *a, queue=queue):
                return queue.pop(0)

            with mock.patch.object(run_mod, "cont", cont):
                rc, so, se = self.in_process(run_mod.coordinator, out, self.args())
            self.assertEqual(rc, 1, label)
            self.assertIn(f"{label} failed (continuity exit {n})", se)
            self.assertNotIn("boom", se + so)  # no echoed free text
            self.assertNotIn("\x1b", se + so)
        co = json.loads((out / "coordinator.json").read_text())

        for n in (1, 3):
            def cont(rd, plan_, who, *a, n=n):
                return (n, nasty) if a[0] == "checkpoint" else (0, {})

            with mock.patch.object(run_mod, "cont", cont):
                rc, so, se = self.in_process(run_mod.finish, out, plan, co, True, plan["sessions"]["restart"])
            self.assertEqual(rc, 1)
            self.assertIn(f"final checkpoint failed (continuity exit {n})", se)
            self.assertNotIn("boom", se + so)
            self.assertNotIn("\x1b", se + so)


class WorkerRole(unittest.TestCase):
    def test_worker_role_has_no_artifact_lens_precondition(self):
        src = (EX / "run.py").read_text()
        role = next(ast.literal_eval(n.value) for n in ast.parse(src).body
                    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "WORKER_ROLE")
        self.assertEqual(role, "researcher")
        agents = ROOT / "agents"
        agent = next(p for p in (agents / f"{role}.md", agents / role / f"{role}.md") if p.is_file())
        body = agent.read_text().lower()
        for phrase in ("lens", "refute", "dispatch only when", "take a dispatch only"):
            self.assertNotIn(phrase, body, f"{agent.name} would refuse a bounded read task")


class CheckerVerdict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("native_run", EX / "run.py")
        cls.nr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.nr)

    def test_normalize(self):
        n = self.nr.normalize_verdict
        self.assertEqual(n("VERDICT: STANDS\nREASON: x\nEVIDENCE: y"), "CONFIRMED")
        self.assertEqual(n("verdict: refuted\nREASON: x"), "REJECTED")
        self.assertEqual(n("CONFIRMED"), "CONFIRMED")
        self.assertEqual(n("REJECTED"), "REJECTED")
        self.assertIsNone(n("VERDICT: UNVERIFIABLE\nREASON: x"))
        self.assertIsNone(n("The sum looks right to me."))
        self.assertIsNone(n(""))
        self.assertIsNone(n(None))

    def test_markdown_decorated_verdict_line(self):
        # live OpenCode reply shape: the refuter's format list rendered as a markdown list
        n = self.nr.normalize_verdict
        self.assertEqual(n("- VERDICT: STANDS\n- REASON: x\n- EVIDENCE: y"), "CONFIRMED")
        self.assertEqual(n("* **VERDICT:** REFUTED"), "REJECTED")
        self.assertEqual(n("`VERDICT: STANDS`"), "CONFIRMED")
        self.assertIsNone(n("- VERDICT: UNVERIFIABLE"))
        self.assertIsNone(n("- note: VERDICT: STANDS"))

    def test_malformed_first_verdict_line_is_not_skipped(self):
        # tribune F1: the first line that looks like a verdict decides; a later clean STANDS cannot win
        n = self.nr.normalize_verdict
        for reply in ("VERDICT: REFUTED (sum is 72, not 73)\nREASON: x\n  VERDICT: STANDS",
                      "## VERDICT: REFUTED\nVERDICT: STANDS",
                      "VERDICT: **REFUTED**\nVERDICT: STANDS",
                      "VERDICT: REFUTED.\n- VERDICT: STANDS",
                      "Verdict - unclear\nVERDICT: STANDS"):
            self.assertNotEqual(n(reply), "CONFIRMED", reply)
        self.assertIsNone(n("VERDICT: REFUTED (sum is 72, not 73)\nVERDICT: STANDS"))
        # tribune F5 mutant: trailing text after the verdict word is not a clean verdict
        self.assertIsNone(n("VERDICT: STANDS - but wrong"))
        self.assertIsNone(n("VERDICT:\nSTANDS"))

    def test_first_verdict_line_wins(self):
        self.assertIsNone(self.nr.normalize_verdict("VERDICT: UNVERIFIABLE\nVERDICT: STANDS"))

    def test_claim_text_cannot_spoof(self):
        prompt = self.nr.CHECKER_PROMPT.format(numbers="1 2", claim="3")
        # only the model's reply is parsed; a verdict quoted mid-line is not a verdict line
        self.assertIsNone(self.nr.normalize_verdict("The claim is fine, VERDICT: STANDS is what you wanted"))
        self.assertNotIn("exactly", prompt)
        self.assertNotRegex(prompt, r"(?im)^\s*VERDICT:")
        self.assertIn("disprove", prompt)


if __name__ == "__main__":
    unittest.main()
