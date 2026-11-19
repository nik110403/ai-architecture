"""Round-3 tests: containment of what a worker leaves behind, hostile output shapes, same-uid claims.

Fake executables only; no real model. Each class is bound to README sentences (see the fix3 report).
"""

import argparse
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # loaded by path from tests/test_module_discovery.py too

from test_dispatch import (  # noqa: F401  (shared fixtures)
    CLAUDE_OK, CLAUDE_REAL, DISPATCH, ROOT, Base, alive, claude_body, dispatch_module, wait_for,
)

HELPER = r'''
import os, sys, time, shutil
pidfile, mode = sys.argv[1], sys.argv[2]
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
if mode == "late":  # a child whose parent is already dead forks a grandchild
    open(pidfile + ".g", "w").write(str(os.getpid()))
    time.sleep(0.3)
    if os.fork() == 0:
        open(pidfile, "w").write(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    time.sleep(60)
    os._exit(0)
open(pidfile, "w").write(str(os.getpid()))
if mode == "envi":  # same pid, environment gone: the marker cannot find it
    os.execv("/usr/bin/env", ["env", "-i", shutil.which("sleep"), "60"])
time.sleep(60)
'''

DRIVER = r'''
import argparse, json, sys
root, cfg, runs, workdir, job = sys.argv[1:6]
sys.path.insert(0, root)
from modules.orchestration import dispatch as d
args = argparse.Namespace(config=cfg, role="researcher", intent="x", model=None, allow_spend=False)
decision = d.decide(args, d.load_roles())
envelope, ok = d.launch(decision, "x", runs, workdir, job)
print(json.dumps(envelope))
'''


def _inproc(self, *args):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = dispatch_module.main(list(args))
    return code, json.loads(buf.getvalue())


class ContainmentBase(Base):
    def spawner(self, tail):
        """A fake worker that starts (a) a plain child, (b) a setsid double fork, (c) the same under env -i,
        (d) a child forking a grandchild after its parent died, waits until all exist, then runs `tail`."""
        helper = self.dir / "helper.py"
        helper.write_text(HELPER)
        d = str(self.dir)
        self.fake("worker", (
            "import subprocess, time\n"
            f"a = subprocess.Popen(['sleep', '60'])\nopen({d + '/a.pid'!r}, 'w').write(str(a.pid))\n"
            f"for name, mode in (('b', 'plain'), ('c', 'envi'), ('d', 'late')):\n"
            f"    subprocess.Popen([sys.executable, {str(helper)!r}, {d!r} + '/' + name + '.pid', mode])\n"
            f"names = ['a.pid', 'b.pid', 'c.pid', 'd.pid', 'd.pid.g']\n"
            f"end = time.time() + 15\n"
            f"while time.time() < end and not all(os.path.exists({d!r} + '/' + n) for n in names):\n"
            f"    time.sleep(0.02)\n"
            f"open({d + '/ready'!r}, 'w').write('1')\n" + tail
        ))

    def pids(self):
        out = {}
        for name in ("a", "b", "c", "d"):
            path = self.dir / f"{name}.pid"
            out[name] = int(path.read_text()) if path.exists() else None
        g = self.dir / "d.pid.g"
        out["dg"] = int(g.read_text()) if g.exists() else None
        for pid in out.values():
            if pid:
                self.addCleanup(lambda p=pid: subprocess.run(["kill", "-9", str(p)], capture_output=True))
        return out

    def start(self, mode, cfg, job):
        if mode == "run":
            argv = [sys.executable, "-B", str(DISPATCH),
                    *self.run_args(cfg, "--role", "researcher", "--intent", "x", "--job-id", job)]
        else:
            driver = self.dir / "driver.py"
            driver.write_text(DRIVER)
            argv = [sys.executable, "-B", str(driver), str(ROOT), cfg, str(self.runs), str(self.dir), job]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def scenario(self, mode, how, job="job-C"):
        """Run the spawner worker under `run` or under `launch()`; `how` is timeout, sigterm or success."""
        self.spawner("time.sleep(60)" if how != "success" else "")
        cfg = self.config(timeout_seconds=5 if how == "timeout" else 600)
        proc = self.start(mode, cfg, job)
        if how == "sigterm":
            self.assertTrue(wait_for(lambda: (self.dir / "ready").exists(), 20), "worker never got ready")
            os.kill(proc.pid, signal.SIGTERM)
        out, err = proc.communicate(timeout=90)
        try:
            envelope = json.loads(out)
        except ValueError:
            self.fail(f"no envelope: {out!r} {err!r}")
        return self.pids(), envelope

    def dead(self, pid):
        return wait_for(lambda: not alive(pid), 10) is not None


