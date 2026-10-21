#!/usr/bin/env python3
"""Config-scope tests: overrides come ONLY from user scope ($AIARCH_CONFIG or
~/.config/ai-architecture/gates.json). A project-scope file can never loosen a gate.
The only project file the destructive guard reads is <cwd>/.claude/destructive-patterns.json,
and it can only ADD patterns."""

import json
import os
import subprocess
import sys
import tempfile

from hook_process import run as run_process

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "bash-destructive-guard.sh")
HOME = os.environ["HOME"]
passed = failed = 0


def decide_full(command, cwd, env_extra=None, drop_cfg_env=True, proc_cwd=None):
    env = dict(os.environ)
    if drop_cfg_env:
        env.pop("AIARCH_CONFIG", None)
    env.update(env_extra or {})
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    r = run_process(
        ["bash", HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=proc_cwd,
    )
    out = r.stdout.strip()
    if not out:
        return "allow", ""
    j = json.loads(out)["hookSpecificOutput"]
    return j["permissionDecision"], j.get("permissionDecisionReason", "")


def decide(command, cwd, env_extra=None, drop_cfg_env=True, proc_cwd=None):
    return decide_full(command, cwd, env_extra, drop_cfg_env, proc_cwd)[0]


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}: expected {want}, got {got}")


proj = os.path.realpath(tempfile.mkdtemp())

# Baseline with no user config at all: floor + shipped defaults.
check("baseline: rm -rf ~/.ssh denied", decide("rm -rf ~/.ssh", proj), "deny")
check(
    "baseline: shipped default root ~/.claude denied",
    decide("rm -rf ~/.claude/skills", proj),
    "deny",
)
check(
    "baseline: unlisted dir is allowed", decide("rm -rf ~/vault/x/build", proj), "allow"
)

