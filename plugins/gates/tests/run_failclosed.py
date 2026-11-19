#!/usr/bin/env python3
"""A hook that cannot run, crashes, hangs or answers with something that is not a JSON object must
fail CLOSED through run-hook.sh: deny (pretool) / block (stop). Stdlib only; each broken hook is a tiny
script dropped next to a COPY of run-hook.sh, so no real gate is touched.

Native-unexercised: an empty PATH that stops the harness itself from resolving `bash` (hooks.json runs
`bash ...`) exits 127, which Claude Code treats as non-blocking -- that fail-open is outside this plugin
(see README). Here the empty PATH is applied to run-hook.sh itself, started by absolute path."""
import json, os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "hooks")
TMP = tempfile.mkdtemp()
HOOKS = os.path.join(TMP, "hooks")
shutil.copytree(SRC, HOOKS, ignore=shutil.ignore_patterns("__pycache__", "rotation"))
BASH = shutil.which("bash")

SCRIPTS = {
    "raises.py": "raise RuntimeError('boom')\n",
    "sleeps.py": "import time\ntime.sleep(60)\n",
    "hello.py": "print('hello')\n",
    "oops.py": "print('{oops')\n",
    "array.py": "print('[]')\n",
    "unreadable.py": "print('{}')\n",
}
for name, body in SCRIPTS.items():
    open(os.path.join(HOOKS, name), "w").write(body)
os.chmod(os.path.join(HOOKS, "unreadable.py"), 0)

IS_ROOT = os.geteuid() == 0
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name} {detail}")


def invoke(kind, script, active=False, empty_path=False, wrapper=False):
    payload = {"session_id": "s", "cwd": TMP, "tool_name": "Bash", "tool_input": {"command": "true"}}
    if active:
        payload["stop_hook_active"] = True
    env = dict(os.environ, HOME=TMP)
    if empty_path:
        env["PATH"] = ""
    argv = [BASH, os.path.join(HOOKS, "bash-destructive-guard.sh")] if wrapper else [BASH, os.path.join(HOOKS, "run-hook.sh"), kind, script]
    r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
    return r


def verdict(kind, r):
    """'deny'/'block' only for a well-formed JSON object saying so; 'allow' for empty or {}; else 'INVALID'."""
    out = r.stdout.strip()
    if not out:
        return "allow"
    try:
        obj = json.loads(out)
    except ValueError:
        return "INVALID"
    if not isinstance(obj, dict):
        return "INVALID"
    if kind == "pretool":
        return obj.get("hookSpecificOutput", {}).get("permissionDecision", "allow")
    return obj.get("decision", "allow")


jobs = []
for kind, want in (("pretool", "deny"), ("stop", "block")):
    for script in ("raises.py", "sleeps.py", "hello.py", "oops.py", "array.py", "unreadable.py"):
        if script == "unreadable.py" and IS_ROOT:
            print("SKIP - unreadable hook (mode 000) is not enforced for root; skipped, not passed")
            continue
        jobs.append((f"{kind}: {script} fails closed", kind, want, dict(script=script)))
    jobs.append((f"{kind}: empty PATH (run-hook.sh by absolute path) fails closed", kind, want, dict(script="no-code-guard.py", empty_path=True)))
jobs.append(("pretool: empty PATH through the bash-destructive-guard wrapper denies", "pretool", "deny", dict(script="", empty_path=True, wrapper=True)))

with ThreadPoolExecutor(8) as pool:
    results = list(pool.map(lambda j: invoke(j[1], **j[3]), jobs))
for (name, kind, want, _), r in zip(jobs, results):
    got = verdict(kind, r)
    check(name, got == want, f"expected {want}, got {got}: {r.stdout.strip()[:120]!r} rc={r.returncode}")
    check(name + " (exit 0, single JSON object)", r.returncode == 0 and r.stdout.strip().count("\n") == 0, f"rc={r.returncode} out={r.stdout!r}")

# a stop hook that already blocked once must let the turn end even when the next hook crashes
r = invoke("stop", "raises.py", active=True)
check("stop: crash with stop_hook_active=true is silent ({})", verdict("stop", r) == "allow", r.stdout)
# a well-formed object still passes through untouched
open(os.path.join(HOOKS, "good.py"), "w").write("print('{\"decision\": \"block\", \"reason\": \"x\"}')\n")
check("stop: a valid JSON object passes through", verdict("stop", invoke("stop", "good.py")) == "block")

os.chmod(os.path.join(HOOKS, "unreadable.py"), 0o644)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nfail-closed hooks: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
