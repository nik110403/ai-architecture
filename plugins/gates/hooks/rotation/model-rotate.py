#!/usr/bin/env python3
"""Opt-in PreToolUse hook (matcher "Task|Agent"): rotate the model of a sub-agent dispatch.

For a dispatch whose `subagent_type` is a role listed in the pool file, replace the
dispatch's `model` with a weighted-random pick from that role's models. Nothing else in
the tool input changes. This is a way to spread work across models (for example to compare
them), not an optimising policy: no state is read, every dispatch is an independent draw.

Fail safe: ANY problem (unparseable payload, other tool, role not in the pool, missing /
unreadable / oversized / malformed / wrong-version pool, mode other than "on", a malformed
entry in the role's list, no eligible model, MODEL_ROTATION=off) prints nothing and exits 0,
so the dispatch runs on its configured default. A model that is not listed in the pool is
never picked. Nothing here can deny a tool call.

Pool file (default ~/.config/ai-architecture/model-pool.json, override ROTATION_POOL):
  {"version": 1, "mode": "on", "independent_roles": ["reviewer"],
   "roles": {"builder": [{"model": "provider/model-a", "weight": 3},
                         {"model": "provider/model-b", "weight": 1, "enabled": false}]}}
Names and weights are validated strictly (see MODEL_RE / ROLE_RE); the payload and the pool
are untrusted input and nothing is interpolated into a shell.

Brief lines (both read only from the brief header = the lines before the first blank line,
at most 15):
  pin_arm: <model>      bypass the draw when <model> is an enabled entry of the role.
  builder_arm: <model>  for roles in `independent_roles`, exclude the model that built the
                        work under review (only when another model remains). If a
                        `builder_arm:` line appears more than once anywhere in the prompt it
                        is ignored AND rotation is skipped for independent roles.
An invalid pin is ignored and the weighted draw runs.

Output: updatedInput only, with no permissionDecision. Rotation does not approve dispatches
or start workers; the harness's existing permission policy still decides whether to run them.
A pool model name the harness does not accept may make the harness reject the dispatch
(it is not silently replaced by the default).

Env: MODEL_ROTATION=off disables. ROTATION_POOL / ROTATION_LOG must be absolute paths outside
CLAUDE_PROJECT_DIR and the cwd (a project must not choose them); otherwise the default pool is
used / no log is written. ROTATION_LOG appends one JSON row per decision and per fail-safe exit.
ROTATION_SEED=<text> makes the draw deterministic, honoured only together with ROTATION_TEST=1
(tests/replays; a seeded run always gets the same model); otherwise the draw uses secrets.randbelow.
"""

import json
import os
import random
import re
import secrets
import stat
import sys
import time

MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}\Z")
ROLE_RE = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
MAX_POOL_BYTES = 1_000_000
MAX_WEIGHT = 1_000_000


def normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def brief_header(text):
    lines = normalize_newlines(text).split("\n")
    end = next((i for i, l in enumerate(lines) if l.strip() == ""), len(lines))
    return "\n".join(lines[: min(end, 15)])


def env_path(name):
    """Absolute path from env that is NOT inside the project dir / cwd, else None."""
    v = os.environ.get(name)
    if not v or not os.path.isabs(v):
        return None
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"
        ),
    )
    from gates_config import _inside, project_dirs

    if any(_inside(v, d) for d in project_dirs()):
        return None
    return v


def _home():
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"
        ),
    )
    from gates_config import home

    return home()