class RunContainmentTests(ContainmentBase):
    """`run` arms the subreaper: nothing the worker started stays alive, whatever it did to escape."""

    def check_run(self, how):
        pids, env = self.scenario("run", how)
        for name, pid in pids.items():
            self.assertTrue(pid and self.dead(pid), f"{name} ({pid}) survived `run` after {how}")
        c = env["dispatch"]["containment"]
        self.assertEqual((c["method"], c["survivors"]), ("children+marker", 0), c)
        self.assertGreaterEqual(c["killed"], 4, c)  # b, c, and the late child plus its parent
        self.assertLessEqual(c["killed"], 6, c)
        expect = "succeeded" if how == "success" else "failed"
        self.assertEqual(env["result"]["outcome"], expect, env)
        job = self.runs / "job-C"
        self.assertEqual(json.loads((job / "start.json").read_text())["state"], "finished")
        return env

    def test_timeout_kills_every_escape(self):
        env = self.check_run("timeout")
        self.assertTrue(env["dispatch"]["timed_out"])

    def test_sigterm_kills_every_escape(self):
        env = self.check_run("sigterm")
        self.assertIn("interrupted", env["dispatch"]["note"])

    def test_backgrounded_children_are_killed_after_a_successful_exit(self):
        env = self.check_run("success")
        self.assertEqual(env["dispatch"]["exit_code"], 0)

    def test_survivors_lower_the_outcome_to_failed(self):
        """A descendant that stays listed after the bounded loop: failed, with a note, survivors counted."""
        self.fake("worker", "sys.exit(0)")
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        calls = []

        def listed():  # empty when the subreaper is armed (it refuses with children), then the stuck sleeper
            calls.append(1)
            return set() if len(calls) == 1 else {sleeper.pid}

        with mock.patch.object(dispatch_module, "kill_pid", lambda pid: None), \
                mock.patch.object(dispatch_module, "children_pids", listed), \
                mock.patch.object(dispatch_module, "CLEANUP_SECONDS", 0.3):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["exit_code"]), (1, "failed", 0), out)
        self.assertEqual(out["dispatch"]["containment"]["survivors"], 1)
        self.assertIn("survived containment", out["dispatch"]["note"])

    def test_methods_degrade_honestly_when_the_subreaper_or_proc_is_missing(self):
        self.fake("worker")
        with mock.patch.object(dispatch_module, "arm_subreaper", lambda: False):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        c = out["dispatch"]["containment"]
        self.assertEqual((code, c["method"], c["survivors"]), (0, "marker", None), out)
        with mock.patch.object(sys, "platform", "darwin"):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        c = out["dispatch"]["containment"]
        self.assertEqual((code, c["method"], c["survivors"]), (0, "group", None), out)

    def test_inprocess_run_does_not_leave_the_subreaper_flag_set(self):
        self.fake("worker")
        code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["dispatch"]["containment"]["method"]), (0, "children+marker"), out)
        flag = ctypes.c_int(-1)
        ctypes.CDLL(None).prctl(37, ctypes.byref(flag), 0, 0, 0)  # PR_GET_CHILD_SUBREAPER
        self.assertEqual(flag.value, 0)

    inproc = _inproc