# Project-scope attempts to disable / loosen the guard.
attempts = {
    ".claude/gates.json": {
        "protected_roots": [],
        "protected_names": [],
        "enabled": False,
        "disabled": True,
    },
    "gates.json": {"protected_roots": [], "enabled": False},
    ".config/ai-architecture/gates.json": {"protected_roots": [], "enabled": False},
    ".claude/ai-architecture/gates.json": {"protected_roots": [], "enabled": False},
    ".claude/settings.json": {"env": {"AIARCH_CONFIG": "/dev/null"}},
}
for rel, body in attempts.items():
    path = os.path.join(proj, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(body, open(path, "w"))
check(
    "project-scope config tries to disable guard: rm -rf ~/.ssh still denied",
    decide("rm -rf ~/.ssh", proj),
    "deny",
)
check(
    "project-scope config tries to disable guard: secret read still denied",
    decide("cat ~/.ssh/id_ed25519", proj),
    "deny",
)
check(
    "project-scope config tries to disable guard: rm -rf / still denied",
    decide("rm -rf /", proj),
    "deny",
)
check(
    "project-scope config tries to disable guard: default root still denied",
    decide("rm -rf ~/.claude/skills", proj),
    "deny",
)

# Project destructive-patterns.json: additive only.
pp = os.path.join(proj, ".claude", "destructive-patterns.json")
json.dump(
    {
        "deny": ["\\bfrobnicate\\b"],
        "ask": ["\\bdeploy-prod\\b"],
        "allow": ["rm -rf /"],
        "suppress": ["mkfs"],
    },
    open(pp, "w"),
)
check("project pattern can ADD a deny rule", decide("frobnicate --all", proj), "deny")
check("project pattern can ADD an ask rule", decide("deploy-prod now", proj), "ask")
check(
    "project 'allow'/'suppress' keys have no effect on built-in rules",
    decide("rm -rf /", proj),
    "deny",
)
check(
    "project 'suppress' key does not lift mkfs",
    decide("mkfs.ext4 /dev/sda1", proj),
    "deny",
)
json.dump({"deny": ["(a+)+$"]}, open(pp, "w"))
check(
    "project pattern with a nested quantifier is rejected (command not blocked by it)",
    decide("aaaaaaaaaaaaaaaaaaaaaaaaaaaa!", proj),
    "allow",
)
os.remove(pp)

# User scope via AIARCH_CONFIG.
ucfg = os.path.join(tempfile.mkdtemp(), "gates.json")
json.dump(
    {
        "protected_roots": ["~/vault"],
        "protected_names": ["vault"],
        "deny_patterns": ["\\bnuke-it\\b"],
    },
    open(ucfg, "w"),
)
env = {"AIARCH_CONFIG": ucfg}
check(
    "user config: configured protected root denied",
    decide("rm -rf ~/vault", proj, env),
    "deny",
)
check(
    "user config: name-protected path denied anywhere",
    decide("rm -rf /srv/data/vault", proj, env),
    "deny",
)
check("user config: extra deny pattern applies", decide("nuke-it", proj, env), "deny")
check(
    "user config cannot remove shipped defaults (lists are unioned)",
    decide("rm -rf ~/.claude/skills", proj, env),
    "deny",
)

# User scope via the default location under HOME.
cfgdir = os.path.join(HOME, ".config", "ai-architecture")
os.makedirs(cfgdir, exist_ok=True)
json.dump(
    {"protected_roots": ["~/crown"], "protected_names": ["crown"]},
    open(os.path.join(cfgdir, "gates.json"), "w"),
)
check(
    "default user path ~/.config/ai-architecture/gates.json is honoured",
    decide("rm -rf ~/crown", proj),
    "deny",
)

# F4: $HOME is not trusted. A project settings.json can set env.HOME; protected roots and the
# user config must keep coming from the passwd database.
evil = tempfile.mkdtemp()
check(
    "F4 HOME=<evil>: user config under the real home still applies",
    decide("rm -rf ~/crown", proj, {"HOME": evil}),
    "deny",
)
check(
    "F4 HOME=<evil>: ssh floor not moved",
    decide("cat ~/.ssh/id_rsa", proj, {"HOME": evil}),
    "deny",
)
check(
    "F4 HOME=<evil>: shipped default root still denied",
    decide("rm -rf ~/.claude/skills", proj, {"HOME": evil}),
    "deny",
)

# Malformed user config: baseline stays in force and the guard fails closed (see F2 below).
open(os.path.join(cfgdir, "gates.json"), "w").write("{ this is not json")
check(
    "malformed user config: baseline still denies ~/.ssh",
    decide("rm -rf ~/.ssh", proj),
    "deny",
)
check(
    "malformed user config: previously configured root no longer applies, floor intact",
    decide("cat ~/.ssh/id_rsa", proj),
    "deny",
)

# F2: a config that exists but is malformed or wrongly typed fails closed, visibly.
CFGF = os.path.join(cfgdir, "gates.json")
open(CFGF, "w").write("{ this is not json")
dec, why = decide_full("rm -rf ~/.claude/skills", proj)
check("F2 malformed JSON: protected delete denied", dec, "deny")
check(
    "F2 malformed JSON: reason names the config as invalid",
    "config" in why and "invalid" in why and "fix it" in why,
    True,
)
check(
    "F2 malformed JSON: even a harmless command is denied until fixed",
    decide("ls", proj),
    "deny",
)
json.dump({"protected_roots": "~/code"}, open(CFGF, "w"))
dec, why = decide_full("rm -rf ~/code", proj)
check(
    "F2 wrong-typed protected_roots string: denied with the invalid-config message",
    (dec, "invalid" in why),
    ("deny", True),
)
json.dump({"edit_budget_models": {"m": "two"}}, open(CFGF, "w"))
check("F2 wrong-typed budget value: denied", decide("ls", proj), "deny")
json.dump({"protected_roots": ["~/crown"]}, open(CFGF, "w"))
check("F2 valid config again: guard recovers", decide("ls", proj), "allow")

# F2 other hooks: report on stderr, keep defaults.
open(CFGF, "w").write("{ nope")
r = run_process(
    [sys.executable, os.path.join(HERE, "..", "hooks", "no-code-guard.py")],
    input="{}",
    capture_output=True,
    text=True,
)
check(
    "F2 no-code-guard reports the invalid config on stderr",
    "invalid" in r.stderr and "fix it" in r.stderr,
    True,
)
os.remove(CFGF)
check("F2 missing config file is fine (defaults)", decide("ls", proj), "allow")

# F3: AIARCH_CONFIG pointing inside the project (e.g. from project settings env) is ignored.
inproj = os.path.join(proj, "evil-gates.json")
open(inproj, "w").write(
    "{ broken"
)  # if honoured, the guard would deny everything as invalid config
dec, why = decide_full("ls", proj, {"AIARCH_CONFIG": inproj}, proc_cwd=proj)
check(
    "F3 AIARCH_CONFIG inside the project dir is ignored (default user path used)",
    (dec, "invalid" in why),
    ("allow", False),
)
dec, why = decide_full(
    "ls", proj, {"AIARCH_CONFIG": inproj, "CLAUDE_PROJECT_DIR": proj}
)
check("F3 same via CLAUDE_PROJECT_DIR", (dec, "invalid" in why), ("allow", False))
dec, why = decide_full("ls", proj, {"AIARCH_CONFIG": inproj})
check(
    "F3 control: same file outside any project dir is honoured (denied as invalid)",
    (dec, "invalid" in why),
    ("deny", True),
)

# Wrapper fail-closed: no python3 on PATH.
r = run_process(
    ["/bin/bash", HOOK],
    input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": proj}
    ),
    capture_output=True,
    text=True,
    env={"PATH": "/nonexistent", "HOME": HOME},
)
out = r.stdout.strip()
check(
    "missing python3: the guard wrapper denies (fails closed)",
    json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "allow",
    "deny",
)