def load_pool(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_POOL_BYTES:
            return None
        pool = json.load(f)
    if not isinstance(pool, dict) or pool.get("mode") != "on":
        return None
    if "version" in pool and pool["version"] != 1:
        return None
    if not isinstance(pool.get("roles"), dict):
        return None
    return pool


def role_arms(entries):
    """Return the enabled [(model, weight)] list, or None if any entry is malformed."""
    if not isinstance(entries, list) or not entries:
        return None
    out = []
    for e in entries:
        if not isinstance(e, dict):
            return None
        model, weight, enabled = e.get("model"), e.get("weight"), e.get("enabled", True)
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            return None
        if (
            isinstance(weight, bool)
            or not isinstance(weight, int)
            or not 0 <= weight <= MAX_WEIGHT
        ):
            return None
        if not isinstance(enabled, bool):
            return None
        if enabled and weight > 0:
            out.append((model, weight))
    return out


def draw(eligible):
    total = sum(w for _, w in eligible)
    seed = (
        os.environ.get("ROTATION_SEED")
        if os.environ.get("ROTATION_TEST") == "1"
        else None
    )
    r = (
        random.Random("rotation:" + seed).randrange(total)
        if seed
        else secrets.randbelow(total)
    )
    for model, w in eligible:
        if r < w:
            return model
        r -= w
    return eligible[-1][0]


def write_log(row):
    log = env_path("ROTATION_LOG")
    if not log:
        return
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True, mode=0o700)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(row) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


CTX = {"session_id": None, "role": None}


def bail(reason):
    """Fail-safe exit: print nothing; log a reason row when ROTATION_LOG is usable."""
    write_log(
        {
            "t": int(time.time()),
            "session_id": CTX["session_id"],
            "role": CTX["role"],
            "model": None,
            "pinned": False,
            "reason": reason,
        }
    )


def main():
    if os.environ.get("MODEL_ROTATION") == "off":
        return bail("MODEL_ROTATION=off")
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        return bail("payload is not an object")
    sid = payload.get("session_id")
    CTX["session_id"] = sid if isinstance(sid, str) else None
    if payload.get("hook_event_name") != "PreToolUse" or payload.get(
        "tool_name"
    ) not in ("Task", "Agent"):
        return bail("not a Task/Agent PreToolUse")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return bail("tool_input is not an object")
    role = tool_input.get("subagent_type")
    if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
        return bail("invalid subagent_type")
    CTX["role"] = role
    prompt = tool_input.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""

    path = env_path("ROTATION_POOL") or os.path.join(
        _home(), ".config", "ai-architecture", "model-pool.json"
    )
    try:
        pool = load_pool(path)
    except (OSError, ValueError):
        pool = None
    if pool is None:
        return bail("pool missing, unreadable or invalid")
    if role not in pool["roles"]:
        return bail("role not in pool")
    eligible = role_arms(pool["roles"][role])
    if not eligible:
        return bail("no eligible model for role")

    reasons = []
    builder = None
    header = brief_header(prompt)
    independent = pool.get("independent_roles")
    if isinstance(independent, list) and role in independent:
        pat = r"(?m)^builder_arm:[ \t]*(\S+)[ \t]*$"
        if len(re.findall(r"(?m)^builder_arm:", normalize_newlines(prompt))) > 1:
            return bail("builder_arm appears more than once; rotation skipped")
        m = re.search(pat, header)
        if m:
            builder = m.group(1)
            rest = [a for a in eligible if a[0] != builder]
            if rest:
                eligible = rest

    chosen, pinned = None, False
    m = re.search(r"(?m)^pin_arm:[ \t]*(\S+)[ \t]*$", header)
    if m:
        pin = m.group(1)
        if isinstance(independent, list) and role in independent and builder is None:
            # an independent role must not be pinned onto the builder's model by omitting builder_arm
            reasons.append("pin_arm ignored: independent role without builder_arm")
        elif pin == builder:
            reasons.append("pin_arm matches builder_arm; ignored")
        elif any(a[0] == pin for a in eligible):
            chosen, pinned = pin, True
        else:
            reasons.append("pin_arm is not an enabled model of this role; ignored")
    if chosen is None:
        chosen = draw(eligible)

    updated = dict(tool_input)
    updated["model"] = chosen

    write_log(
        {
            "t": int(time.time()),
            "session_id": CTX["session_id"],
            "role": role,
            "model": chosen,
            "pinned": pinned,
            "reason": "; ".join(reasons) or None,
        }
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": updated,
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # fail safe: never block, never print a partial answer
        try:
            bail("internal error")
        except BaseException:
            pass
    sys.exit(0)