class LibraryContainmentTests(ContainmentBase):
    """`launch()` as a library never arms the subreaper: the marker sweep only, survivors unknown."""

    def check_lib(self, how):
        pids, env = self.scenario("lib", how)
        for name in ("a", "b", "d", "dg"):
            self.assertTrue(pids[name] and self.dead(pids[name]), f"{name} survived the marker sweep after {how}")
        # (c) cleared its environment: the sweep cannot see it. The test says so instead of hiding it.
        self.assertTrue(alive(pids["c"]), "env -i child unexpectedly died; the README escape list would be wrong")
        c = env["dispatch"]["containment"]
        self.assertEqual((c["method"], c["survivors"]), ("marker", None), c)
        self.assertGreaterEqual(c["killed"], 3, c)
        return env

    def test_timeout(self):
        self.check_lib("timeout")

    def test_sigterm(self):
        self.check_lib("sigterm")

    def test_success_with_an_env_i_survivor_holding_the_pipe_fails(self):
        env = self.check_lib("success")
        self.assertEqual(env["result"]["outcome"], "failed")
        self.assertIn("holding the output pipe", env["dispatch"]["note"])

    def test_non_dumpable_child_is_reported_unknown(self):
        self.fake("worker", (
            "import subprocess, time\n"
            "code = 'import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(60)'\n"
            "c = subprocess.Popen([sys.executable, '-c', code], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(self.dir / 'nd.pid')!r}, 'w').write(str(c.pid))\n"
            "time.sleep(1)"
        ))
        cfg = self.config()
        proc = self.start("lib", cfg, "job-N")
        out, err = proc.communicate(timeout=60)
        pid = int((self.dir / "nd.pid").read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        c = json.loads(out)["dispatch"]["containment"]
        self.assertEqual((c["method"], c["survivors"]), ("marker", None))
        self.assertGreaterEqual(c["unknown"], 1, c)
        self.assertTrue(alive(pid), "a non-dumpable child cannot be seen by the sweep")

    def test_marker_differs_for_the_same_job_id_in_two_run_dirs(self):
        self.fake("worker")
        markers = []
        for index in range(2):
            runs = self.dir / f"runs{index}"
            code, out = self.dispatch("run", "--config", self.config(), "--run-dir", str(runs), "--workdir",
                                      str(self.dir), "--role", "researcher", "--intent", "x", "--job-id", "same")
            self.assertEqual(code, 0, out)
            markers.append(self.calls("worker")[index]["environ"]["AI_ARCH_JOB_MARKER"])
        self.assertNotEqual(*markers)
        self.assertEqual(markers[0], dispatch_module.job_marker(self.dir / "runs0" / "same"))

    def test_two_jobs_in_threads_never_touch_each_other(self):
        """Job A's cleanup (even asked for the subreaper) must not kill job B's live worker (seat 1 F7)."""
        self.fake("worker", (
            "import subprocess, time\n"
            "if os.path.exists(os.path.join(here, 'b.go')):\n"
            "    open(os.path.join(here, 'b.live'), 'w').write('1')\n"
            "    time.sleep(4)\n"
            "else:\n"
            "    open(os.path.join(here, 'b.go'), 'w').write('1')\n"
            "    subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
            "    end = time.time() + 15\n"
            "    while time.time() < end and not os.path.exists(os.path.join(here, 'b.live')):\n"
            "        time.sleep(0.02)\n"
        ))
        cfg = self.config()
        args = argparse.Namespace(config=cfg, role="researcher", intent="x", model=None, allow_spend=False)
        decision = dispatch_module.decide(args, dispatch_module.load_roles())
        results = {}

        def job(name):
            results[name] = dispatch_module.launch(decision, "x", self.runs, self.dir, name, subreaper=True)

        a = threading.Thread(target=job, args=("job-A",))
        a.start()
        self.assertTrue(wait_for(lambda: (self.bin / "b.go").exists()))
        b = threading.Thread(target=job, args=("job-B",))
        b.start()
        a.join(60)
        b.join(60)
        self.addCleanup(lambda: subprocess.run(["pkill", "-9", "-f", "sleep 60"], capture_output=True))
        for name in ("job-A", "job-B"):
            envelope, ok = results[name]
            self.assertEqual(envelope["dispatch"]["containment"]["method"], "marker", name)
            self.assertEqual(envelope["dispatch"]["exit_code"], 0, (name, envelope))


class OutputShapeTests(Base):
    """`usage` is rebuilt from an allowlist; whatever the worker shapes, a launched job stays launched."""

    inproc = _inproc

    def raw_claude(self, usage_json, extra=""):
        body = (
            "sys.stdout.write('{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
            f"\"permission_denials\":[],{extra}\"usage\":' + {usage_json!r} + '}}')"
        )
        self.fake("claude", body)
        return self.go(self.config(**self.paid("claude-code")), "--allow-spend")

    def test_hostile_usage_shapes_never_make_a_launched_job_unsupported(self):
        nested = '{"input_tokens":5,"x":' + '{"a":' * 900 + "1" + "}" * 900 + "}"
        rows = [
            ("nested 900", nested, "succeeded", {"input": 5}),
            ("nested 3000", '{"x":' + '{"a":' * 3000 + "1" + "}" * 3000 + "}", "succeeded", None),
            ("Infinity", '{"input_tokens":Infinity,"output_tokens":3}', "succeeded", {"output": 3}),
            ("NaN", '{"input_tokens":NaN}', "succeeded", None),
            ("negative", '{"input_tokens":-5,"output_tokens":2}', "succeeded", {"output": 2}),
            ("string", '{"input_tokens":"9"}', "succeeded", None),
            ("401 digits", '{"input_tokens":' + "9" * 401 + "}", "succeeded", None),
            ("bool", '{"input_tokens":true}', "succeeded", None),
        ]
        for label, usage, outcome, expected in rows:
            with self.subTest(label):
                code, out = self.raw_claude(usage)
                self.assertNotEqual(code, 2, out)
                self.assertEqual(out["result"]["outcome"], outcome, out)
                job = self.runs / out["dispatch"]["job_id"]
                text = (job / "result.json").read_text()
                self.assertNotRegex(text, r"\bNaN\b|Infinity")
                self.assertEqual(json.loads(text), out)
                self.assertEqual(json.loads((job / "start.json").read_text())["state"], "finished")
                json.dumps(out, allow_nan=False)  # strict JSON all the way
                if outcome == "succeeded":
                    self.assertEqual(out["dispatch"]["usage"], expected)

    def test_infinite_cost_and_negative_cost_are_dropped(self):
        for cost in ("Infinity", "-1"):
            code, out = self.raw_claude('{"input_tokens":1}', extra=f'"total_cost_usd":{cost},')
            self.assertEqual((code, out["dispatch"]["usage"]), (0, {"input": 1}), out)

    def test_claude_permission_denials_null_fails(self):
        code, out = self.raw_claude('{"input_tokens":1}', extra='"permission_denials":null,')
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"), out)

    def test_the_serialiser_is_strict_json(self):
        for bad in (float("nan"), float("inf"), {"a": [float("-inf")]}):
            with self.assertRaises(ValueError):
                dispatch_module.dump_json(bad)

    def test_forced_serialiser_failure_gives_the_minimal_envelope(self):
        self.fake("worker", "print('hi')")
        real = dispatch_module.dump_json

        def flaky(obj):
            if isinstance(obj, dict) and "dispatch" in obj:
                raise ValueError("boom")
            return real(obj)

        with mock.patch.object(dispatch_module, "dump_json", flaky):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x",
                                                   "--job-id", "job-X"))
        self.assertEqual(code, 1)
        self.assertEqual((out["job_id"], out["outcome"], out["exit"], out["note"]),
                         ("job-X", "failed", 1, "envelope could not be serialised"))
        self.assertEqual(out["result"]["outcome"], "failed")
        job = self.runs / "job-X"
        self.assertEqual(json.loads((job / "result.json").read_text()), out)
        self.assertEqual(json.loads((job / "start.json").read_text())["state"], "finished")

    def test_opencode_usage_is_the_same_allowlist(self):
        from test_dispatch import STEP, events_body, finish
        ev = finish(tokens={"input": 7, "output": 1, "reasoning": 99, "total": 100, "cache": {"read": 2, "write": 3}},
                    cost=0.5)
        self.fake("opencode", events_body(STEP, ev))
        code, out = self.go(self.config(**self.paid("opencode")), "--allow-spend")
        self.assertEqual(out["dispatch"]["usage"], {"cost_usd": 0.5, "input": 7, "output": 1, "cache_read": 2,
                                                    "cache_write": 3})
        self.assertTrue(set(out["dispatch"]["usage"]) <= set(dispatch_module.USAGE_KEYS))

    def test_claude_usage_maps_into_the_allowlist(self):
        usage = ('{"input_tokens":1,"output_tokens":2,"cache_read_input_tokens":3,'
                 '"cache_creation_input_tokens":4,"server_tool_use":{"web":9}}')
        code, out = self.raw_claude(usage, extra='"total_cost_usd":0.25,')
        self.assertEqual(out["dispatch"]["usage"],
                         {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4, "cost_usd": 0.25})