# ---- N1/N2: payload-first, fail closed on an unparseable payload, config errors visible ----
HOOKS = os.path.join(HERE, "..", "hooks")
ALL = [
    ("bash-destructive-guard.sh", "pretool"),
    ("no-code-guard.py", "pretool"),
    ("edit-budget-gate.py", "pretool"),
    ("review-stop-gate.py", "stop"),
    ("clean-state-check.py", "stop"),
    ("resume-inject.py", "session"),
]


def run_hook(hook, stdin, env_extra=None):
    env = dict(os.environ)
    env.pop("AIARCH_CONFIG", None)
    env.update(env_extra or {})
    cmd = (["bash"] if hook.endswith(".sh") else [sys.executable]) + [
        os.path.join(HOOKS, hook)
    ]
    r = run_process(
        cmd, input=stdin, capture_output=True, text=True, env=env, cwd=proj, timeout=30
    )
    out = r.stdout.strip()
    return (json.loads(out) if out else None), r.returncode


def verdict(j):
    if j is None:
        return "allow"
    if "decision" in j:
        return j["decision"]
    if "hookSpecificOutput" in j and "permissionDecision" in j["hookSpecificOutput"]:
        return j["hookSpecificOutput"]["permissionDecision"]
    return "message" if "systemMessage" in j else "other"


for hook, kind in ALL:
    for bad_payload in ("not json at all", "[1, 2]", ""):
        j, rc = run_hook(hook, bad_payload)
        want = {"pretool": "deny", "stop": "block", "session": "message"}[kind]
        check(
            f"N2 {hook}: unparseable payload {bad_payload[:10]!r} fails closed ({want})",
            (verdict(j), rc),
            (want, 0),
        )

# N2: AIARCH_CONFIG=/dev/stdin must fail closed (the wrapper's stdin is a payload temp file, so it parses as a config with unknown keys).
j, rc = run_hook(
    "bash-destructive-guard.sh",
    json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": proj}),
    {"AIARCH_CONFIG": "/dev/stdin"},
)
check(
    "N2 AIARCH_CONFIG=/dev/stdin: destructive guard denies as refused (raw-path check)",
    (verdict(j), "refused" in json.dumps(j)),
    ("deny", True),
)
lnk = os.path.join(tempfile.mkdtemp(), "cfg.lnk")
os.symlink("/dev/stdin", lnk)
j, rc = run_hook(
    "bash-destructive-guard.sh",
    json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": proj}),
    {"AIARCH_CONFIG": lnk},
)
check(
    "N2 AIARCH_CONFIG=symlink->/dev/stdin: denied as refused (symlink-hop check)",
    (verdict(j), "refused" in json.dumps(j)),
    ("deny", True),
)
fifo = os.path.join(tempfile.mkdtemp(), "cfg.fifo")
os.mkfifo(fifo)
j, rc = run_hook(
    "review-stop-gate.py",
    json.dumps({"stop_hook_active": False}),
    {"AIARCH_CONFIG": fifo},
)
check(
    "N2 a FIFO config path does not hang and is reported as invalid",
    "not a regular file" in json.dumps(j),
    True,
)

# N1: unknown top-level key = invalid config; surfaced by every hook through systemMessage.
bad_cfg = os.path.join(tempfile.mkdtemp(), "gates.json")
json.dump({"protected_root": ["~/x"]}, open(bad_cfg, "w"))  # misspelt key
BASH_LS = json.dumps(
    {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": proj}
)
payloads = {
    "pretool": json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/a.md", "content": "x"},
            "cwd": proj,
        }
    ),
    "stop": json.dumps({"stop_hook_active": True, "cwd": proj}),
    "session": json.dumps({"source": "startup", "cwd": proj}),
}
for hook, kind in ALL:
    pl = BASH_LS if hook == "bash-destructive-guard.sh" else payloads[kind]
    j, rc = run_hook(hook, pl, {"AIARCH_CONFIG": bad_cfg})
    blob = json.dumps(j)
    check(
        f"N1 {hook}: unknown config key surfaced to the user",
        ("unknown key" in blob and "protected_root" in blob),
        True,
    )
j, _ = run_hook("bash-destructive-guard.sh", BASH_LS, {"AIARCH_CONFIG": bad_cfg})
check(
    "N1 unknown key: destructive guard denies (same fail-closed path as F2)",
    verdict(j),
    "deny",
)
json.dump({"_comment": "fine", "protected_roots": ["~/x"]}, open(bad_cfg, "w"))
j, _ = run_hook("bash-destructive-guard.sh", BASH_LS, {"AIARCH_CONFIG": bad_cfg})
check("N1 control: known keys plus a _comment key are valid", verdict(j), "allow")

