#!/usr/bin/env python3
"""Feeds fixtures/destructive-guard.json to the destructive-command guard as hook JSON on stdin.
No fixture command is ever executed or passed through a shell: only json.dumps -> subprocess input.
Tokens: @HOME@ in a command/cwd is replaced by the test HOME."""

import json, os, subprocess, sys, tempfile, time

from hook_process import run as run_process

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "bash-destructive-guard.sh")
CASES = json.load(open(os.path.join(HERE, "fixtures", "destructive-guard.json")))
# Additional ASK/near-miss forms from the pinned staged destructive-guard tests.
# Literal strings are hook input only, never shell commands.
ASK_FORMS = [
    ("git clean -fdx", "ask"),
    ("git branch -D main", "ask"),
    ("git stash clear", "ask"),
    ("git push origin +main", "ask"),
    ("git push origin :main", "ask"),
    ("git push origin --delete main", "ask"),
    ("git reflog expire --expire=now --all", "ask"),
    ('psql -c "DELETE FROM users"', "ask"),
    ('psql -c "DROP DATABASE prod"', "ask"),
    ("git stash drop", "ask"),
    ("git clean -x", "ask"),
    ("git clean -d", "ask"),
    ("git clean -f", "ask"),
    ("git push origin HEAD:+main", "ask"),
    ("git push -d origin feature", "ask"),
    ('mysql -e "TRUNCATE TABLE t"', "ask"),
    ("sqlite3 db -e 'DROP TABLE t'", "ask"),
    ("cd repo && git branch -D old", "ask"),
    ("git reflog delete HEAD@{1}", "ask"),
    ("git clean -n", "allow"),
    ("git clean -dn", "allow"),
    ("git clean --dry-run -fd", "allow"),
    ("git branch -d merged", "allow"),
    ("git branch -a", "allow"),
    ("git stash", "allow"),
    ("git stash list", "allow"),
    ("git stash pop", "allow"),
    ("git push origin main", "allow"),
    ("git push -u origin feature", "allow"),
    ("git push origin HEAD:main", "allow"),
    ("git reflog", "allow"),
    ("git reflog show", "allow"),
    ('psql -c "SELECT 1"', "allow"),
    ('git commit -m "delete from docs"', "allow"),
    ('find . -name "delete from"', "allow"),
    ("git branch --delete --force old", "ask"),
    ("git branch --delete -f old", "ask"),
    ("git branch --delete old", "allow"),
    ("git clean --force", "ask"),
    ("git push origin --delete=old", "ask"),
    ('psql --command="DROP SCHEMA app"', "ask"),
    ('mysql --execute "DELETE FROM users"', "ask"),
    ('db --eval "DROP DATABASE app"', "ask"),
    ("git stash clear; cat ~/.ssh/id_ed25519", "deny"),
    ("cat <<'DOC' >/tmp/note\ngit clean -fdx\nDOC", "allow"),
]
CASES += [
    dict(name="ASK forms: " + cmd, command=cmd, expect=expect)
    for cmd, expect in ASK_FORMS
]
if "--ask-forms-only" in sys.argv:
    CASES = CASES[-len(ASK_FORMS) :]
HOME = os.environ["HOME"]
P95_LIMIT_MS = 1000.0  # sanity bound only; wall time is load-dependent

cfg_dir = tempfile.mkdtemp()
cfg = os.path.join(cfg_dir, "gates.json")
open(cfg, "w").write(
    open(os.path.join(HERE, "fixtures", "test-config.json"))
    .read()
    .replace("@SANDBOX@", "/nonexistent/sandbox")
)
env = dict(os.environ, AIARCH_CONFIG=cfg)
workdir = tempfile.mkdtemp()

fail, times = 0, []
for c in CASES:
    sub = lambda s: s.replace("@HOME@", HOME)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": sub(c["command"])},
        "cwd": sub(c.get("cwd", workdir)),
    }
    t0 = time.perf_counter()
    r = run_process(
        ["bash", HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    times.append((time.perf_counter() - t0) * 1000)
    out = r.stdout.strip()
    got = (
        json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "allow"
    )
    if got == c["expect"]:
        print(f"ok   - {c['name']}")
    else:
        fail += 1
        print(f"FAIL - {c['name']}: expected {c['expect']}, got {got} {out[:200]}")

# Wall-clock cap: a guard that hangs (stub copy of the hooks dir, real wrapper) must be denied by the wrapper.
import shutil

stub_dir = os.path.join(tempfile.mkdtemp(), "hooks")
shutil.copytree(os.path.join(HERE, "..", "hooks"), stub_dir)
open(os.path.join(stub_dir, "bash-destructive-guard.py"), "w").write(
    "import time\ntime.sleep(60)\n"
)
t0 = time.perf_counter()
r = run_process(
    ["bash", os.path.join(stub_dir, "bash-destructive-guard.sh")],
    capture_output=True,
    text=True,
    env=env,
    input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": workdir}
    ),
)
el = time.perf_counter() - t0
try:
    dec = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]