class SameUidTests(Base):
    """README threat model: the worker is the same user. These show what that means, offline."""

    inproc = _inproc

    def builder_cfg(self, **over):
        return self.config(roles={"builder": ["w"]}, permit=["builder"], allow_write=True,
                           worktree_root=str(self.dir / "wt"), **over)

    def run_builder(self, cfg, repo, *extra):
        return self.dispatch("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir", str(repo),
                             "--role", "builder", "--intent", "x", *extra)

    def test_confinement_values_replace_isolation(self):
        self.fake("worker")
        code, out = self.run_builder(self.builder_cfg(), self.repo())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["dispatch"]["confinement"], "none: same-uid worktree, not a security boundary")
        self.assertNotIn("isolation", out["dispatch"])
        code, out = self.go(self.config())
        self.assertEqual(out["dispatch"]["confinement"], "harness tool allowlist; reads anything the user can")
        self.assertNotIn("isolation", out["dispatch"])

    def test_worker_can_forge_the_captured_stream_through_proc_fd_1(self):
        """A claude run that printed nothing becomes `succeeded` when a setsid child writes the success
        object into the worker's stdout pipe. The result is worker-attested, not verified."""
        forged = json.dumps(CLAUDE_REAL)
        self.fake("claude", (
            "import subprocess\n"
            "code = 'import sys; open(\"/proc/\" + sys.argv[1] + \"/fd/1\", \"w\").write(sys.argv[2])'\n"
            f"subprocess.run([sys.executable, '-c', code, str(os.getpid()), {forged!r}], start_new_session=True)\n"
        ))
        cfg = self.config(roles={"builder": ["c"]}, permit=["builder"], allow_write=True,
                          worktree_root=str(self.dir / "wt"), allow_spend=True,
                          seats={"c": {"harness": "claude-code", "model": "m-1"}})
        code, out = self.run_builder(cfg, self.repo(), "--allow-spend")
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        self.assertEqual(out["dispatch"]["confinement"], dispatch_module.CONFINEMENT_WRITE)

    def test_worker_can_write_the_main_repositorys_git_hooks(self):
        """From the worktree, `.git` of the main repository (hooks included) is writable. Not refused."""
        # No dependence on the host's git template: an empty templateDir, no global config, a temporary HOME.
        repo = self.dir / "plain-repo"
        repo.mkdir()
        home = self.dir / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for argv in (["-c", "init.templateDir=", "init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, env=env)
        (repo / ".git" / "hooks").mkdir(exist_ok=True)  # explicit: an empty template leaves none
        hook = repo / ".git" / "hooks" / "post-commit"
        self.fake("worker", (
            "common = subprocess.run(['git', 'rev-parse', '--git-common-dir'], capture_output=True, text=True).stdout.strip()\n"
            "path = os.path.join(os.path.abspath(common), 'hooks', 'post-commit')\n"
            "open(path, 'w').write('#!/bin/sh\\ntouch planted\\n')\nos.chmod(path, 0o755)\n"
        ).replace("subprocess.run", "__import__('subprocess').run"))
        code, out = self.run_builder(self.builder_cfg(), repo)
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        self.assertTrue(hook.exists() and os.access(hook, os.X_OK), "the worker could not write main .git/hooks")

    def test_worker_reads_the_dispatchers_environment_through_proc_ppid(self):
        self.fake("worker", (
            f"open({str(self.dir / 'seen')!r}, 'wb').write(open('/proc/%d/environ' % os.getppid(), 'rb').read())"
        ))
        code, out = self.dispatch(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"),
                                  env_extra={"UNLISTED_SECRET": "hunter2"})
        self.assertEqual(code, 0, out)
        self.assertIn(b"UNLISTED_SECRET=hunter2", (self.dir / "seen").read_bytes())
        self.assertNotIn("UNLISTED_SECRET", self.calls("worker")[0]["environ"])

    def test_worker_can_kill_the_dispatcher_and_rewrite_the_job_files(self):
        job = self.runs / "job-K"
        forged = json.dumps({"result": {"event_id": "job-K", "outcome": "succeeded", "evidence": [],
                                        "finished_at": "2026-01-01T00:00:00Z"}, "dispatch": {"forged": True}})
        self.fake("worker", (
            "import time\n"
            "os.kill(os.getppid(), 9)\ntime.sleep(0.3)\n"
            f"open({str(job.resolve() / 'result.json')!r}, 'w').write({forged!r})\n"
        ))
        proc = subprocess.Popen(
            [sys.executable, "-B", str(DISPATCH), *self.run_args(self.config(), "--role", "researcher",
                                                                "--intent", "x", "--job-id", "job-K")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, -9)
        self.assertTrue(wait_for(lambda: (job / "result.json").exists() and "forged" in (job / "result.json").read_text()))
        self.assertEqual(json.loads((job / "start.json").read_text())["state"], "running")

    def test_worker_can_stop_the_dispatcher_and_nothing_enforces_the_deadline(self):
        self.fake("worker", "import time\nos.kill(os.getppid(), 19)\ntime.sleep(30)\n")
        proc = subprocess.Popen(
            [sys.executable, "-B", str(DISPATCH), *self.run_args(self.config(timeout_seconds=1), "--role",
                                                                "researcher", "--intent", "x", "--job-id", "job-T")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(wait_for(lambda: dispatch_module.proc_state(proc.pid) == "T"), "dispatcher not stopped")
        time.sleep(2.5)  # past the 1 s deadline
        job = self.runs / "job-T"
        start = json.loads((job / "start.json").read_text())
        self.assertEqual(start["state"], "running")
        self.assertFalse((job / "result.json").exists(), "a stopped dispatcher enforced its deadline")
        self.assertEqual(dispatch_module.proc_state(proc.pid), "T")
        os.killpg(start["pgid"], signal.SIGKILL)
        proc.kill()

    def test_agent_docs_say_the_dispatcher_grants_the_roles_tool_list(self):
        sentence = "The dispatcher grants exactly the role's tool list."
        named = {"builder", "design-builder", "pipeline-runner", "surveyor", "steward", "browser-worker"}
        for path in sorted((ROOT / "agents").glob("*.md")):
            text = path.read_text()
            self.assertNotIn("does not grant tools", text, path.name)
            if path.stem in named:
                self.assertIn(sentence, text, path.name)


class SmallItemTests(Base):
    inproc = _inproc

    def test_env_allow_path_does_not_undo_the_absolute_only_rule(self):
        self.fake("worker")
        seat = {"harness": "local-command", "command": [str(self.bin / "worker")], "env_allow": ["PATH"]}
        code, out = self.dispatch(*self.run_args(self.config(seats={"w": seat}), "--role", "researcher",
                                                 "--intent", "x"),
                                  env_extra={"PATH": f"rel{os.pathsep}{os.environ['PATH']}{os.pathsep}."})
        self.assertEqual(code, 0, out)
        path = self.calls("worker")[0]["environ"]["PATH"]
        self.assertTrue(all(os.path.isabs(d) for d in path.split(os.pathsep)), path)

    def test_all_relative_path_becomes_the_fixed_safe_path_never_empty(self):
        self.fake("worker")
        for allow in ([], ["PATH"]):
            seat = {"harness": "local-command", "command": [str(self.bin / "worker")], "env_allow": allow}
            code, out = self.dispatch(*self.run_args(self.config(seats={"w": seat}), "--role", "researcher",
                                                     "--intent", "x"),
                                      env_extra={"PATH": f"rel{os.pathsep}.{os.pathsep}"})
            self.assertEqual(code, 0, out)
            self.assertEqual(self.calls("worker")[-1]["environ"]["PATH"], dispatch_module.SAFE_PATH)

    def test_bare_git_with_an_all_relative_path_is_refused_and_a_planted_git_never_runs(self):
        planted = self.dir / "planted.ran"
        (self.dir / "git").write_text(f"#!/bin/sh\ntouch {planted}\nexit 1\n")
        (self.dir / "git").chmod(0o755)
        self.fake("worker")
        cfg = self.config(roles={"builder": ["w"]}, permit=["builder"], allow_write=True,
                          worktree_root=str(self.dir / "wt"))
        code, out = self.dispatch("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir",
                                  str(self.repo()), "--role", "builder", "--intent", "x",
                                  env_extra={"PATH": f".{os.pathsep}{os.pathsep}rel"})
        self.assertEqual(code, 2, out)
        self.assertIn("git unavailable", out["dispatch"]["refused"])
        self.assertFalse(planted.exists(), "a cwd-relative git ran")

    def test_worktree_root_gets_the_run_dir_owner_and_mode_check(self):
        self.fake("worker")
        root = self.dir / "wt"
        root.mkdir()
        root.chmod(0o777)
        cfg = self.config(roles={"builder": ["w"]}, permit=["builder"], allow_write=True, worktree_root=str(root))
        code, out = self.dispatch("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir",
                                  str(self.repo()), "--role", "builder", "--intent", "x")
        self.assertEqual(code, 2, out)
        self.assertIn("worktree_root must be owned", out["dispatch"]["refused"])
        self.assertEqual(self.calls("worker"), [])
        self.assertEqual(list(root.iterdir()), [])

    def test_refusals_carry_the_job_id_when_known(self):
        self.fake("worker")
        self.go(self.config(), "--job-id", "job-R")
        code, out = self.go(self.config(), "--job-id", "job-R")
        self.assertEqual((code, out["dispatch"]["job_id"]), (2, None), out)  # another job's id: never named
        code, out = self.go(self.config(), "--job-id", "bad id!")
        self.assertEqual((code, out["dispatch"]["job_id"]), (2, None), out)
        code, out = self.go(self.config())
        self.assertEqual(code, 0)

    def test_popen_oserror_on_a_write_role_removes_the_worktree_and_branch(self):
        bad = self.bin / "worker"
        bad.write_text("#!/nonexistent/interpreter\n")
        bad.chmod(0o755)
        repo = self.repo()
        cfg = self.config(roles={"builder": ["w"]}, permit=["builder"], allow_write=True,
                          worktree_root=str(self.dir / "wt"))
        code, out = self.dispatch("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir", str(repo),
                                  "--role", "builder", "--intent", "x", "--job-id", "job-O")
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"), out)
        self.assertIn("launch failed", out["dispatch"]["note"])
        self.assertFalse((self.dir / "wt" / "job-O").exists(), "orphan worktree directory")
        branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "dispatch/*"],
                                  capture_output=True, text=True)
        self.assertEqual(branches.stdout.strip(), "")
        self.assertEqual((self.runs / "job-O" / "result.json").exists(), True)


SIBLING = r'''
import contextlib, ctypes, io, json, subprocess, sys
root, cfg, runs, workdir, preset = sys.argv[1:6]
sys.path.insert(0, root)
from modules.orchestration import dispatch as d
libc = ctypes.CDLL(None)
libc.prctl(36, int(preset), 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
sibling = subprocess.Popen(["sleep", "60"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = d.main(["run", "--config", cfg, "--run-dir", runs, "--workdir", workdir, "--role", "researcher",
                   "--intent", "x", "--job-id", "job-S"])
flag = ctypes.c_int(-1)
libc.prctl(37, ctypes.byref(flag), 0, 0, 0)  # PR_GET_CHILD_SUBREAPER
alive = sibling.poll() is None
sibling.kill()
print(json.dumps({"code": code, "method": json.loads(buf.getvalue())["dispatch"]["containment"]["method"],
                  "alive": alive, "flag": flag.value, "wait": sibling.wait()}))
'''


class CarriedFixTests(ContainmentBase):
    """U04a carried items 5-8: no group kill after reap, in-process subreaper, refusal job id, PATH tests."""

    inproc = _inproc

    def killpg_log(self, *args):
        """Run in-process with os.killpg wrapped: every call must hit a worker that is not yet reaped."""
        real, calls, bad = os.killpg, [], []

        def checked(pgid, number):
            calls.append(pgid)
            try:
                os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                bad.append(pgid)
            return real(pgid, number)

        with mock.patch.object(os, "killpg", checked):
            code, out = self.inproc(*args)
        return code, out, calls, bad

    def test_group_kill_happens_before_the_worker_is_reaped(self):
        self.fake("worker", "print('done')")
        code, out, calls, bad = self.killpg_log(
            *self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        self.assertTrue(calls, "the group was never killed after a normal exit")
        self.assertEqual(bad, [], "killpg after the worker was reaped (its group id may be reused)")

    def test_an_ignored_sigchld_does_not_turn_a_clean_exit_into_a_failure(self):
        self.fake("worker", "print('done')")
        previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # inherited by a dispatcher started that way
        self.addCleanup(signal.signal, signal.SIGCHLD, previous)
        with mock.patch.object(os, "killpg") as killed:
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        killed.assert_not_called()  # the kernel reaped the worker: its group id may already belong to a stranger

    def test_group_kill_before_reap_on_timeout(self):
        self.fake("worker", "import time\ntime.sleep(60)")
        code, out, calls, bad = self.killpg_log(
            *self.run_args(self.config(timeout_seconds=1), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["dispatch"]["timed_out"]), (1, True), out)
        self.assertTrue(calls)
        self.assertEqual(bad, [], "killpg after the worker was reaped")

    def test_without_waitid_the_group_kill_after_reap_is_skipped(self):
        self.fake("worker", "print('done')")
        real, calls = os.killpg, []
        with mock.patch.object(dispatch_module, "HOLD_ZOMBIE", False), \
                mock.patch.object(os, "killpg", lambda pgid, number: calls.append(pgid) or real(pgid, number)):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        self.assertEqual(calls, [], "group kill after reap is skipped where waitid(WNOWAIT) is missing")

    def sibling(self, preset):
        self.fake("worker")
        driver = self.dir / "sibling.py"
        driver.write_text(SIBLING)
        proc = subprocess.run(
            [sys.executable, "-B", str(driver), str(ROOT), self.config(), str(self.runs), str(self.dir), str(preset)],
            capture_output=True, text=True, timeout=60, cwd=self.dir)
        try:
            return json.loads(proc.stdout)
        except ValueError:
            self.fail(f"no result: {proc.stdout!r} {proc.stderr!r}")

    def test_inprocess_run_spares_the_callers_children_and_restores_the_flag(self):
        got = self.sibling(1)
        self.assertEqual((got["code"], got["method"]), (0, "marker"), got)
        self.assertTrue(got["alive"], "the caller's sibling child was killed")
        self.assertEqual(got["wait"], -signal.SIGKILL, got)  # its own wait() saw its own kill, not a stolen status
        self.assertEqual(got["flag"], 1, "the caller's subreaper flag was not restored")

    def test_inprocess_run_with_the_flag_off_leaves_it_off(self):
        got = self.sibling(0)
        self.assertEqual((got["code"], got["method"], got["flag"]), (0, "marker", 0), got)
        self.assertTrue(got["alive"])

    def test_get_subreaper_reads_the_flag(self):
        self.assertIn(dispatch_module.get_subreaper(), (0, 1))
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsNone(dispatch_module.get_subreaper())

    def test_refusal_job_id_cases(self):
        self.fake("worker")
        cfg = self.config()
        cases = (
            ("unpermitted role", ["--role", "verifier", "--intent", "x"], "job-P", "job-P"),
            ("empty prompt", ["--role", "researcher", "--intent", " "], "job-E", "job-E"),
            ("no match", ["--intent", "zzz"], "job-N", "job-N"),
            ("invalid id", ["--role", "verifier", "--intent", "x"], "bad id!", None),
        )
        for label, extra, job, expect in cases:
            code, out = self.dispatch(*self.run_args(cfg, *extra, "--job-id", job))
            self.assertEqual((code, out["dispatch"]["job_id"]), (2, expect), (label, out))
        self.go(cfg, "--job-id", "job-R")
        code, out = self.go(cfg, "--job-id", "job-R")
        self.assertEqual((code, out["dispatch"]["job_id"]), (2, None), out)
        code, out = self.dispatch("run", "--run-dir", str(self.runs), "--job-id", "job-C", "--role", "researcher")
        self.assertEqual((code, out["dispatch"]["job_id"]), (2, "job-C"), out)  # no --config at all

    def test_base_env_keeps_absolute_path_entries_only(self):
        os.environ["PATH"] = "relative:/usr/bin::bin"
        self.assertEqual(dispatch_module.base_env()["PATH"], "/usr/bin")

    def test_git_and_version_probe_receive_absolute_path_entries_only(self):
        self.fake("git", "print('x')")
        self.fake("probe", "print('x')")
        os.environ["PATH"] = f"relative:{self.bin}::bin:.:/usr/bin"
        dispatch_module.git(self.dir, "status")
        self.assertEqual(dispatch_module.harness_version(str(self.bin / "probe")), "fake 1.0")
        expect = f"{self.bin}:/usr/bin"
        self.assertEqual(self.calls("git")[0]["environ"]["PATH"], expect)
        self.assertEqual(self.all_calls("probe")[0]["environ"]["PATH"], expect)

    def test_git_never_inherits_an_open_caller_stdin(self):
        self.fake("git", "print('x')")  # the fake reads stdin to EOF, like any stdin-reading git hook would
        read_end, write_end = os.pipe()  # write end stays open: an inherited stdin never reaches EOF
        saved = os.dup(0)
        try:
            os.dup2(read_end, 0)
            with mock.patch.object(dispatch_module, "GIT_TIMEOUT_SECONDS", 5):
                proc = dispatch_module.git(self.dir, "status")
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.calls("git")[0]["stdin"], "")


if __name__ == "__main__":
    unittest.main()
