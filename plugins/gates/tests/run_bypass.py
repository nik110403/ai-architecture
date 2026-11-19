#!/usr/bin/env python3
"""Shared bypass corpus through the Python destructive-command tripwire.

fixtures/bypass-corpus.json is also replayed through the structural parser (opencode/tests/u11-structural.test.ts).
Every `deny`/`ask` entry must be denied or asked; every `allow` entry must pass. `--hooks DIR` points at a
(mutated) copy of the hooks directory. The Python guard stays a stdlib tripwire over the literal text: only the
tree-sitter classifier may auto-approve a command."""
import json, os, subprocess, sys, tempfile

from hook_process import run as run_process

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "..", "hooks")
if "--hooks" in sys.argv:
    HOOKS = sys.argv[sys.argv.index("--hooks") + 1]
CORPUS = json.load(open(os.path.join(HERE, "fixtures", "bypass-corpus.json")))
env = dict(os.environ, HOME=tempfile.mkdtemp(), CLAUDE_HOOK_STATE_DIR=tempfile.mkdtemp())
env.pop("AIARCH_CONFIG", None)


def decide(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": tempfile.gettempdir()})
    r = run_process(["bash", os.path.join(HOOKS, "bash-destructive-guard.sh")], input=payload, capture_output=True, text=True, env=env)
    out = r.stdout.strip()
    return "allow" if not out else json.loads(out)["hookSpecificOutput"]["permissionDecision"]


passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name} {detail}")


assert len(CORPUS["entries"]) >= 20 and len(CORPUS["allow"]) >= 5
for e in CORPUS["entries"]:
    cmd = e["cmd"] + ("A" * (1 << 20) if e.get("gen") == "megabyte" else "")
    if e["expect"] in ("deny", "ask"):
        got = decide(cmd)
        check(f"corpus {e['expect']}: {e.get('note') or e['cmd'][:40]!r}", got in ("deny", "ask"), f"got {got} for {e['cmd'][:60]!r}")
for cmd in CORPUS["allow"]:
    got = decide(cmd)
    check(f"corpus allow: {cmd!r}", got == "allow", f"got {got}")
print(f"\nbypass corpus (python guard): {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