except Exception:
    dec = "none"
if dec == "deny" and el < 9:
    print(f"ok   - wrapper denies a hung guard after {el:.1f}s")
else:
    fail += 1
    print(f"FAIL - hung guard: decision={dec} elapsed={el:.1f}s {r.stdout[:200]}")


def run_wrapper(hooks_dir, wrapper, payload, args=(), timeout=30):
    t0 = time.perf_counter()
    r = run_process(
        ["bash", os.path.join(hooks_dir, wrapper), *args],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return r.stdout, time.perf_counter() - t0


def check(name, ok, detail=""):
    global fail
    if ok:
        print(f"ok   - {name}")
    else:
        fail += 1
        print(f"FAIL - {name} {detail}")


def decision(out):
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return "allow" if not out.strip() else "none"


hooks = os.path.join(HERE, "..", "hooks")
BAD = "rm -rf " + "~"  # built here so no literal destructive command sits in this file


# heredoc opener line ending in a backslash: denied outright (no continuation emulation); other forms still decided
def deny_case(label, cmd):
    out, _ = run_wrapper(
        hooks,
        "bash-destructive-guard.sh",
        json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": workdir}
        ),
    )
    check(label, decision(out) == "deny", out[:200])


deny_case(
    "heredoc opener continued by backslash-newline is denied",
    "cat <<A >/tmp/f\\\nx; " + BAD + "\nA\n",
)
deny_case(
    "opener continuation hiding command on next line",
    "cat <<A >/tmp/f\\\nx\n" + BAD + "\nA\n",
)
for q in ("'A'", '"A"', "\\A"):
    deny_case(
        "quoted heredoc body ending in backslash cannot hide a command " + q,
        "cat <<" + q + " >/tmp/f\nx\\\nA\n" + BAD + "\nA\n",
    )
deny_case(
    "comment ending in backslash does not swallow next line", "echo hi # x\\\n" + BAD
)
deny_case(
    "bare # ending in backslash does not swallow next line", "echo hi #\\\n" + BAD
)
# large payloads are decided (not buffered byte-by-byte) well inside the 10 s hook limit
for label, cmd in (
    ("5 MB benign", "echo " + "a" * (5 * 1024 * 1024)),
    ("5 MB destructive tail", "echo " + "a" * (5 * 1024 * 1024) + "; " + BAD),
):
    out, el = run_wrapper(
        hooks,
        "bash-destructive-guard.sh",
        json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": workdir}
        ),
    )
    check(
        f"{label} payload decided in {el:.1f}s",
        decision(out) == "deny" and el < 9,
        out[:200],
    )
# run-hook.sh missing or not a regular file: the .sh itself must print the deny
solo = os.path.join(tempfile.mkdtemp(), "hooks")
shutil.copytree(hooks, solo)
os.remove(os.path.join(solo, "run-hook.sh"))
out, _ = run_wrapper(
    solo,
    "bash-destructive-guard.sh",
    json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": workdir}),
)
check("missing run-hook.sh denies", decision(out) == "deny", out[:200])
os.mkdir(os.path.join(solo, "run-hook.sh"))
out, _ = run_wrapper(
    solo,
    "bash-destructive-guard.sh",
    json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": workdir}),
)
check("run-hook.sh not a regular file denies", decision(out) == "deny", out[:200])
# stop hook failure: silent only when stop_hook_active is exactly true
open(os.path.join(stub_dir, "stub-stop.py"), "w").write("import sys\nsys.exit(3)\n")
for label, pl, want in (
    ("true", '{"stop_hook_active": true}', "{}"),
    ("false", '{"stop_hook_active": false, "cwd": "/my true dir"}', "block"),
    ("absent", "{}", "block"),
):
    out, _ = run_wrapper(stub_dir, "run-hook.sh", pl, args=("stop", "stub-stop.py"))
    got = "{}" if out.strip() == "{}" else ("block" if '"block"' in out else out)
    check(f"stop hook failure with stop_hook_active {label}", got == want, out[:200])

times.sort()
p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
print(
    f"latency wall: n={len(times)} p50={times[len(times) // 2]:.0f}ms p95={p95:.0f}ms (limit {P95_LIMIT_MS:.0f}ms)"
)
if p95 >= P95_LIMIT_MS:
    fail += 1
    print("FAIL - latency p95 over limit")
print(f"destructive-guard: {len(CASES)} cases, {fail} failed")
sys.exit(1 if fail else 0)