# F2 / security HIGH: size cap, check order, project pattern cap and regex budget.
import time


def timed(command, cwd, env_extra=None):
    t0 = time.monotonic()
    got = decide_full(command, cwd, env_extra)
    return got, time.monotonic() - t0


(got, why), dt = timed("cp " * 4000 + "; cat ~/.ssh/id_rsa", proj)
check("F2 4000x 'cp ' + secret read: denied", got, "deny")
check("F2 4000x 'cp ' + secret read: denied fast (<2s)", dt < 2, True)
check("F2 oversized command: reason names the cap", "8192" in why, True)
(got, why), dt = timed("cp " * 2600 + "; cat ~/.ssh/id_rsa", proj)
check(
    "F2 ~8KB command (under the cap) with a secret read: denied, not timed out",
    (got, dt < 5),
    ("deny", True),
)
os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
pp = os.path.join(proj, ".claude", "destructive-patterns.json")
SLOW = "^.*.*.*.*.*.*.*.*.*.*.*.*.*.*.*.*.*Q%d$"
json.dump({"deny": [SLOW % i for i in range(50)]}, open(pp, "w"))
(got, why), dt = timed("echo " + "a" * 3000, proj)
check(
    "security HIGH: 50 slow project patterns -> denied as a config error, in time",
    (got, "cap" in why, dt < 5),
    ("deny", True, True),
)
json.dump({"deny": ["x%d" % i for i in range(33)]}, open(pp, "w"))
check(
    "security HIGH: 33 project patterns -> deny (cap 32)",
    decide("echo hi", proj),
    "deny",
)
json.dump({"deny": ["x%d" % i for i in range(32)]}, open(pp, "w"))
check(
    "security HIGH: 32 project patterns -> still allowed",
    decide("echo hi", proj),
    "allow",
)
json.dump({"deny": [SLOW % i for i in range(30)]}, open(pp, "w"))
(got, why), dt = timed("echo " + "a" * 3000, proj)
check(
    "security HIGH: 30 slow project patterns -> deny on exhausted regex budget, in time",
    (got, "budget" in why, dt < 6),
    ("deny", True, True),
)
(got, why), dt = timed("cat ~/.ssh/id_rsa", proj)
check(
    "security HIGH: secret read is judged BEFORE project regexes (secret reason, fast)",
    (got, "budget" in why, dt < 3),
    ("deny", False, True),
)
os.remove(pp)

# Project rule file must be a bounded regular file (round-3 HIGH): symlink / FIFO / oversize deny fast.
import time as _t


def _timed(cmd):
    t0 = _t.time()
    r = decide_full(cmd, proj, None, True, None)
    return r, _t.time() - t0


os.remove(pp) if os.path.lexists(pp) else None
os.symlink("/dev/zero", pp)
(got, why), dt = _timed("echo hi")
check(
    "security HIGH: rule file symlink -> /dev/zero denies quickly",
    (got, dt < 2),
    ("deny", True),
)
os.remove(pp)
os.mkfifo(pp)
(got, why), dt = _timed("echo hi")
check("security HIGH: rule file FIFO denies quickly", (got, dt < 2), ("deny", True))
os.remove(pp)
open(pp, "w").write('{"deny": [], "pad": "' + "a" * 70000 + '"}')
(got, why), dt = _timed("echo hi")
check(
    "security HIGH: oversize (>64 KB) rule file denies as config error",
    (got, "exceeds" in why),
    ("deny", True),
)
json.dump({"deny": ["forbidden-word"]}, open(pp, "w"))
check("rule file: normal file still works (allow)", decide("echo hi", proj), "allow")
check(
    "rule file: normal file still works (deny)",
    decide("echo forbidden-word", proj),
    "deny",
)
os.remove(pp)

# Round-3 N1: newline floods must deny within the total deadline, never time out into allow.
for name, cmd in (
    ("cat <key> + 8100 newlines", "cat ~/.ssh/id_rsa" + "\n" * 8100 + "x"),
    (
        "cat <key>; chmod - + 200 r + 7900 newlines",
        "cat ~/.ssh/id_rsa; chmod -" + "r" * 200 + "\n" * 7900 + "x",
    ),
):
    (got, why), dt = _timed(cmd)
    check(f"security N1: {name} denies in < 7 s", (got, dt < 7), ("deny", True))
check(
    "N1: normal multi-line command unchanged (allow)",
    decide("echo a\n\n  echo b", proj),
    "allow",
)
check(
    "N1: newline-separated secret read still denied",
    decide("echo a\n\n  cat ~/.ssh/id_rsa", proj),
    "deny",
)

print(f"\nconfig scope: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
