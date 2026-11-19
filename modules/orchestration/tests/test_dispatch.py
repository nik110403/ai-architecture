import ast
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORCH = HERE.parent
ROOT = ORCH.parents[1]
DISPATCH = ORCH / "dispatch.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.orchestration import dispatch as dispatch_module  # noqa: E402

FAKE = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
log = {{"argv": sys.argv[1:], "env": sorted(os.environ), "environ": dict(os.environ), "stdin": sys.stdin.read(),
       "cwd": os.getcwd(), "pid": os.getpid()}}
open(os.path.join(here, "{name}.log"), "a").write(json.dumps(log) + "\\n")
if sys.argv[1:] == ["--version"]:
    vf = os.path.join(here, "{name}.version")
    print(open(vf).read() if os.path.exists(vf) else "fake 1.0")
    sys.exit(0)
{body}
"""
CLAUDE_REAL = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
               "terminal_reason": "completed", "result": "fine"}


def claude_body(**over):
    return "print(json.dumps(%r))" % {**CLAUDE_REAL, **over}


CLAUDE_OK = claude_body()
STEP = {"type": "step_start", "sessionID": "s", "part": {"type": "step-start"}}
TEXT = {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "hello"}}
TOKENS = {"total": 1001, "input": 1000, "output": 1, "reasoning": 0, "cache": {"write": 0, "read": 0}}


def finish(reason="stop", **part):
    return {"type": "step_finish", "sessionID": "s",
            "part": {"type": "step-finish", "reason": reason, "tokens": dict(TOKENS), "cost": 0.003015, **part}}


def denial(error):
    return {"type": "tool_use", "sessionID": "s",
            "part": {"type": "tool", "tool": "read", "state": {"status": "error", "input": {"p": "SECRET"}, "error": error}}}


RULE = "The user has specified a rule which prevents you from using this specific tool call. SECRET"
UNAVAILABLE = "Model tried to call unavailable tool 'invalid'. Available tools: glob, grep, read."


def events_body(*events, raw=None):
    lines = [json.dumps(e) for e in events] + ([raw] if raw is not None else [])
    return "sys.stdout.write(%r)" % ("\n".join(lines) + "\n")


OPENCODE_OK = events_body(STEP, TEXT, finish())
INSTALL = ("modules/orchestration", "modules/contracts", "adapters/runtime", "agents")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    return not (stat_path.exists() and stat_path.read_text().rsplit(")", 1)[1].split()[0] == "Z")


def wait_for(check, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return None


class Base(unittest.TestCase):
    script = DISPATCH

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.runs = self.dir / "runs"
        old = os.environ.get("PATH")
        # Only the fakes plus system tools: a real claude/opencode on the host must never be reachable.
        os.environ["PATH"] = f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        self.stderr = ""

    def fake(self, name, body="print('ok')"):
        path = self.bin / name
        path.write_text(FAKE.format(python=sys.executable, name=name, body=body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def all_calls(self, name):
        log = self.bin / f"{name}.log"
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []

    def calls(self, name):
        return [c for c in self.all_calls(name) if c["argv"] != ["--version"]]

    def config(self, **over):
        cfg = {
            "seats": {"w": {"harness": "local-command", "command": [str(self.bin / "worker")]}},
            "roles": {"researcher": ["w"]},
            "permit": ["researcher"],
        }
        cfg.update(over)
        self.cfg_count = getattr(self, "cfg_count", 0) + 1
        path = self.dir / f"cfg{self.cfg_count}.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(path)

    def dispatch(self, *args, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, "-B", str(self.script), *args], capture_output=True, text=True, env=env, cwd=self.dir,
            timeout=120,
        )
        self.stderr = proc.stderr
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            self.fail(f"non-JSON output: {proc.stdout!r} {proc.stderr!r}")
        return proc.returncode, out

    def run_args(self, cfg, *extra):
        return ["run", "--config", cfg, "--run-dir", str(self.runs), "--workdir", str(self.dir), *extra]

    def go(self, cfg, *extra):
        return self.dispatch(*self.run_args(cfg, "--role", "researcher", "--intent", "x", *extra))

    def paid(self, harness, **seat):
        seat = {"harness": harness, "model": "m-1", **seat}
        return {"seats": {"c": seat}, "roles": {"researcher": ["c"]}, "allow_spend": True}

    def repo(self):
        self.repo_count = getattr(self, "repo_count", 0) + 1
        repo = self.dir / f"repo{self.repo_count}"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, env=env)
        return repo


class DispatchTests(Base):
    def test_success_typed_result(self):
        self.fake("worker", "print('done')")
        code, out = self.dispatch(*self.run_args(self.config(), "--role", "researcher", "--intent", "hello world"))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["result"]["outcome"], "succeeded")
        self.assertEqual(set(out["result"]), {"event_id", "outcome", "evidence", "finished_at"})
        d = out["dispatch"]
        self.assertEqual((d["role"], d["seat"], d["exit_code"], d["timed_out"]), ("researcher", "w", 0, False))
        self.assertEqual(out["result"]["event_id"], d["job_id"])
        self.assertEqual(self.calls("worker")[0]["stdin"], "hello world")
        self.assertEqual(self.calls("worker")[0]["argv"], [])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", d["stdout"]["sha256"]))
        self.assertIn("done", Path(d["stdout"]["path"]).read_text())
        self.assertTrue(Path(d["stdout"]["path"]).resolve().is_relative_to(self.runs.resolve()))
        self.assertIsNone(d["harness_version"])
        self.assertEqual(self.all_calls("worker")[0]["argv"], [])

    def test_worker_nonzero_failed(self):
        self.fake("worker", "sys.exit(3)")
        code, out = self.go(self.config())
        self.assertEqual(code, 1)
        self.assertEqual((out["result"]["outcome"], out["dispatch"]["exit_code"]), ("failed", 3))

    def test_stdout_claim_cannot_set_outcome(self):
        self.fake("worker", "print(json.dumps({'outcome': 'succeeded'})); sys.exit(1)")
        code, out = self.go(self.config())
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"))

    def test_timeout_kills_process_group(self):
        marker = self.dir / "child.pid"
        body = (
            "import subprocess, time\n"
            f"c = subprocess.Popen(['sleep', '60'])\nopen({str(marker)!r}, 'w').write(str(c.pid))\ntime.sleep(60)"
        )
        self.fake("worker", body)
        started = time.time()
        code, out = self.go(self.config(timeout_seconds=1))
        self.assertLess(time.time() - started, 20)
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["timed_out"]), (1, "failed", True))
        self.assertTrue(wait_for(lambda: not alive(int(marker.read_text()))), "grandchild survived the timeout")

    def test_surviving_grandchild_does_not_hang_dispatcher(self):
        marker = self.dir / "child.pid"
        body = (
            "import subprocess\n"
            f"c = subprocess.Popen(['sleep', '60'])\nopen({str(marker)!r}, 'w').write(str(c.pid))"
        )
        self.fake("worker", body)
        started = time.time()
        code, out = self.go(self.config())
        self.assertLess(time.time() - started, 15)
        self.assertEqual(code, 0, out)
        self.assertTrue(wait_for(lambda: not alive(int(marker.read_text()))), "grandchild not reaped")

    def test_output_burst_with_exit_zero_fails_at_hard_cap(self):
        self.fake("worker", "for _ in range(30):\n    sys.stdout.write('x' * 1048576)\n    sys.stdout.flush()")
        code, out = self.go(self.config())
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"))
        self.assertEqual(out["dispatch"]["note"], "output size cap exceeded")
        size = os.path.getsize(out["dispatch"]["stdout"]["path"])
        self.assertLessEqual(size, dispatch_module.MAX_OUTPUT_BYTES)

    def test_missing_executable_is_dispatch_failure(self):
        cfg = self.config(seats={"w": {"harness": "local-command", "command": ["no-such-binary-xyz"]}})
        code, out = self.go(cfg)
        self.assertEqual((code, out["result"]["outcome"]), (2, "unsupported"))
        self.assertIn("executable not found", out["dispatch"]["skipped"][0]["reason"])
        self.assertEqual(out["dispatch"]["role"], "researcher")

    def test_relative_path_entries_are_ignored(self):
        self.fake("worker")
        cwd_bin = self.dir / "rel"
        cwd_bin.mkdir()
        shutil.move(str(self.bin / "worker"), str(cwd_bin / "tool"))
        cfg = self.config(seats={"w": {"harness": "local-command", "command": ["tool"]}})
        code, out = self.dispatch(*self.run_args(cfg, "--role", "researcher", "--intent", "x"),
                                  env_extra={"PATH": f"rel{os.pathsep}{os.environ['PATH']}"})
        self.assertEqual(code, 2, out)
        self.assertIn("executable not found", out["dispatch"]["skipped"][0]["reason"])

    def test_missing_model_on_paid_seat(self):
        self.fake("claude")
        cfg = self.config(seats={"c": {"harness": "claude-code"}}, roles={"researcher": ["c"]}, allow_spend=True)
        code, out = self.go(cfg, "--allow-spend")
        self.assertEqual(code, 2)
        self.assertEqual(out["dispatch"]["skipped"], [{"seat": "c", "reason": "missing model"}])
        self.assertEqual(self.all_calls("claude"), [])

    def two_seats(self, **over):
        cfg = {
            "seats": {
                "a": {"harness": "local-command", "model": "m-a", "command": ["missing-a-binary"]},
                "b": {"harness": "local-command", "model": "m-b", "command": [str(self.bin / "worker")]},
            },
            "roles": {"researcher": ["a", "b"]},
        }
        cfg.update(over)
        return self.config(**cfg)

    def test_pin_unavailable_is_refused_even_with_fallback(self):
        self.fake("worker")
        code, out = self.go(self.two_seats(), "--model", "m-a")
        self.assertEqual((code, out["result"]["outcome"]), (2, "unsupported"))
        self.assertEqual(self.calls("worker"), [])
        self.assertIn("no fallback with a pin", out["dispatch"]["refused"])
        self.assertEqual(
            out["dispatch"]["skipped"],
            [{"seat": "b", "reason": "model pin mismatch"},
             {"seat": "a", "reason": "executable not found: missing-a-binary"}],
        )

    def test_pin_matching_no_seat_lists_all_skips(self):
        self.fake("worker")
        code, out = self.go(self.two_seats(), "--model", "m-zzz")
        self.assertEqual(code, 2)
        self.assertIn("not available on any configured seat", out["dispatch"]["refused"])
        self.assertEqual([s["seat"] for s in out["dispatch"]["skipped"]], ["a", "b"])
        self.assertEqual(out["dispatch"]["role"], "researcher")

    def test_pin_honoured_exactly(self):
        self.fake("worker")
        code, out = self.go(self.two_seats(), "--model", "m-b")
        self.assertEqual((code, out["dispatch"]["seat"], out["dispatch"]["model"]), (0, "b", "m-b"))
        self.assertEqual(out["dispatch"]["skipped"], [{"seat": "a", "reason": "model pin mismatch"}])

    def test_fallback_order_with_visible_skips(self):
        self.fake("worker")
        code, out = self.go(self.two_seats())
        self.assertEqual((code, out["dispatch"]["seat"]), (0, "b"))
        self.assertEqual(out["dispatch"]["skipped"][0]["seat"], "a")
        self.assertIn("not found", out["dispatch"]["skipped"][0]["reason"])

    def test_unavailable_paid_seat_walks_on(self):
        self.fake("worker")
        cfg = self.config(
            seats={"c": {"harness": "claude-code"}, "w": {"harness": "local-command", "command": [str(self.bin / "worker")]}},
            roles={"researcher": ["c", "w"]}, allow_spend=True,
        )
        code, out = self.go(cfg, "--allow-spend")
        self.assertEqual((code, out["dispatch"]["seat"]), (0, "w"))
        self.assertEqual(out["dispatch"]["skipped"], [{"seat": "c", "reason": "missing model"}])

    def test_no_retry_after_launch_failure(self):
        self.fake("worker", "sys.exit(9)")
        w = {"harness": "local-command", "command": [str(self.bin / "worker")]}
        code, out = self.go(self.config(seats={"a": w, "b": w}, roles={"researcher": ["a", "b"]}))
        self.assertEqual((code, out["dispatch"]["seat"]), (1, "a"))
        self.assertEqual(len(self.calls("worker")), 1)

    def test_intent_ambiguous_and_zero(self):
        self.fake("worker")
        for text in ("research and verify things", "nothing relevant here"):
            code, out = self.dispatch(*self.run_args(self.config(permit=["researcher", "verifier"]), "--intent", text))
            self.assertEqual((code, out["result"]["outcome"]), (2, "unsupported"), text)
        self.assertEqual(self.calls("worker"), [])
        _, out = self.dispatch(*self.run_args(self.config(), "--intent", "research and verify"))
        self.assertIn("verifier", out["dispatch"]["refused"])
        self.assertIn("researcher", out["dispatch"]["refused"])

    def test_intent_resolves_single_role(self):
        self.fake("worker")
        code, out = self.dispatch(*self.run_args(self.config(), "--intent", "Please research the tree"))
        self.assertEqual((code, out["dispatch"]["role"]), (0, "researcher"))

    def test_role_not_permitted_and_no_config(self):
        self.fake("worker")
        code, out = self.go(self.config(permit=[]))
        self.assertEqual((code, out["result"]["outcome"]), (2, "unsupported"))
        self.assertEqual(out["dispatch"]["role"], "researcher")
        code, out = self.dispatch("run", "--role", "researcher", "--intent", "x", "--run-dir", str(self.runs))
        self.assertEqual(code, 2)
        self.assertEqual(self.calls("worker"), [])

    def test_missing_role_file_is_unavailable(self):
        self.fake("worker")
        tree = InstallTests.install(self.dir / "tree")
        (tree / "agents/researcher.md").unlink()
        code, out = self.run_script(tree, *self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual((code, out["result"]["outcome"]), (2, "unsupported"))
        self.assertIn("unavailable", out["dispatch"]["refused"])
        self.assertEqual(self.calls("worker"), [])

    def run_script(self, tree, *args):
        self.script = tree / "modules/orchestration/dispatch.py"
        try:
            return self.dispatch(*args)
        finally:
            self.script = DISPATCH

    def test_role_file_disagreeing_with_roles_json_is_unavailable(self):
        self.fake("worker")
        tree = InstallTests.install(self.dir / "tree")
        path = tree / "agents/researcher.md"
        path.write_text(path.read_text().replace("tools: Read, Grep, Glob, WebFetch, WebSearch", "tools: Read, Bash"))
        code, out = self.run_script(tree, *self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual(code, 2)
        self.assertIn("disagrees", out["dispatch"]["refused"])

    # --- spend ---

    def test_spend_needs_both_gates(self):
        self.fake("claude", CLAUDE_OK)
        code, out = self.go(self.config(**{**self.paid("claude-code"), "allow_spend": False}), "--allow-spend")
        self.assertEqual(code, 2)
        self.assertEqual(out["dispatch"]["skipped"][0]["reason"], "spend not permitted by config")
        code, out = self.go(self.config(**self.paid("claude-code")))
        self.assertEqual(code, 2)
        self.assertIn("--allow-spend", out["dispatch"]["skipped"][0]["reason"])
        self.assertEqual(self.all_calls("claude"), [])

    def test_spend_miss_does_not_fall_through_to_free_seat(self):
        self.fake("claude", CLAUDE_OK)
        self.fake("worker")
        seats = {"c": {"harness": "claude-code", "model": "m-1"},
                 "w": {"harness": "local-command", "command": [str(self.bin / "worker")]}}
        for allow_config, cli in ((False, ("--allow-spend",)), (True, ())):
            cfg = self.config(seats=seats, roles={"researcher": ["c", "w"]}, allow_spend=allow_config)
            code, out = self.go(cfg, *cli)
            self.assertEqual(code, 2, out)
            self.assertEqual(out["result"]["outcome"], "unsupported")
            self.assertEqual(out["dispatch"]["skipped"][0]["seat"], "c")
        self.assertEqual(self.all_calls("worker"), [])

    def test_local_command_named_claude_counts_as_paid(self):
        self.fake("claude", CLAUDE_OK)
        seats = {"w": {"harness": "local-command", "command": [str(self.bin / "claude"), "-p"]}}
        code, out = self.go(self.config(seats=seats, roles={"researcher": ["w"]}), "--allow-spend")
        self.assertEqual(code, 2)
        self.assertEqual(out["dispatch"]["skipped"][0]["reason"], "spend not permitted by config")
        code, out = self.go(self.config(seats=seats, roles={"researcher": ["w"]}, allow_spend=True))
        self.assertEqual(code, 2)
        self.assertIn("--allow-spend", out["dispatch"]["skipped"][0]["reason"])
        self.assertEqual(self.all_calls("claude"), [])
        code, out = self.go(self.config(seats=seats, roles={"researcher": ["w"]}, allow_spend=True), "--allow-spend")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("claude")[0]["argv"], ["-p"])

    # --- harness argv, env, files ---

    def test_claude_argv_files_and_env(self):
        self.fake("claude", CLAUDE_OK)
        cfg = self.config(**self.paid("claude-code", effort="high", max_budget_usd=2, env_allow=["ANTHROPIC_API_KEY"]))
        code, out = self.go(cfg, "--allow-spend", "--job-id", "job-1")
        self.assertEqual(code, 0, out)
        agent_file = str((self.runs / "job-1").resolve() / "agents.json")
        call = self.calls("claude")[0]
        tools = "Read,Grep,Glob,WebFetch,WebSearch"
        self.assertEqual(call["argv"], [
            "-p", "--agents", agent_file, "--agent", "researcher", "--tools", tools,
            "--permission-mode", "dontAsk", "--allowedTools", tools, "--setting-sources", "user",
            "--strict-mcp-config", "--output-format", "json", "--no-session-persistence",
            "--model", "m-1", "--effort", "high", "--max-budget-usd", "2",
        ])
        self.assertEqual(call["stdin"], "x")
        agents = json.loads(Path(agent_file).read_text())
        self.assertEqual(list(agents), ["researcher"])
        self.assertEqual(set(agents["researcher"]), {"description", "prompt", "tools"})
        self.assertEqual(agents["researcher"]["tools"], ["Read", "Grep", "Glob", "WebFetch", "WebSearch"])
        self.assertTrue(agents["researcher"]["prompt"])
        self.assertEqual(stat.S_IMODE(os.stat(agent_file).st_mode), 0o600)
        self.assertEqual(out["dispatch"]["argv"][3], agent_file)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", call["environ"])
        self.assertEqual(self.all_calls("claude")[0]["argv"], ["--version"])

    def test_claude_plan_uses_placeholder_and_writes_nothing(self):
        self.fake("claude", CLAUDE_OK)
        cfg = self.config(**self.paid("claude-code"))
        code, out = self.dispatch("plan", "--config", cfg, "--role", "researcher", "--allow-spend")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["plan"]["argv"][3], "<job-dir>/agents.json")
        self.assertEqual(self.all_calls("claude"), [])
        self.assertFalse(self.runs.exists())

    def test_opencode_argv_env_and_config(self):
        self.fake("opencode", OPENCODE_OK)
        cfg = self.config(**self.paid("opencode", model="prov/m-2", effort="max"))
        code, out = self.go(cfg, "--allow-spend", "--job-id", "job-2")
        self.assertEqual(code, 0, out)
        call = self.calls("opencode")[0]
        self.assertEqual(call["argv"], ["run", "--agent", "researcher", "--model", "prov/m-2", "--format", "json",
                                        "--variant", "max"])
        self.assertNotIn("--auto", call["argv"])
        self.assertEqual(call["stdin"], "x")
        self.assertEqual(call["environ"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        content = json.loads(call["environ"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(sorted(content), ["agent", "small_model"])
        self.assertEqual(content["small_model"], "prov/m-2")
        self.assertEqual(content["agent"]["title"], {"disable": True})
        self.assertEqual(sorted(content["agent"]), ["researcher", "title"])
        agent = content["agent"]["researcher"]
        self.assertEqual(agent["mode"], "primary")
        self.assertEqual(agent["model"], "prov/m-2")
        self.assertTrue(agent["prompt"] and agent["description"])
        self.assertEqual(agent["permission"], {
            "*": "deny", "external_directory": "deny", "read": "allow", "grep": "allow", "glob": "allow",
            "webfetch": "allow", "websearch": "allow",
        })
        for name in ("start.json", "result.json"):
            text = (self.runs / "job-2" / name).read_text()
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", text)
            self.assertNotIn(agent["prompt"][:40], text)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", json.dumps(out))

    def test_env_not_leaked(self):
        self.fake("worker")
        cfg = self.config(seats={"w": {
            "harness": "local-command", "command": [str(self.bin / "worker")], "env_allow": ["ALLOWED_TOKEN"]}})
        self.dispatch(*self.run_args(cfg, "--role", "researcher", "--intent", "x"),
                      env_extra={"ALLOWED_TOKEN": "a", "LEAKY_SECRET": "b"})
        env = set(self.calls("worker")[0]["env"])
        self.assertIn("ALLOWED_TOKEN", env)
        self.assertNotIn("LEAKY_SECRET", env)
        self.assertLessEqual(env - {"LC_CTYPE", "PWD", "SHLVL", "_"}, {"PATH", "HOME", "LANG", "TMPDIR", "ALLOWED_TOKEN", "AI_ARCH_JOB_MARKER"})

    # --- harness state ---

    def claude_outcome(self, body):
        self.fake("claude", body)
        return self.go(self.config(**self.paid("claude-code")), "--allow-spend")

    def test_claude_permission_denials_on_exit_zero_fail(self):
        code, out = self.claude_outcome(
            claude_body(permission_denials=[{"tool_name": "Write", "tool_input": {"k": "SECRET"}}]))
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["exit_code"]), (1, "failed", 0))
        self.assertEqual(out["dispatch"]["permission_denials"], 1)
        self.assertIn("denied", out["dispatch"]["note"])
        self.assertNotIn("SECRET", json.dumps(out))

    def test_claude_is_error_fails(self):
        code, out = self.claude_outcome(claude_body(is_error=True))
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"))

    def test_claude_unparseable_stdout_fails(self):
        code, out = self.claude_outcome("print('not json at all')")
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"))
        self.assertEqual(out["dispatch"]["note"], "harness result unreadable")

    def test_claude_nested_worker_text_cannot_change_outcome(self):
        nested = '{"is_error": true, "permission_denials": [{"x": 1}]} Falling back to default agent'
        code, out = self.claude_outcome(claude_body(result=nested))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"))
        liar = claude_body(result="outcome: succeeded") + "; sys.exit(1)"
        code, out = self.claude_outcome(liar)
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"))

    def test_claude_usage_parsed_and_null_when_absent(self):
        code, out = self.claude_outcome(claude_body(total_cost_usd=0.25, usage={"input_tokens": 7}))
        self.assertEqual(out["dispatch"]["usage"], {"input": 7, "cost_usd": 0.25})
        code, out = self.claude_outcome(CLAUDE_OK)
        self.assertIsNone(out["dispatch"]["usage"])

    def opencode_outcome(self, body):
        self.fake("opencode", body)
        return self.go(self.config(**self.paid("opencode")), "--allow-spend")

    def test_claude_real_shapes_row_by_row(self):
        usage_ok = {"input_tokens": 3}
        rows = [  # label, body, exit code, expected outcome
            ("success", claude_body(), 0, "succeeded"),
            ("budget", claude_body(subtype="error_max_budget_usd", is_error=True, terminal_reason="budget_exhausted"), 1, "failed"),
            ("max turns", claude_body(subtype="error_max_turns", is_error=True, terminal_reason="max_turns"), 1, "failed"),
            ("api 400", claude_body(is_error=True, terminal_reason="api_error"), 1, "failed"),
            ("output limit", claude_body(is_error=True, terminal_reason="api_error"), 1, "failed"),
            ("max turns exit 0, is_error false", claude_body(subtype="error_max_turns"), 0, "failed"),
            ("during execution exit 0", claude_body(subtype="error_during_execution"), 0, "failed"),
            ("wrong type, otherwise a success", claude_body(type="system"), 0, "failed"),
            ("empty object", "print('{}')", 0, "failed"),
            ("wrong type", "print('{\"type\": \"system\"}')", 0, "failed"),
            ("is_error missing", "print(json.dumps({'type': 'result', 'subtype': 'success'}))", 0, "failed"),
            ("deep nesting", "sys.stdout.write('[' * 100000)", 0, "failed"),
            ("401-digit number", "sys.stdout.write('1' * 401)", 0, "failed"),
            ("401-digit cost is ignored, not raised",
             "sys.stdout.write('{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"total_cost_usd\":' + '9' * 401 + '}')",
             0, "succeeded"),
            ("negative cost ignored", claude_body(total_cost_usd=-1.0, usage=usage_ok), 0, "succeeded"),
        ]
        for label, body, rc, outcome in rows:
            with self.subTest(label):
                code, out = self.claude_outcome(body + (f"; sys.exit({rc})" if rc else ""))
                self.assertEqual((code, out["result"]["outcome"]), (0 if outcome == "succeeded" else 1, outcome), out)
                job = self.runs / out["dispatch"]["job_id"]
                self.assertEqual(json.loads((job / "result.json").read_text()), out)
                self.assertEqual(json.loads((job / "start.json").read_text())["state"], "finished")
                if label == "max turns exit 0, is_error false":
                    self.assertIn("error_max_turns", out["dispatch"]["note"])
                if label.startswith("401-digit cost"):
                    self.assertIsNone(out["dispatch"]["usage"])
                if label == "negative cost ignored":
                    self.assertEqual(out["dispatch"]["usage"], {"input": 3})

    def test_opencode_success_and_usage_with_cache(self):
        ev = finish(tokens={"total": 30, "input": 10, "output": 4, "cache": {"write": 2, "read": 5}}, cost=0.5)
        code, out = self.opencode_outcome(events_body(STEP, TEXT, finish("tool-calls", cost=0.25), ev))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        usage = out["dispatch"]["usage"]
        self.assertEqual(usage, {"cost_usd": 0.75, "input": 1010, "output": 5, "cache_read": 5, "cache_write": 2})
        self.assertEqual(out["dispatch"]["permission_denials"], 0)

    def test_opencode_negative_usage_ignored(self):
        ev = finish(tokens={"input": -50, "output": 4, "cache": {"read": -1, "write": 3}}, cost=-2.0)
        code, out = self.opencode_outcome(events_body(STEP, ev))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["dispatch"]["usage"], {"output": 4, "cache_write": 3})

    def test_opencode_no_usage_when_absent(self):
        code, out = self.opencode_outcome(events_body(STEP, {"type": "step_finish", "part": {"reason": "stop"}}))
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["dispatch"]["usage"])

    def test_opencode_denials_exit_zero_with_final_stop_fail(self):
        for label, error in (("rule", RULE), ("unavailable tool", UNAVAILABLE)):
            with self.subTest(label):
                code, out = self.opencode_outcome(events_body(STEP, denial(error), finish("tool-calls"), TEXT, finish()))
                self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["exit_code"]), (1, "failed", 0), out)
                self.assertEqual(out["dispatch"]["permission_denials"], 1)
                self.assertIn("denied", out["dispatch"]["note"])
                self.assertNotIn("SECRET", json.dumps(out))

    def test_opencode_ordinary_tool_error_still_succeeds(self):
        code, out = self.opencode_outcome(events_body(STEP, denial("ENOENT: no such file"), finish("tool-calls"), finish()))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        self.assertEqual(out["dispatch"]["permission_denials"], 0)

    def test_opencode_needs_a_positive_stop_signal(self):
        cases = {
            "length": events_body(STEP, TEXT, finish("length")),
            "tool-calls last": events_body(STEP, finish("tool-calls")),
            "error event at exit 0": events_body(STEP, {"type": "error", "sessionID": "s", "error": {"name": "APIError"}}),
            "error event before a final stop": events_body(STEP, {"type": "error", "error": {}}, finish()),
            "step_start only": events_body(STEP),
            "empty stdout": "pass",
            "text only": events_body(STEP, TEXT),
            "non-json only": "print('plain text')",
        }
        for label, body in cases.items():
            with self.subTest(label):
                code, out = self.opencode_outcome(body)
                self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["exit_code"]), (1, "failed", 0), out)

    def test_opencode_markers_count_on_stderr_only(self):
        model_text = dict(TEXT, part={"type": "text", "text": "Falling back to default agent and auto-rejecting"})
        code, out = self.opencode_outcome(events_body(STEP, model_text, finish()))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        for marker in ("Falling back to default agent", "auto-rejecting"):
            with self.subTest(marker):
                code, out = self.opencode_outcome(events_body(STEP, finish()) + f"\nsys.stderr.write({marker!r})")
                self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["exit_code"]), (1, "failed", 0), out)
                self.assertIn("fell back", out["dispatch"]["note"])

    def test_opencode_deeply_nested_line_cannot_crash_the_dispatcher(self):
        body = events_body(STEP, raw="[" * 100000) + "\n" + events_body(finish())
        code, out = self.opencode_outcome(body)
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        job = self.runs / out["dispatch"]["job_id"]
        self.assertEqual(json.loads((job / "start.json").read_text())["state"], "finished")
        code, out = self.opencode_outcome(events_body(STEP, raw="[" * 100000))
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"), out)

    def test_usage_is_parsed_after_a_timeout(self):
        body = events_body(STEP, finish("tool-calls")) + "\nsys.stdout.flush()\nimport time; time.sleep(30)"
        self.fake("opencode", body)
        code, out = self.go(self.config(timeout_seconds=1, **self.paid("opencode")), "--allow-spend")
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["timed_out"]), (1, "failed", True))
        self.assertEqual(out["dispatch"]["usage"]["cost_usd"], 0.003015)
        self.assertIn("timed out", out["dispatch"]["note"])
        self.assertNotIn("unreadable", out["dispatch"]["note"])

    def test_forged_stdout_file_cannot_change_outcome_or_hash(self):
        runs = self.runs
        printed = json.dumps({**CLAUDE_REAL, "is_error": True, "permission_denials": [{"t": 1}]})
        forged = json.dumps(CLAUDE_REAL)
        body = (
            f"sys.stdout.write({printed!r} + '\\n')\nsys.stdout.flush()\n"
            f"p = {str(runs.resolve() / 'job-M' / 'stdout.txt')!r}\n"
            f"os.unlink(p)\nopen(p, 'w').write({forged!r})"
        )
        self.fake("claude", body)
        code, out = self.go(self.config(**self.paid("claude-code")), "--allow-spend", "--job-id", "job-M")
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"), out)
        import hashlib
        self.assertEqual(out["dispatch"]["stdout"]["sha256"], hashlib.sha256((printed + "\n").encode()).hexdigest())
        self.assertEqual(out["result"]["evidence"][0]["sha256"], out["dispatch"]["stdout"]["sha256"])
        self.assertEqual((runs / "job-M" / "stdout.txt").read_text(), forged)

    def test_setsid_survivor_is_killed_by_run_and_does_not_lower_a_success(self):
        marker = self.dir / "survivor.pid"
        self.fake("worker", (
            "import subprocess\n"
            f"c = subprocess.Popen(['sleep', '60'], start_new_session=True)\nopen({str(marker)!r}, 'w').write(str(c.pid))\n"
            "print('done')"
        ))
        self.addCleanup(lambda: marker.exists() and subprocess.run(["kill", "-9", marker.read_text()], capture_output=True))
        started = time.time()
        code, out = self.go(self.config())
        self.assertLess(time.time() - started, 15)
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["exit_code"]), (0, "succeeded", 0), out)
        self.assertTrue(wait_for(lambda: not alive(int(marker.read_text()))), "setsid child survived `run`")
        containment = out["dispatch"]["containment"]
        self.assertEqual((containment["method"], containment["survivors"]), ("children+marker", 0))
        self.assertGreaterEqual(containment["killed"], 1)

    def test_oversized_budget_and_infinity_refused(self):
        path = self.dir / "raw.json"
        for text in ("Infinity", "1e999", "9" * 401):
            with self.subTest(text):
                path.write_text('{"seats":{"c":{"harness":"claude-code","model":"m","max_budget_usd":%s}}}' % text)
                self.refused(str(path), "max_budget_usd")

    def test_version_line_is_capped(self):
        path = self.bin / "verbose"
        path.write_text(f"#!{sys.executable}\nprint('v' * 5000)\n")
        path.chmod(0o755)
        self.assertEqual(len(dispatch_module.harness_version(str(path))), 100)

    def test_git_timeout_is_a_refusal(self):
        git = self.bin / "git"
        git.write_text("#!/bin/sh\nexec sleep 5\n")
        git.chmod(0o755)
        old = dispatch_module.GIT_TIMEOUT_SECONDS
        dispatch_module.GIT_TIMEOUT_SECONDS = 0.3
        self.addCleanup(setattr, dispatch_module, "GIT_TIMEOUT_SECONDS", old)
        with self.assertRaises(dispatch_module.Refusal) as caught:
            dispatch_module.git(self.dir, "status")
        self.assertIn("git unavailable", caught.exception.reason)

    def test_relative_path_cannot_shadow_git_env_or_reach_the_worker(self):
        rel = self.dir / "rel"
        rel.mkdir()
        planted = self.dir / "planted.ran"
        for name in ("git", "planted_tool"):
            (rel / name).write_text(f"#!/bin/sh\ntouch {planted}\nexit 1\n")
            (rel / name).chmod(0o755)
        path = f"rel{os.pathsep}{os.pathsep}.{os.pathsep}{os.environ['PATH']}"
        self.fake("worker")
        repo = self.repo()
        cfg = self.config(
            roles={"builder": ["w"]}, permit=["builder"], allow_write=True, worktree_root=str(self.dir / "wt"))
        code, out = self.dispatch("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir", str(repo),
                                  "--role", "builder", "--intent", "x", env_extra={"PATH": path})
        self.assertEqual(code, 0, out)
        self.assertFalse(planted.exists(), "a relative PATH entry shadowed git")
        worker_path = self.calls("worker")[0]["environ"]["PATH"]
        self.assertTrue(all(os.path.isabs(d) for d in worker_path.split(os.pathsep)), worker_path)
        shebang = self.bin / "shebang-worker"
        shebang.write_text("#!/usr/bin/env planted_tool\n")
        shebang.chmod(0o755)
        cfg = self.config(seats={"w": {"harness": "local-command", "command": [str(shebang)]}})
        code, out = self.dispatch(*self.run_args(cfg, "--role", "researcher", "--intent", "x"), env_extra={"PATH": path})
        self.assertEqual(code, 1, out)
        self.assertFalse(planted.exists(), "env resolved a relative PATH entry")

    def test_marker_is_derived_from_the_job_dir_and_recorded_nowhere(self):
        import hashlib
        self.fake("worker")
        code, out = self.go(self.config(), "--job-id", "job-ID")
        self.assertEqual(code, 0, out)
        environ = self.calls("worker")[0]["environ"]
        job_dir = (self.runs / "job-ID").resolve()
        self.assertEqual(environ["AI_ARCH_JOB_MARKER"], hashlib.sha256(str(job_dir).encode()).hexdigest())
        self.assertNotIn("AI_ARCH_JOB_ID", environ)
        for text in (json.dumps(out), (job_dir / "start.json").read_text(), (job_dir / "result.json").read_text()):
            self.assertNotIn("AI_ARCH_JOB", text)
            self.assertNotIn(environ["AI_ARCH_JOB_MARKER"], text)

    # --- config rulings ---

    def test_budget_on_a_seat_no_adapter_can_honour_is_refused_by_seat(self):
        self.fake("worker")
        for label, seat in (("local", self.valid_seat(max_budget_usd=1)),
                            ("opencode", {"w": {"harness": "opencode", "model": "p/m", "max_budget_usd": 1}})):
            with self.subTest(label):
                self.refused(self.config(seats=seat), "seat w: max_budget_usd is not supported")

    def test_null_budget_and_null_worktree_root_are_refused(self):
        self.fake("worker")
        self.refused(self.config(seats={"c": {"harness": "claude-code", "model": "m", "max_budget_usd": None}},
                                 roles={"researcher": ["c"]}), "max_budget_usd")
        self.refused(self.config(worktree_root=None), "worktree_root")

    def test_refusals_after_role_resolution_carry_the_role(self):
        self.fake("worker")
        code, out = self.dispatch(*self.run_args(self.config(), "--role", "researcher", "--intent", "   "))
        self.assertEqual((code, out["dispatch"]["role"]), (2, "researcher"))
        self.assertIn("empty prompt", out["dispatch"]["refused"])
        code, out = self.dispatch(*self.run_args(self.config(), "--role", "researcher", "--prompt-file", str(self.dir / "nope")))
        self.assertEqual((code, out["dispatch"]["role"]), (2, "researcher"))
        self.assertIn("prompt file unreadable", out["dispatch"]["refused"])
        self.assertEqual(self.calls("worker"), [])

    def test_run_dir_mode_and_owner_are_checked(self):
        self.fake("worker")
        self.runs.mkdir(mode=0o700)
        os.chmod(self.runs, 0o777)
        code, out = self.go(self.config())
        self.assertEqual(code, 2, out)
        self.assertIn("run dir", out["dispatch"]["refused"])
        self.assertEqual(out["dispatch"]["role"], "researcher")
        os.chmod(self.runs, 0o700)
        self.assertEqual(self.go(self.config())[0], 0)
        os.chmod(self.runs, 0o755)
        self.assertEqual(self.go(self.config())[0], 0, "group/other-readable is fine; only writable is refused")
        import io, contextlib
        from unittest import mock
        buf = io.StringIO()
        with mock.patch.object(dispatch_module.os, "geteuid", return_value=os.geteuid() + 1), contextlib.redirect_stdout(buf):
            code = dispatch_module.main(self.run_args(self.config(), "--role", "researcher", "--intent", "x"))
        self.assertEqual(code, 2)
        self.assertIn("run dir", json.loads(buf.getvalue())["dispatch"]["refused"])

    def test_symlinked_claude_counts_as_paid(self):
        self.fake("claude", CLAUDE_OK)
        (self.bin / "harmless").symlink_to(self.bin / "claude")
        seats = {"w": {"harness": "local-command", "command": [str(self.bin / "harmless")]}}
        for command in (str(self.bin / "harmless"), "harmless"):
            seats["w"]["command"] = [command]
            code, out = self.go(self.config(seats=seats, roles={"researcher": ["w"]}))
            self.assertEqual(code, 2, out)
            self.assertEqual(out["dispatch"]["skipped"][0]["reason"], "spend not permitted by config")
        self.assertEqual(self.all_calls("claude"), [])

    def test_pin_tries_every_matching_seat_in_order_and_lists_skips(self):
        self.fake("worker")
        seats = {
            "a": {"harness": "local-command", "model": "m", "command": ["missing-a-binary"]},
            "b": {"harness": "local-command", "model": "m", "command": ["missing-b-binary"]},
            "c": {"harness": "local-command", "model": "m", "command": [str(self.bin / "worker")]},
            "d": {"harness": "local-command", "model": "other", "command": [str(self.bin / "worker")]},
        }
        cfg = self.config(seats=seats, roles={"researcher": ["a", "d", "b", "c"]})
        code, out = self.go(cfg, "--model", "m")
        self.assertEqual((code, out["dispatch"]["seat"]), (0, "c"), out)
        self.assertEqual([s["seat"] for s in out["dispatch"]["skipped"]], ["d", "a", "b"])
        seats["c"]["command"] = ["missing-c-binary"]
        code, out = self.go(self.config(seats=seats, roles={"researcher": ["a", "d", "b", "c"]}), "--model", "m")
        self.assertEqual(code, 2)
        self.assertIn("no fallback with a pin", out["dispatch"]["refused"])
        self.assertEqual([s["seat"] for s in out["dispatch"]["skipped"]], ["d", "a", "b", "c"])

    def test_status_reports_role_files_and_agrees_with_plan(self):
        self.fake("worker")
        cfg = self.config()
        _, out = self.dispatch("status", "--config", cfg)
        self.assertEqual(out["roles"]["researcher"], {"permitted": True, "role_available": True, "reason": None})
        tree = InstallTests.install(self.dir / "tree")
        shutil.rmtree(tree / "agents")
        self.script = tree / "modules/orchestration/dispatch.py"
        try:
            code, status = self.dispatch("status", "--config", cfg)
            plan_code, plan = self.dispatch("plan", "--config", cfg, "--role", "researcher")
        finally:
            self.script = DISPATCH
        self.assertEqual(code, 0, status)
        report = status["roles"]["researcher"]
        self.assertFalse(report["role_available"])
        self.assertIn("unavailable", report["reason"])
        self.assertEqual(plan_code, 2)
        self.assertEqual(plan["dispatch"]["refused"], f"{report['reason']}")

    # --- in-process fault injection ---

    def inproc(self, *args):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = dispatch_module.main(list(args))
        return code, json.loads(buf.getvalue())

    def flaky(self, match):
        import errno
        real = dispatch_module.write_private

        def write(path, text):
            if match(Path(path).name, text):
                raise OSError(errno.ENOSPC, "injected")
            return real(path, text)
        return write

    def test_failed_running_rewrite_kills_the_worker_and_fails(self):
        from unittest import mock
        self.fake("worker", "import time; time.sleep(30)")
        flaky = self.flaky(lambda name, text: name == "start.json" and '"state": "running"' in text)
        with mock.patch.object(dispatch_module, "write_private", flaky):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x", "--job-id", "job-F"))
        self.assertEqual((code, out["result"]["outcome"]), (1, "failed"), out)
        self.assertIn("dispatcher failure after launch", out["dispatch"]["note"])
        pid = out["dispatch"]["pid"]
        self.assertTrue(pid)
        self.assertTrue(wait_for(lambda: not alive(pid)), "worker left running")
        self.assertEqual(json.loads((self.runs / "job-F" / "result.json").read_text()), out)

    def test_unwritable_result_json_still_reaches_stdout_with_a_note(self):
        from unittest import mock
        self.fake("worker")
        flaky = self.flaky(lambda name, text: name == "result.json")
        with mock.patch.object(dispatch_module, "write_private", flaky):
            code, out = self.inproc(*self.run_args(self.config(), "--role", "researcher", "--intent", "x", "--job-id", "job-W"))
        self.assertEqual((code, out["result"]["outcome"]), (0, "succeeded"), out)
        self.assertIn("result.json could not be written", out["dispatch"]["note"])
        self.assertFalse((self.runs / "job-W" / "result.json").exists())

    def test_failure_before_launch_removes_worktree_branch_and_job_dir(self):
        from unittest import mock
        self.fake("worker")
        repo = self.repo()
        cfg = self.config(roles={"builder": ["w"]}, permit=["builder"], allow_write=True,
                          worktree_root=str(self.dir / "wt"))
        flaky = self.flaky(lambda name, text: name == "prompt.txt")
        with mock.patch.object(dispatch_module, "write_private", flaky):
            code, out = self.inproc("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir", str(repo),
                                    "--role", "builder", "--intent", "x", "--job-id", "job-P")
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["role"]), (2, "unsupported", "builder"), out)
        self.assertIn("launch preparation failed", out["dispatch"]["refused"])
        self.assertFalse((self.dir / "wt" / "job-P").exists(), "orphan worktree directory")
        self.assertFalse((self.runs / "job-P").exists())
        branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "dispatch/*"], capture_output=True, text=True)
        self.assertEqual(branches.stdout.strip(), "")
        listing = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True)
        self.assertEqual(len(listing.stdout.strip().splitlines()), 1)
        self.assertEqual(self.calls("worker"), [])

    # --- crash record and signals ---

    def test_kill_nine_leaves_the_captured_output_and_a_reconcilable_record(self):
        proc, data, marker = self.start_slow(prefix="print('before-kill', flush=True)\n")
        stdout = self.runs / "job-S" / "stdout.txt"
        self.assertTrue(wait_for(lambda: "before-kill" in stdout.read_text()), "pump copy lagged")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(10)
        self.assertIn("before-kill", stdout.read_text())
        start = json.loads((self.runs / "job-S" / "start.json").read_text())
        self.assertEqual(start["dispatcher_pid"], proc.pid)
        for key in ("dispatcher_start_ticks", "worker_start_ticks"):
            self.assertIsInstance(start[key], int, key)
        self.assertEqual(start["worker_start_ticks"], dispatch_module.proc_start_ticks(start["pid"]))
        import datetime
        parse = lambda t: datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual((parse(start["deadline"]) - parse(start["started_at"])).total_seconds(), 600)
        self.assertNotIn("AI_ARCH_JOB_ID", json.dumps(start))
        os.killpg(data["pgid"], signal.SIGKILL)

    def test_sighup_kills_the_worker_group_and_writes_a_failed_result(self):
        proc, data, marker = self.start_slow()
        os.kill(proc.pid, signal.SIGHUP)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 1)
        result = json.loads((self.runs / "job-S" / "result.json").read_text())
        self.assertEqual(result["result"]["outcome"], "failed")
        self.assertIn(f"interrupted by signal {int(signal.SIGHUP)}", result["dispatch"]["note"])
        self.assertTrue(wait_for(lambda: not alive(data["pid"])))

    def test_signal_during_a_hung_version_probe_leaves_a_clean_refusal(self):
        repo = self.repo()
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                marker = self.dir / f"probe{int(number)}.pid"
                (self.bin / "claude").write_text(
                    f"#!{sys.executable}\nimport os, sys, time\nif sys.argv[1:] == ['--version']:\n"
                    f"    open({str(marker)!r}, 'w').write(str(os.getpid()))\n    time.sleep(60)\n")
                (self.bin / "claude").chmod(0o755)
                cfg = self.config(
                    seats={"c": {"harness": "claude-code", "model": "m-1"}}, roles={"builder": ["c"]},
                    permit=["builder"], allow_write=True, allow_spend=True, worktree_root=str(self.dir / "wt"))
                job = f"job-V{int(number)}"
                proc = subprocess.Popen(
                    [sys.executable, "-B", str(DISPATCH), "run", "--config", cfg, "--run-dir", str(self.runs),
                     "--workdir", str(repo), "--role", "builder", "--intent", "x", "--allow-spend", "--job-id", job],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
                self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
                self.assertTrue(wait_for(marker.exists), "probe never started")
                started = time.time()
                os.kill(proc.pid, number)
                out, err = proc.communicate(timeout=20)
                self.assertLess(time.time() - started, 8)
                self.assertEqual(proc.returncode, 2, (out, err))
                envelope = json.loads(out)
                self.assertEqual((envelope["result"]["outcome"], envelope["dispatch"]["role"]), ("unsupported", "builder"))
                self.assertIn("interrupted by signal", envelope["dispatch"]["refused"])
                self.assertFalse((self.runs / job).exists())
                self.assertFalse((self.dir / "wt" / job).exists())
                branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "dispatch/*"],
                                          capture_output=True, text=True)
                self.assertEqual(branches.stdout.strip(), "")
                self.assertTrue(wait_for(lambda: not alive(int(marker.read_text()))), "probe process left running")

    def test_example_config_plans_runs_and_refuses_as_documented(self):
        tool = shutil.which("python3")
        self.assertTrue(tool)
        cfg = self.dir / "example.json"
        cfg.write_text((ORCH / "example.json").read_text())
        code, out = self.dispatch("plan", "--config", str(cfg), "--role", "researcher")
        self.assertEqual((code, out["plan"]["seat"], out["plan"]["write"]), (0, "local-echo", False), out)
        code, out = self.dispatch(*self.run_args(str(cfg), "--role", "researcher", "--intent", "list the open questions"))
        self.assertEqual((code, out["result"]["outcome"], out["dispatch"]["seat"]), (0, "succeeded", "local-echo"), out)
        code, out = self.dispatch("plan", "--config", str(cfg), "--role", "builder")
        self.assertEqual(code, 2, out)
        self.assertIn("allow_write is false", out["dispatch"]["refused"])
        self.assertEqual(out["dispatch"]["role"], "builder")
        code, out = self.dispatch("status", "--config", str(cfg))
        self.assertEqual(code, 0)
        self.assertTrue(all(r["role_available"] for r in out["roles"].values()), out["roles"])

    # --- envelope ---

    def test_job_id_envelope_and_permissions(self):
        self.fake("worker")
        code, out = self.go(self.config(), "--job-id", "job-A")
        self.assertEqual((code, out["dispatch"]["job_id"], out["result"]["event_id"]), (0, "job-A", "job-A"))
        job = self.runs / "job-A"
        self.assertEqual(stat.S_IMODE(os.stat(job).st_mode), 0o700)
        names = sorted(p.name for p in job.iterdir())
        self.assertEqual(names, ["prompt.txt", "result.json", "start.json", "stderr.txt", "stdout.txt"])
        for path in job.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path.name)
        start = json.loads((job / "start.json").read_text())
        for key in ("job_id", "role", "seat", "model", "harness", "harness_version", "argv", "workdir", "worktree",
                    "started_at", "pid", "state"):
            self.assertIn(key, start)
        self.assertEqual(start["pid"], self.calls("worker")[0]["pid"])
        self.assertEqual(json.loads((job / "result.json").read_text()), out)
        for key in ("job_id", "pid", "started_at", "harness_version", "argv", "usage"):
            self.assertIn(key, out["dispatch"])
        code, out = self.go(self.config(), "--job-id", "job-A")
        self.assertEqual(code, 2)
        self.assertIn("already exists", out["dispatch"]["refused"])
        self.assertEqual(len(self.calls("worker")), 1)

    def test_generated_job_id_and_harness_version(self):
        self.fake("claude", CLAUDE_OK)
        (self.bin / "claude.version").write_text("9.9.9 (Fake)\nsecond line\n")
        code, out = self.go(self.config(**self.paid("claude-code")), "--allow-spend")
        self.assertEqual(code, 0, out)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", out["dispatch"]["job_id"]))
        self.assertEqual(out["dispatch"]["harness_version"], "9.9.9 (Fake)")
        self.assertEqual(self.all_calls("claude")[0]["argv"], ["--version"])
        self.assertEqual(set(self.all_calls("claude")[0]["environ"]) - {"LC_CTYPE", "PWD", "SHLVL", "_"}, {"PATH"})

    def start_slow(self, body_seconds=30, job="job-S", prefix=""):
        marker = self.dir / "gc.pid"
        self.fake("worker", prefix + (
            "import subprocess, time\n"
            f"c = subprocess.Popen(['sleep', '60'])\nopen({str(marker)!r}, 'w').write(str(c.pid))\ntime.sleep({body_seconds})"
        ))
        cfg = self.config()
        proc = subprocess.Popen(
            [sys.executable, "-B", str(DISPATCH), *self.run_args(cfg, "--role", "researcher", "--intent", "x", "--job-id", job)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.addCleanup(lambda: [proc.stdout.close(), proc.stderr.close()])
        start = self.runs / job / "start.json"

        def running():
            try:
                data = json.loads(start.read_text())
            except (OSError, ValueError):
                return None
            return data if data["pid"] and marker.exists() else None

        data = wait_for(running)
        self.assertIsNotNone(data, "start.json never showed a running pid")
        return proc, data, marker

    def test_start_json_has_pid_while_running_and_survives_dispatcher_sigkill(self):
        proc, data, marker = self.start_slow()
        self.assertEqual((data["state"], data["pgid"]), ("running", data["pid"]))
        self.assertTrue(alive(data["pid"]))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(10)
        after = json.loads((self.runs / "job-S" / "start.json").read_text())
        self.assertEqual((after["pid"], after["state"]), (data["pid"], "running"))
        self.assertFalse((self.runs / "job-S" / "result.json").exists())
        os.killpg(data["pgid"], signal.SIGKILL)

    def test_sigterm_kills_worker_group_and_writes_failed_result(self):
        proc, data, marker = self.start_slow()
        os.kill(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 1)
        result = json.loads((self.runs / "job-S" / "result.json").read_text())
        self.assertEqual(result["result"]["outcome"], "failed")
        self.assertIn("interrupted", result["dispatch"]["note"])
        self.assertEqual(json.loads(out)["result"]["outcome"], "failed")
        self.assertTrue(wait_for(lambda: not alive(data["pid"])))
        self.assertTrue(wait_for(lambda: not alive(int(marker.read_text()))), "grandchild survived SIGTERM")

    # --- config, plan, status ---

    def test_plan_and_status_launch_nothing(self):
        self.fake("worker")
        self.fake("claude", CLAUDE_OK)
        self.fake("opencode")
        seats = {
            "w": {"harness": "local-command", "command": [str(self.bin / "worker")]},
            "c": {"harness": "claude-code", "model": "m-1"},
            "o": {"harness": "opencode", "model": "p/m"},
        }
        cfg = self.config(seats=seats, roles={"researcher": ["w", "c", "o"]}, allow_spend=True)
        code, out = self.dispatch("plan", "--config", cfg, "--intent", "research it")
        self.assertEqual((code, out["plan"]["role"], out["plan"]["seat"]), (0, "researcher", "w"))
        code, out = self.dispatch("plan", "--config", cfg, "--role", "researcher", "--model", "m-1", "--allow-spend")
        self.assertEqual((code, out["plan"]["seat"]), (0, "c"))
        code, out = self.dispatch("status", "--config", cfg)
        self.assertEqual(code, 0)
        self.assertTrue(out["seats"]["w"]["available"])
        for name in ("worker", "claude", "opencode"):
            self.assertEqual(self.all_calls(name), [], name)
        self.assertFalse(self.runs.exists())

    def test_status_reports_executable_and_spend_separately(self):
        self.fake("claude", CLAUDE_OK)
        cfg = self.config(seats={"c": {"harness": "claude-code", "model": "m"},
                                 "o": {"harness": "opencode", "model": "m"}}, roles={"researcher": ["c", "o"]})
        _, out = self.dispatch("status", "--config", cfg)
        c, o = out["seats"]["c"], out["seats"]["o"]
        self.assertEqual((c["paid"], c["executable_found"], c["spend_permitted"], c["available"]), (True, True, False, True))
        self.assertEqual((o["executable_found"], o["available"]), (False, False))
        self.assertIn("not found", o["reason"])

    def test_refusals_are_typed_for_plan_run_and_status(self):
        for argv in (["plan"], ["run", "--run-dir", str(self.runs)], ["status"]):
            code, out = self.dispatch(*argv, "--config", self.dir / "none.json")
            self.assertEqual((code, out["result"]["outcome"]), (2, "unsupported"), argv)
            self.assertIn("config unreadable", out["dispatch"]["refused"])
            self.assertEqual(set(out["result"]), {"event_id", "outcome", "evidence", "finished_at"})
        code, out = self.dispatch("plan", "--config", str(self.dir / "none.json"))
        self.assertIsNone(out["plan"])

    def refused(self, cfg, reason, *extra):
        code, out = self.dispatch(*self.run_args(cfg, *(extra or ("--role", "researcher", "--intent", "x"))))
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", self.stderr)
        self.assertIn(reason, out["dispatch"]["refused"])
        self.assertEqual(self.all_calls("worker"), [])

    def valid_seat(self, **over):
        return {"w": {"harness": "local-command", "command": [str(self.bin / "worker")], **over}}

    def test_baseline_config_is_valid(self):
        self.fake("worker")
        code, out = self.go(self.config())
        self.assertEqual(code, 0, out)

    def test_injection_values_each_refused_with_specific_reason(self):
        self.fake("worker")
        paid = {"harness": "claude-code", "model": "m"}
        cases = [
            ("model", self.config(seats={"c": {**paid, "model": "-bad"}}, roles={"researcher": ["c"]}), "invalid model"),
            ("model chars", self.config(seats={"c": {**paid, "model": "a b"}}, roles={"researcher": ["c"]}), "invalid model"),
            ("effort", self.config(seats={"c": {**paid, "effort": "-x"}}, roles={"researcher": ["c"]}), "invalid effort"),
            ("seat name", self.config(seats={"-s": self.valid_seat()["w"]}, roles={}), "invalid seat name"),
            ("env name", self.config(seats=self.valid_seat(env_allow=["A=B"])), "invalid env_allow name"),
            ("harness", self.config(seats={"w": {"harness": "nope"}}), "unknown harness"),
        ]
        for label, cfg, reason in cases:
            with self.subTest(label):
                self.refused(cfg, reason)
        with self.subTest("role"):
            self.refused(self.config(), "invalid role", "--role=-evil", "--intent", "x")
        with self.subTest("pin"):
            self.refused(self.config(), "invalid model", "--role", "researcher", "--intent", "x", "--model=-m")
        with self.subTest("job id"):
            self.refused(self.config(), "invalid job id", "--role", "researcher", "--intent", "x", "--job-id=-j")

    def test_malformed_config_shapes_are_refusals_not_tracebacks(self):
        self.fake("worker")
        seat = self.valid_seat()["w"]
        cases = {
            "unknown top key": (dict(bogus=1), "unknown config keys"),
            "unknown seat key": (dict(seats={"w": {**seat, "extra": 1}}), "unknown keys"),
            "permit number": (dict(permit=5), "permit must list known roles"),
            "permit nested": (dict(permit=[["researcher"]]), "permit must list known roles"),
            "roles list": (dict(roles=[]), "roles must be an object"),
            "role order number": (dict(roles={"researcher": 5}), "seat list must name defined seats"),
            "role order nested": (dict(roles={"researcher": [["w"]]}), "seat list must name defined seats"),
            "seats list": (dict(seats=[]), "seats must be an object"),
            "seat null": (dict(seats={"w": None}), "must be an object"),
            "harness list": (dict(seats={"w": {"harness": ["x"]}}), "unknown harness"),
            "model number": (dict(seats={"w": {**seat, "model": 5}}), "invalid model"),
            "command nested": (dict(seats={"w": {**seat, "command": [["x"]]}}), "command must be a non-empty list"),
            "env_allow string": (dict(seats={"w": {**seat, "env_allow": "A"}}), "env_allow must be a list"),
            "budget bool": (dict(seats={"w": {**seat, "max_budget_usd": True}}), "max_budget_usd"),
            "timeout null": (dict(timeout_seconds=None), "timeout_seconds"),
            "allow_write number": (dict(allow_write=1), "allow_write must be a boolean"),
            "worktree number": (dict(worktree_root=5), "worktree_root"),
            "relative worktree": (dict(worktree_root="rel/dir"), "worktree_root"),
        }
        for label, (over, reason) in cases.items():
            with self.subTest(label):
                self.refused(self.config(**over), reason)
        for label, raw in (("nan budget", '{"seats":{"c":{"harness":"claude-code","model":"m","max_budget_usd":NaN}}}'),
                           ("config not object", "[1]"), ("duplicate key", '{"permit":[],"permit":[]}')):
            with self.subTest(label):
                path = self.dir / "raw.json"
                path.write_text(raw)
                self.refused(str(path), "")

    def test_nonfinite_numbers_refused(self):
        path = self.dir / "raw.json"
        path.write_text('{"timeout_seconds": Infinity}')
        self.refused(str(path), "timeout_seconds")
        path.write_text('{"seats":{"c":{"harness":"claude-code","model":"m","max_budget_usd":NaN}}}')
        self.refused(str(path), "max_budget_usd")


class WriteRoleTests(Base):
    def cfg(self, role="builder", **over):
        base = dict(
            seats={"w": {"harness": "local-command", "command": [str(self.bin / "worker")]}},
            roles={role: ["w"]}, permit=[role], allow_write=True, worktree_root=str(self.dir / "wt"),
        )
        base.update(over)
        return self.config(**base)

    def run_in(self, cfg, workdir, role="builder"):
        return self.dispatch("run", "--config", cfg, "--run-dir", str(self.runs), "--workdir", str(workdir),
                             "--role", role, "--intent", "x")

    def test_write_refused_without_allow_write(self):
        self.fake("worker")
        code, out = self.run_in(self.cfg(allow_write=False), self.repo())
        self.assertEqual(code, 2)
        self.assertIn("allow_write", out["dispatch"]["refused"])
        self.assertEqual(self.calls("worker"), [])

    def test_every_role_holding_bash_write_or_edit_is_gated(self):
        self.fake("worker")
        roles = dispatch_module.load_roles()
        gated = [r for r, s in roles.items() if {"Bash", "Write", "Edit"} & set(s["tools"])]
        self.assertTrue({"verifier", "surveyor", "pipeline-runner", "steward", "builder"} <= set(gated))
        for role in ("pipeline-runner", "verifier", "surveyor", "steward"):
            code, out = self.run_in(self.cfg(role=role, allow_write=False), self.repo(), role)
            self.assertEqual(code, 2, role)
            self.assertIn("allow_write", out["dispatch"]["refused"], role)
        self.assertEqual(self.calls("worker"), [])

    def test_pipeline_runner_runs_in_worktree_when_allowed(self):
        self.fake("worker")
        repo = self.repo()
        code, out = self.run_in(self.cfg(role="pipeline-runner"), repo, "pipeline-runner")
        self.assertEqual(code, 0, out)
        wt = out["dispatch"]["worktree"]
        self.assertEqual(Path(self.calls("worker")[0]["cwd"]).resolve(), Path(wt["path"]).resolve())

    def test_write_refused_outside_git_repo(self):
        self.fake("worker")
        plain = self.dir / "plain"
        plain.mkdir()
        code, out = self.run_in(self.cfg(), plain)
        self.assertEqual(code, 2)
        self.assertIn("git repository", out["dispatch"]["refused"])
        self.assertEqual(self.calls("worker"), [])
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_write_runs_in_new_worktree(self):
        self.fake("worker")
        repo = self.repo()
        code, out = self.run_in(self.cfg(), repo)
        self.assertEqual(code, 0, out)
        wt = out["dispatch"]["worktree"]
        self.assertTrue(wt["branch"].startswith("dispatch/builder-"))
        self.assertTrue(Path(wt["path"]).is_dir())
        self.assertEqual(Path(self.calls("worker")[0]["cwd"]).resolve(), Path(wt["path"]).resolve())
        self.assertTrue(Path(wt["path"]).resolve().is_relative_to((self.dir / "wt").resolve()))
        branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", wt["branch"]], capture_output=True, text=True)
        self.assertIn(wt["branch"], branches.stdout)

    def test_worktree_add_does_not_run_repo_hooks(self):
        self.fake("worker")
        repo = self.repo()
        marker = self.dir / "hook-ran"
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "control", str(self.dir / "control")],
                       check=True, capture_output=True, env=env)
        self.assertTrue(marker.exists(), "control: the hook must fire under plain git")
        marker.unlink()
        code, out = self.run_in(self.cfg(), repo)
        self.assertEqual(code, 0, out)
        self.assertFalse(marker.exists(), "worktree add ran a repository hook")


class InstallTests(Base):
    @staticmethod
    def install(target):
        target.mkdir(parents=True)
        for rel in INSTALL:
            shutil.copytree(ROOT / rel, target / rel, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        return target

    def test_integration_lists_the_install_set(self):
        text = (ORCH / "INTEGRATION.md").read_text()
        for rel in INSTALL:
            self.assertIn(f"`{rel}", text)

    def test_status_plan_and_run_work_from_the_install_set_alone(self):
        self.fake("worker", "print('hi')")
        tree = self.install(self.dir / "tree")
        script = str(tree / "modules/orchestration/dispatch.py")
        cfg = self.config()
        for argv in (["status", "--config", cfg], ["plan", "--config", cfg, "--role", "researcher"]):
            proc = subprocess.run([sys.executable, "-B", script, *argv], capture_output=True, text=True, cwd=self.dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(
            [sys.executable, "-B", script, *self.run_args(cfg, "--role", "researcher", "--intent", "x")],
            capture_output=True, text=True, cwd=self.dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["result"]["outcome"], "succeeded")


class StructureTests(unittest.TestCase):
    def frontmatter(self, path):
        text = path.read_text(encoding="utf-8")
        header = text[4:].split("\n---\n", 1)[0]
        return dict(line.split(": ", 1) for line in header.splitlines())

    def test_roles_json_agrees_with_agents(self):
        roles = json.loads((ORCH / "roles.json").read_text())["roles"]
        files = {}
        for p in (ROOT / "agents").glob("*.md"):
            files[p.stem] = p
        for p in (ROOT / "agents").glob("*/*.md"):
            if p.stem == p.parent.name:
                files[p.stem] = p
        self.assertEqual(set(roles), set(files))
        for name, spec in roles.items():
            tools = [t.strip() for t in self.frontmatter(files[name])["tools"].split(",")]
            self.assertEqual(spec["tools"], tools, name)
            self.assertEqual(spec["access"], "write" if {"Bash", "Write", "Edit"} & set(tools) else "read", name)
        words = [w for spec in roles.values() for w in spec["intent"]]
        self.assertEqual(len(words), len(set(words)), "intent words must be unique across roles")

    def test_descriptor_is_valid_through_the_module_check(self):
        from modules.contracts import contracts
        d = contracts.check_module_descriptor(ORCH, module="orchestration")
        self.assertEqual((d["claimed_support"], d["evidence"]), ("planned", []))
        self.assertIn("core-contracts", [x["identifier"] for x in d["dependencies"]])
        example = json.loads((ORCH / "example.json").read_text())
        self.assertIn("seats", example)

    def test_no_route_import(self):
        for path in (DISPATCH, *(ROOT / "adapters/runtime").glob("*.py")):
            tree = ast.parse(path.read_text())
            names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            self.assertFalse([n for n in names if "route" in n.split(".")], path)
            self.assertNotIn("shell=True", path.read_text())


if __name__ == "__main__":
    unittest.main()
