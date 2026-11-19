#!/usr/bin/env python3
"""Bounded intent -> role -> native worker dispatch (stdlib only).

Subcommands: plan (resolve, launch nothing), run (launch one worker), status (PATH lookup only).
Default denies everything: a recipient config must permit the role, and paid harnesses also need
`allow_spend` in the config AND --allow-spend on the command line. Worker stdout is untrusted data;
the outcome is computed from the exit code and from output the dispatcher captured itself (never
re-read from disk), and a harness check can only move an outcome down to failed.
"""

import argparse
import ctypes
import datetime
import hashlib
import importlib
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.contracts.contracts import check_record, unique_keys  # noqa: E402

HARNESS_MODULES = {
    "claude-code": "adapters.runtime.claude_code",
    "opencode": "adapters.runtime.opencode",
    "local-command": "adapters.runtime.local_command",
}
PAID = {"claude-code": True, "opencode": True, "local-command": False}
PAID_EXECUTABLES = {"claude", "opencode"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}\Z")
EFFORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
BASE_ENV = ("PATH", "HOME", "LANG", "TMPDIR")
CONFIG_KEYS = {"seats", "roles", "permit", "allow_write", "allow_spend", "timeout_seconds", "worktree_root"}
SEAT_KEYS = {"harness", "model", "effort", "env_allow", "command", "max_budget_usd"}
WRITE_TOOLS = {"Bash", "Write", "Edit"}
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
PUMP_GRACE_SECONDS = 2.0
GIT_TIMEOUT_SECONDS = 60
VERSION_TIMEOUT_SECONDS = 10
PLACEHOLDER = "<job-dir>/agents.json"
# Printed by OpenCode 1.18.31 on stderr only; scanned on stderr only.
FALLBACK_MARKERS = ("Falling back to default agent", "auto-rejecting")
# OpenCode 1.18.31 tool_use error prefixes that mean a denied tool call. A harness change fails closed.
DENIAL_PREFIXES = ("The user has specified a rule", "Model tried to call unavailable tool")
EXIT_FAILED, EXIT_REFUSED = 1, 2
SAFE_PATH = "/usr/bin:/bin"  # what a worker gets when the configured PATH has no absolute entry
CLEANUP_SECONDS = 5.0  # bound on the descendant kill-and-reap loop
CLEANUP_ROUNDS = 20
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
HOLD_ZOMBIE = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")  # exit can be seen without reaping
MARKER_VAR = "AI_ARCH_JOB_MARKER"
USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "cost_usd")
CONFINEMENT_WRITE = "none: same-uid worktree, not a security boundary"
CONFINEMENT_READ = "harness tool allowlist; reads anything the user can"


class Refusal(Exception):
    def __init__(self, reason, skipped=None, role=None, job_id=None):
        super().__init__(reason)
        self.reason, self.skipped, self.role, self.job_id = reason, skipped or [], role, job_id


class ReuseRefusal(Refusal):
    """The job id belongs to another job: the refusal must never name it."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_roles():
    return json.loads((HERE / "roles.json").read_text(encoding="utf-8"))["roles"]


def dump_json(obj):
    """The one serialiser for envelopes and job files: strict JSON, never NaN or Infinity."""
    return json.dumps(obj, indent=2, allow_nan=False)


def result_schema():
    return json.loads((ROOT / "modules/contracts/schemas.json").read_text(encoding="utf-8"))["result"]


def check_name(value, pattern, what):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refusal(f"invalid {what}: {value!r}")
    return value


def need(condition, message):
    if not condition:
        raise Refusal(message)


def is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def usable(value):
    """A finite number >= 0 (usage figures only)."""
    return is_number(value) and value >= 0


def is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def load_config(path, roles):
    need(path, "no --config given: default denies everything")
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except (OSError, ValueError) as error:
        raise Refusal(f"config unreadable: {error}")
    need(isinstance(config, dict), "config must be an object")
    need(not set(config) - CONFIG_KEYS, f"unknown config keys: {sorted(set(config) - CONFIG_KEYS)}")
    seats = config.get("seats", {})
    need(isinstance(seats, dict), "seats must be an object")
    for name, seat in seats.items():
        check_name(name, NAME, "seat name")
        need(isinstance(seat, dict), f"seat {name}: must be an object")
        need(not set(seat) - SEAT_KEYS, f"seat {name}: unknown keys {sorted(set(seat) - SEAT_KEYS)}")
        harness = seat.get("harness")
        need(isinstance(harness, str) and harness in HARNESS_MODULES, f"seat {name}: unknown harness {harness!r}")
        if "model" in seat:
            check_name(seat["model"], MODEL, "model")
        if "effort" in seat:
            check_name(seat["effort"], EFFORT, "effort")
        env_allow = seat.get("env_allow", [])
        need(is_str_list(env_allow), f"seat {name}: env_allow must be a list of names")
        for var in env_allow:
            check_name(var, ENV_NAME, "env_allow name")
        command = seat.get("command")
        need(
            (harness == "local-command") == (command is not None),
            f"seat {name}: command is required for, and only for, local-command",
        )
        need(
            command is None or (is_str_list(command) and command and all(command)),
            f"seat {name}: command must be a non-empty list of strings",
        )
        if "max_budget_usd" in seat:
            module = importlib.import_module(HARNESS_MODULES[harness])
            need(
                getattr(module, "SUPPORTS_BUDGET", False),
                f"seat {name}: max_budget_usd is not supported by harness {harness}; no adapter can honour it there",
            )
            budget = seat["max_budget_usd"]
            need(
                is_number(budget) and budget > 0,
                f"seat {name}: max_budget_usd must be a finite positive number",
            )
    mapping = config.get("roles", {})
    need(isinstance(mapping, dict), "roles must be an object")
    for role, order in mapping.items():
        need(isinstance(role, str) and role in roles, f"config names unknown role {role!r}")
        need(
            is_str_list(order) and all(s in seats for s in order),
            f"role {role}: seat list must name defined seats",
        )
    permit = config.get("permit", [])
    need(is_str_list(permit) and all(r in roles for r in permit), "permit must list known roles")
    for key in ("allow_write", "allow_spend"):
        need(isinstance(config.get(key, False), bool), f"{key} must be a boolean")
    timeout = config.get("timeout_seconds", 600)
    need(is_number(timeout) and 0 < timeout <= 86400, "timeout_seconds must be a number in (0, 86400]")
    if "worktree_root" in config:
        root = config["worktree_root"]
        need(isinstance(root, str) and os.path.isabs(root), "worktree_root must be an absolute path")
    config.setdefault("seats", {})
    config.setdefault("roles", {})
    config.setdefault("permit", [])
    config.setdefault("timeout_seconds", 600)
    return config


def role_access(tools):
    """A role holding any of Bash, Write, Edit can execute or change files: it counts as write."""
    return "write" if WRITE_TOOLS & set(tools) else "read"


def resolve_role(roles, role, intent):
    if role:
        check_name(role, NAME, "role")
        need(role in roles, f"unknown role {role!r}")
        return role
    words = set(re.findall(r"[a-z0-9-]+", (intent or "").lower()))
    matches = sorted(n for n, spec in roles.items() if words & set(spec["intent"]))
    if len(matches) != 1:
        kind = "ambiguous" if matches else "no role matches"
        raise Refusal(f"{kind} intent; candidates: {matches}")
    return matches[0]


def load_role_file(role, spec):
    """Read the role's prompt and tools from agents/ in this tree; refuse when absent or disagreeing."""
    for path in (ROOT / "agents" / f"{role}.md", ROOT / "agents" / role / f"{role}.md"):
        if path.is_file():
            break
    else:
        raise Refusal(f"role {role} unavailable: agents/{role}.md not found")
    try:
        text = path.read_text(encoding="utf-8")
        header, body = text[4:].split("\n---\n", 1) if text.startswith("---\n") else (None, None)
        fields = dict(line.split(": ", 1) for line in header.splitlines() if ": " in line)
        tools = [t.strip() for t in fields["tools"].split(",")]
        name, description = fields["name"], fields["description"]
    except (OSError, ValueError, AttributeError, KeyError) as error:
        raise Refusal(f"role {role} unavailable: agent file unreadable ({error.__class__.__name__})")
    if name != role or tools != spec["tools"]:
        raise Refusal(f"role {role} unavailable: agent file disagrees with roles.json")
    return {"description": description, "prompt": body.strip()}


def find_exe(name):
    """Absolute path of an executable. Relative PATH entries and relative paths are ignored.

    Not `shutil.which`: that one honours relative PATH entries (including ""), which would let a
    worker-writable or cwd-relative directory shadow a trusted binary.
    """
    if os.sep in name:
        candidates = [name] if os.path.isabs(name) else []
    else:
        candidates = [
            os.path.join(d, name) for d in os.environ.get("PATH", os.defpath).split(os.pathsep) if os.path.isabs(d)
        ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def seat_executable(seat):
    command = seat.get("command")
    if command:
        return command[0]
    return importlib.import_module(HARNESS_MODULES[seat["harness"]]).EXECUTABLE


def seat_is_paid(seat):
    if PAID[seat["harness"]]:
        return True
    command = seat_executable(seat)
    names = {os.path.basename(command)}
    exe = find_exe(command)
    if exe:
        names.add(os.path.basename(os.path.realpath(exe)))
    return bool(names & PAID_EXECUTABLES)


def spend_miss(seat, config, cli_spend):
    if not seat_is_paid(seat):
        return None
    if not config.get("allow_spend", False):
        return "spend not permitted by config"
    if not cli_spend:
        return "spend not permitted: --allow-spend not given"
    return None


def seat_request(role, spec, info, seat):
    return {
        "role": role, "tools": spec["tools"], "prompt": info["prompt"], "description": info["description"],
        "model": seat.get("model"), "effort": seat.get("effort"), "command": seat.get("command"),
        "max_budget_usd": seat.get("max_budget_usd"), "agent_file": PLACEHOLDER,
    }


def seat_state(request, seat):
    """Return (module, argv, None) when launchable, else (None, None, reason). Runs nothing."""
    harness = seat["harness"]
    if PAID[harness] and not seat.get("model"):
        return None, None, "missing model"
    module = importlib.import_module(HARNESS_MODULES[harness])
    try:
        argv = module.argv(request)
        module.env(request)
    except ValueError as error:
        return None, None, f"unsupported: {error}"
    exe = find_exe(argv[0])
    if exe is None:
        return None, None, f"executable not found: {argv[0]}"
    return module, [exe, *argv[1:]], None


def decide(args, roles):
    holder = {}
    try:
        return _decide(args, roles, holder)
    except Refusal as refusal:
        refusal.role = refusal.role or holder.get("role")
        raise


def _decide(args, roles, holder):
    config = load_config(args.config, roles)
    role = holder["role"] = resolve_role(roles, args.role, args.intent)
    need(role in config["permit"], f"role {role} is not permitted by config")
    spec = roles[role]
    info = load_role_file(role, spec)
    order = config["roles"].get(role, [])
    need(order, f"no seats configured for role {role}")
    seats = config["seats"]
    pin = args.model
    if pin is not None:
        check_name(pin, MODEL, "model")
    skipped, chosen, launch_plan = [], None, None
    if pin is not None:
        candidates = [n for n in order if seats[n].get("model") == pin]
        skipped += [{"seat": n, "reason": "model pin mismatch"} for n in order if n not in candidates]
        if not candidates:
            raise Refusal(f"pinned model {pin!r} is not available on any configured seat", skipped)
    else:
        candidates = order
    for name in candidates:
        seat = seats[name]
        request = seat_request(role, spec, info, seat)
        module, argv, reason = seat_state(request, seat)
        if argv is None:
            skipped.append({"seat": name, "reason": reason})
            continue
        miss = spend_miss(seat, config, args.allow_spend)
        if miss:
            skipped.append({"seat": name, "reason": miss})
            raise Refusal(f"{miss}; refusing instead of falling through to another seat", skipped)
        chosen, launch_plan = name, (module, argv, request)
        break
    if chosen is None:
        if pin is not None:
            raise Refusal(f"pinned model {pin!r} unavailable on every matching seat; no fallback with a pin", skipped)
        raise Refusal("no configured seat is available", skipped)
    module, argv, request = launch_plan
    seat = seats[chosen]
    write = role_access(spec["tools"]) == "write"
    if write and not config.get("allow_write", False):
        raise Refusal(f"role {role} can execute or change files and config allow_write is false", skipped)
    if write and not config.get("worktree_root"):
        raise Refusal("write role needs an explicit worktree_root in config", skipped)
    return {
        "role": role, "seat": chosen, "model": seat.get("model"), "harness": seat["harness"], "argv": argv,
        "skipped": skipped, "write": write, "config": config, "seat_cfg": seat, "module": module,
        "request": request,
    }


def make_result(event_id, outcome, evidence):
    record = {"event_id": event_id, "outcome": outcome, "evidence": evidence, "finished_at": now()}
    check_record("result", record, result_schema())
    return record


def refusal_envelope(reason, skipped, role=None, job_id=None):
    dispatch = {
        "job_id": job_id, "role": role, "seat": None, "model": None, "harness": None, "skipped": skipped, "exit_code": None,
        "timed_out": False, "worktree": None, "stdout": None, "refused": reason,
    }
    return {"result": make_result(uuid.uuid4().hex, "unsupported", []), "dispatch": dispatch}


def absolute_path_only(env):
    """PATH keeps absolute entries only; with none left it becomes SAFE_PATH, never an empty PATH."""
    if "PATH" in env:
        kept = [d for d in env["PATH"].split(os.pathsep) if os.path.isabs(d)]
        env["PATH"] = os.pathsep.join(kept) or SAFE_PATH
    return env


def base_env():
    """PATH (absolute entries only), HOME, LANG, TMPDIR: nothing relative ever reaches a child."""
    return absolute_path_only({k: os.environ[k] for k in BASE_ENV if k in os.environ})


def worker_env(seat, extra=None):
    env = base_env()
    for var in seat.get("env_allow", []):
        if var in os.environ:
            env[var] = os.environ[var]
    env.update(extra or {})
    return absolute_path_only(env)  # after env_allow: PATH in env_allow must not undo the rule


def write_private(path, text):
    """Atomic 0600 write."""
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def git(workdir, *argv):
    exe = find_exe("git")
    if exe is None:
        raise Refusal("git unavailable: not found on an absolute PATH entry")
    try:
        return subprocess.run(
            [exe, "-C", str(workdir), "-c", "core.hooksPath=/dev/null", *argv],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=base_env(), timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise Refusal(f"git unavailable: {error.__class__.__name__}")


def plan_worktree(config, role, job_id):
    """Where the worktree will be. Touches nothing, so start.json can name it before it exists."""
    return {"path": str(Path(config["worktree_root"]).resolve() / job_id), "branch": f"dispatch/{role}-{job_id}"}


def make_worktree(workdir, config, worktree):
    top = git(workdir, "rev-parse", "--show-toplevel")
    need(top.returncode == 0, "write role needs a git repository at --workdir")
    root = Path(config["worktree_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_owned_dir(root, "worktree_root")
    added = git(workdir, "worktree", "add", "-b", worktree["branch"], worktree["path"], "HEAD")
    need(added.returncode == 0, f"git worktree add failed: {added.stderr.strip()[:200]}")


def remove_worktree(workdir, worktree):
    try:
        git(workdir, "worktree", "remove", "--force", worktree["path"])
        git(workdir, "branch", "-D", worktree["branch"])
    except Refusal:
        pass
    shutil.rmtree(worktree["path"], ignore_errors=True)


def kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def proc_start_ticks(pid):
    """Field 22 of /proc/<pid>/stat (start time in clock ticks); None where unavailable."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def job_marker(job_dir):
    """Discovery hint for everything a job's worker started: sha256 of the real job directory path.

    Not stored anywhere and not a credential: a later recovery step recomputes it from its own run
    dir and job id. It lets a sweep of /proc/*/environ find survivors that left the process group.
    """
    return hashlib.sha256(os.path.realpath(job_dir).encode("utf-8", "surrogateescape")).hexdigest()


def proc_state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def proc_euid(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[2])
    except (OSError, ValueError, IndexError):
        pass
    return None


def kill_pid(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def get_subreaper():
    """prctl(PR_GET_CHILD_SUBREAPER): 0 or 1, None where unavailable; Linux only."""
    if sys.platform != "linux":
        return None
    try:
        flag = ctypes.c_int(-1)
        libc = ctypes.CDLL(None, use_errno=True)
        return int(flag.value) if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) == 0 else None
    except (OSError, AttributeError):
        return None


def set_subreaper(on):
    """prctl(PR_SET_CHILD_SUBREAPER). True when it took effect; Linux only."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1 if on else 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def children_pids():
    """Pids of every direct child of this process, from all threads' /proc/self/task/*/children."""
    pids = set()
    try:
        tasks = os.listdir("/proc/self/task")
    except OSError:
        return pids
    for task in tasks:
        try:
            pids.update(int(p) for p in Path(f"/proc/self/task/{task}/children").read_text().split())
        except (OSError, ValueError):
            continue
    return pids


def arm_subreaper():
    """Called by `run` (never a library caller, never a non-main thread) right before Popen."""
    if threading.current_thread() is not threading.main_thread():
        return False
    if not Path(f"/proc/self/task/{threading.get_native_id()}/children").exists():
        return False  # kernel without CONFIG_PROC_CHILDREN: the child list is unavailable
    if children_pids():
        return False  # the caller's own children would be killed and reaped by cleanup: use the marker sweep only
    return set_subreaper(True)


def reap_descendants(skip):
    """Kill and reap, by exact pid, every child of this process until none is left or the bound hits.

    Returns the set of pids killed while alive. Never waitpid(-1) and never `skip` (the harness's own
    Popen child, which proc.wait owns).
    """
    killed, end = set(), time.monotonic() + CLEANUP_SECONDS
    for _ in range(CLEANUP_ROUNDS):
        pids = children_pids() - skip
        if not pids or time.monotonic() >= end:
            break
        for pid in pids:
            if proc_state(pid) not in (None, "Z"):
                killed.add(pid)
            kill_pid(pid)
        for pid in pids:
            while True:
                try:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                if time.monotonic() >= end:
                    break
                time.sleep(0.005)
    return killed


def sweep_marker(marker):
    """SIGKILL every process whose environ holds the exact marker entry; count unreadable same-user entries.

    Start ticks are read before and after the environ read so a recycled pid is never hit.
    """
    needle = f"{MARKER_VAR}={marker}".encode()
    killed, unknown, me = set(), 0, os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        pid = int(entry)
        try:
            if proc_euid(pid) != os.geteuid():
                continue  # not ours (status stays readable for a non-dumpable process, unlike stat of /proc/<pid>)
            before = proc_start_ticks(pid)
            with open(f"/proc/{pid}/environ", "rb") as handle:
                data = handle.read()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            unknown += 1
            continue
        if needle in data.split(b"\0") and before is not None and before == proc_start_ticks(pid):
            kill_pid(pid)
            killed.add(pid)
    return killed, unknown


def contain(proc, exit_code, marker, armed):
    """Group kill, then (armed) child list, then marker sweep, then (armed) child list again.

    Returns the `dispatch.containment` record. `survivors` is an int only when the child list ran.
    """
    if proc.returncode is None:  # a reaped worker's group id may be reused: `supervise` already killed it unreaped
        kill_group(proc.pid)
    if sys.platform != "linux" or not os.path.isdir("/proc"):
        return {"method": "group", "killed": 0, "survivors": None, "unknown": 0}
    skip = {proc.pid} if exit_code is None else set()
    killed = reap_descendants(skip) if armed else set()
    swept, unknown = sweep_marker(marker)
    killed |= swept
    survivors = None
    if armed:
        killed |= reap_descendants(skip)
        survivors = len(children_pids())
    return {"method": "children+marker" if armed else "marker", "killed": len(killed),
            "survivors": survivors, "unknown": unknown}


def poll_worker(proc):
    """Wait up to 0.05 s; the exit code once the worker has exited, else None.

    The group is killed while the exited worker is still an unreaped zombie: its pid and process
    group id cannot be reused until `proc.wait()` reaps it, so the kill can never hit a stranger.
    Without `waitid(WNOWAIT)` the group kill after the exit is skipped (sweep and child list still run).
    """
    if not HOLD_ZOMBIE:
        try:
            return proc.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            return None
    try:
        pending = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
    except ChildProcessError:  # SIGCHLD inherited as SIG_IGN: the kernel already reaped it, so no safe group kill
        return proc.wait()
    if pending:
        time.sleep(0.05)
        return None
    kill_group(proc.pid)
    return proc.wait()


def harness_version(exe, stop=None):
    """First line of `<exe> --version`, capped; None on any failure. Called under `run` only.

    Polls so a signal (the stop event) ends a hung probe at once instead of after the timeout.
    """
    stop = stop or threading.Event()
    try:
        proc = subprocess.Popen(
            [exe, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={k: v for k, v in base_env().items() if k == "PATH"}, start_new_session=True,
        )
    except OSError:
        return None
    data, code, eof = b"", None, False
    end = time.monotonic() + VERSION_TIMEOUT_SECONDS
    try:
        while code is None and not stop.is_set() and time.monotonic() < end:
            if eof:
                try:
                    code = proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            elif select.select([proc.stdout], [], [], 0.05)[0]:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if chunk:
                    data += chunk[: max(0, 65536 - len(data))]
                else:
                    eof = True
    except (OSError, ValueError):
        code = None
    finally:
        kill_group(proc.pid)
        proc.wait()
        proc.stdout.close()
    if code != 0:
        return None
    lines = data.decode("utf-8", "replace").strip().splitlines()
    return lines[0][:100] if lines else None


class Capture:
    """Everything a pump read, up to the cap: a 0600 copy on disk, the bytes and a sha256 in memory.

    The verdict and the recorded hash come from memory, never from the file (a same-uid worker can
    rewrite the file). The copy is written unbuffered so a dispatcher crash loses nothing already read.
    """

    def __init__(self, path):
        self.file = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb", buffering=0)
        self.buf, self.sha, self.lock, self.frozen = bytearray(), hashlib.sha256(), threading.Lock(), False

    def add(self, data):
        """Returns "ok", "full" (cap hit; the rest is dropped) or "frozen"."""
        with self.lock:
            if self.frozen:
                return "frozen"
            room = MAX_OUTPUT_BYTES - len(self.buf)
            chunk = data[:room]
            if chunk:
                self.buf += chunk
                self.sha.update(chunk)
                try:
                    view = memoryview(chunk)
                    while view:
                        view = view[self.file.write(view):]
                except (OSError, ValueError):
                    pass  # the on-disk copy is best effort; the capture in memory is the record
            return "full" if len(data) > room else "ok"

    def freeze(self):
        with self.lock:
            self.frozen = True
            return bytes(self.buf), self.sha.hexdigest()

    def close(self):
        try:
            self.file.close()
        except OSError:
            pass


def pump(stream, capture, overflow):
    while True:
        try:
            data = os.read(stream.fileno(), 65536)
        except (OSError, ValueError):
            break
        if not data:
            break
        status = capture.add(data)
        if status == "full":
            overflow.set()
        if status != "ok":
            break


def parse_lines(text):
    """JSON values of the lines that parse. Any failure, RecursionError included, skips the line."""
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:  # noqa: BLE001 - untrusted bytes: nothing here may raise
            continue


def add_usage(bucket, key, value):
    """Add a finite number >= 0; anything else, or a sum that stops being finite, is ignored."""
    if usable(value) and is_number(bucket.get(key, 0) + value):
        bucket[key] = bucket.get(key, 0) + value


def opencode_state(out_bytes, err_bytes):
    """Success needs a positive signal: the last event is a step_finish with reason "stop"."""
    out_text, err_text = out_bytes.decode("utf-8", "replace"), err_bytes.decode("utf-8", "replace")
    fell_back = any(m in err_text for m in FALLBACK_MARKERS)  # stderr only; stdout is model text
    errored, denials, last = False, 0, None
    usage = {}
    for event in parse_lines(out_text):
        last = event
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "error":
            errored = True
        elif event.get("type") == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            error = state.get("error")
            if state.get("status") == "error" and isinstance(error, str) and error.startswith(DENIAL_PREFIXES):
                denials += 1
        elif event.get("type") == "step_finish":
            add_usage(usage, "cost_usd", part.get("cost"))
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            add_usage(usage, "input", tokens.get("input"))
            add_usage(usage, "output", tokens.get("output"))
            add_usage(usage, "cache_read", cache.get("read"))
            add_usage(usage, "cache_write", cache.get("write"))
    finished = (
        isinstance(last, dict) and last.get("type") == "step_finish"
        and isinstance(last.get("part"), dict) and last["part"].get("reason") == "stop"
    )
    note = None
    if fell_back:
        note = "harness fell back or auto-rejected a permission request"
    elif errored:
        note = "harness reported an error event"
    elif denials:
        note = "harness denied a tool request"
    elif not finished:
        note = "harness did not finish cleanly (last event is not a step_finish with reason stop)"
    return note, usage or None, denials


def claude_state(out_bytes):
    try:
        data = json.loads(out_bytes.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - untrusted bytes: RecursionError and OverflowError included
        data = None
    if not isinstance(data, dict):
        return "harness result unreadable", None, None
    usage = {}
    raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    for key, source in (("input", "input_tokens"), ("output", "output_tokens"),
                        ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
        add_usage(usage, key, raw.get(source))
    add_usage(usage, "cost_usd", data.get("total_cost_usd"))
    denials = data.get("permission_denials", [])
    count = len(denials) if isinstance(denials, list) else None
    subtype = repr(data.get("subtype"))[:60]
    note = None
    if data.get("is_error"):
        note = f"harness reported an error (subtype {subtype})"
    elif data.get("type") != "result" or data.get("subtype") != "success" or data.get("is_error") is not False:
        note = f"harness did not report success (type {repr(data.get('type'))[:30]}, subtype {subtype})"
    if count is None:
        note = "harness permission_denials is not a list"
    elif count:
        note = "harness denied a tool request"
    return note, usage or None, count


def harness_state(harness, out_bytes, err_bytes):
    """Return (failure_note_or_None, usage_or_None, permission_denials_or_None). Only ever lowers.

    The prefixes and reasons match OpenCode 1.18.31 and Claude Code 2.1.314; a harness that changes them
    fails closed (no positive signal, no success).
    """
    if harness == "opencode":
        return opencode_state(out_bytes, err_bytes)
    if harness == "claude-code":
        return claude_state(out_bytes)
    return None, None, None


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_owned_dir(path, what):
    info = os.stat(path)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refusal(f"{what} must be owned by the effective user and not group- or other-writable")


def check_run_dir(base):
    check_owned_dir(base, "run dir")


def launch(decision, prompt, run_dir, workdir, job_id=None, subreaper=False):
    """One worker, supervised. `subreaper=True` is for `run` only: the process then owns its descendants."""
    job_id = job_id or uuid.uuid4().hex
    check_name(job_id, NAME, "job id")
    stop, signals, previous = threading.Event(), [], {}

    def on_signal(number, _frame):
        signals.append(number)
        stop.set()

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # before the version probe
        try:
            previous[number] = signal.signal(number, on_signal)
        except ValueError:  # not the main thread
            pass
    try:
        base = Path(run_dir).resolve()
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_run_dir(base)
        job_dir = (base / job_id).resolve()
        job_dir.relative_to(base)
        try:
            job_dir.mkdir(mode=0o700)
        except FileExistsError:
            raise ReuseRefusal(f"job id {job_id!r} already exists")
        os.chmod(job_dir, 0o700)
        return supervise(decision, prompt, job_dir, job_id, workdir, stop, signals, subreaper)
    except ReuseRefusal:
        raise
    except Refusal as refusal:
        refusal.job_id = refusal.job_id or job_id
        raise
    finally:  # restored only after supervise has written the result
        for number, handler in previous.items():
            signal.signal(number, handler)


def supervise(decision, prompt, job_dir, job_id, workdir, stop, signals, subreaper=False):
    role, config, harness = decision["role"], decision["config"], decision["harness"]
    prompt_path, stdout_path, stderr_path = job_dir / "prompt.txt", job_dir / "stdout.txt", job_dir / "stderr.txt"
    timeout = config["timeout_seconds"]
    worktree, created, captures, proc, error, armed = None, False, [], None, None, False
    marker = job_marker(job_dir)
    try:  # everything up to Popen: a failure here is a refusal and leaves nothing behind
        request = dict(decision["request"], agent_file=str(job_dir / "agents.json"))
        module = decision["module"]
        argv = [decision["argv"][0], *module.argv(request)[1:]]
        extra_env = {**module.env(request), MARKER_VAR: marker}
        version = harness_version(argv[0], stop) if harness != "local-command" else None
        if stop.is_set():
            raise Refusal(f"interrupted by signal {signals[0]} before launch")
        started_dt = datetime.datetime.now(datetime.timezone.utc)
        started = iso(started_dt)
        worktree = plan_worktree(config, role, job_id) if decision["write"] else None
        cwd = worktree["path"] if worktree else str(workdir)
        start = {
            "job_id": job_id, "role": role, "seat": decision["seat"], "model": decision["model"],
            "harness": harness, "harness_version": version, "argv": argv, "workdir": cwd,
            "worktree": worktree, "started_at": started,
            "deadline": iso(started_dt + datetime.timedelta(seconds=timeout)),
            "dispatcher_pid": os.getpid(), "dispatcher_start_ticks": proc_start_ticks(os.getpid()),
            "pid": None, "pgid": None, "worker_start_ticks": None, "state": "starting",
        }
        write_private(job_dir / "start.json", dump_json(start) + "\n")
        if worktree:
            make_worktree(workdir, config, worktree)
            created = True
        for filename, text in module.harness_files(request).items():
            write_private(job_dir / filename, text)
        write_private(prompt_path, prompt)
        captures = [Capture(stdout_path), Capture(stderr_path)]
        if stop.is_set():
            raise Refusal(f"interrupted by signal {signals[0]} before launch")
        stdin = open(prompt_path, "rb")
        try:
            armed = arm_subreaper() if subreaper else False  # after every pre-launch helper: none is adopted
            proc = subprocess.Popen(
                argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, shell=False,
                env=worker_env(decision["seat_cfg"], extra_env), start_new_session=True,
            )
        except OSError as exc:
            error = f"launch failed: {exc.__class__.__name__}: {exc}"
            if created:
                remove_worktree(workdir, worktree)
                created, worktree = False, None
        finally:
            stdin.close()
    except BaseException as exc:
        if proc is None:
            for capture in captures:
                capture.close()
            if created:
                remove_worktree(workdir, worktree)
            shutil.rmtree(job_dir, ignore_errors=True)
            if isinstance(exc, Exception) and not isinstance(exc, Refusal):
                raise Refusal(f"launch preparation failed: {exc.__class__.__name__}: {exc}") from exc
            raise
        error = f"dispatcher failure after launch: {exc.__class__.__name__}"
        kill_group(proc.pid)

    # From here the worker exists (or Popen reported an exec error): the record says launched, whatever happens.
    exit_code, timed_out, pumps, overflow = None, False, [], threading.Event()
    if proc is not None:
        try:
            start.update(pid=proc.pid, pgid=proc.pid, worker_start_ticks=proc_start_ticks(proc.pid), state="running")
            write_private(job_dir / "start.json", dump_json(start) + "\n")
            pumps = [
                threading.Thread(target=pump, args=(s, c, overflow), daemon=True)
                for s, c in ((proc.stdout, captures[0]), (proc.stderr, captures[1]))
            ]
            for thread in pumps:
                thread.start()
            deadline = time.monotonic() + timeout
            while error is None:
                exit_code = poll_worker(proc)
                if exit_code is not None:
                    break
                if stop.is_set():
                    error = f"interrupted by signal {signals[0]}"
                elif time.monotonic() >= deadline:
                    timed_out = True
                elif overflow.is_set():
                    error = "output size cap exceeded"
                else:
                    continue
                break
        except BaseException as exc:  # noqa: BLE001 - once launched, nothing may escape as a refusal
            error = error or f"dispatcher failure after launch: {exc.__class__.__name__}"
        if exit_code is None:
            kill_group(proc.pid)
            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # All killing happens here, before the captured output is frozen and judged.
        try:
            containment = contain(proc, exit_code, marker, armed)
        except Exception:  # noqa: BLE001 - a containment failure must not turn a launched job into a refusal
            containment = {"method": "marker" if sys.platform == "linux" else "group", "killed": 0,
                           "survivors": None, "unknown": 0}
            error = error or "containment failed"
        end = time.monotonic() + PUMP_GRACE_SECONDS
        for thread in pumps:
            thread.join(max(0.0, end - time.monotonic()))
        held = any(thread.is_alive() for thread in pumps)
        for index, stream in enumerate((proc.stdout, proc.stderr)):
            if index >= len(pumps) or not pumps[index].is_alive():
                try:
                    stream.close()
                except OSError:
                    pass
        if overflow.is_set() and error is None:
            error = "output size cap exceeded"
        if held and error is None:
            error = "worker left a process holding the output pipe"
        if stop.is_set() and error is None and exit_code != 0:
            error = f"interrupted by signal {signals[0]}"
    else:
        containment = {"method": "children+marker" if armed else "marker", "killed": 0,
                       "survivors": 0 if armed else None, "unknown": 0}
    out_bytes, out_sha = captures[0].freeze()
    err_bytes, _ = captures[1].freeze()
    for capture in captures:
        capture.close()

    note, usage, denials = None, None, None
    if proc is not None:
        try:
            note, usage, denials = harness_state(harness, out_bytes, err_bytes)
        except Exception:  # noqa: BLE001 - fail closed
            note = "harness state check failed"
    survivors = containment["survivors"]
    if timed_out and error is None:
        error = f"job timed out after {timeout:g}s"
    if survivors:
        error = error or f"{survivors} descendant process(es) survived containment"
    ok = error is None and not timed_out and exit_code == 0 and note is None
    dispatch = {
        "job_id": job_id, "role": role, "seat": decision["seat"], "model": decision["model"],
        "harness": harness, "harness_version": version, "argv": argv, "pid": proc.pid if proc else None,
        "started_at": started, "skipped": decision["skipped"], "exit_code": exit_code, "timed_out": timed_out,
        "worktree": worktree, "stdout": None, "stderr": str(stderr_path), "note": error or note,
        "permission_denials": denials, "usage": usage, "containment": containment,
        "confinement": CONFINEMENT_WRITE if decision["write"] else CONFINEMENT_READ,
    }
    evidence = []
    if proc is not None:
        dispatch["stdout"] = {"path": str(stdout_path), "sha256": out_sha}
        evidence = [{"kind": "stdout", "reference": str(stdout_path), "sha256": out_sha}]
    envelope = {"result": make_result(job_id, "succeeded" if ok else "failed", evidence), "dispatch": dispatch}
    try:
        text = dump_json(envelope)
    except Exception:  # noqa: BLE001 - the worker may have shaped something unserialisable
        envelope, ok = minimal_envelope(job_id), False
        text = dump_json(envelope)
    try:
        write_private(job_dir / "result.json", text + "\n")
    except Exception as exc:  # noqa: BLE001 - the envelope still reaches stdout
        note_text = f"result.json could not be written: {exc.__class__.__name__}"
        target = envelope["dispatch"] if "dispatch" in envelope else envelope
        target["note"] = f"{target['note']}; {note_text}" if target.get("note") else note_text
    else:
        start["state"] = "finished"
        try:
            write_private(job_dir / "start.json", dump_json(start) + "\n")
        except Exception:  # noqa: BLE001 - advisory only; result.json is authoritative
            pass
    return envelope, ok


def minimal_envelope(job_id):
    """What stands in when the full envelope cannot be serialised: still a failed, launched job."""
    return {
        "result": make_result(job_id, "failed", []), "job_id": job_id, "outcome": "failed", "exit": EXIT_FAILED,
        "note": "envelope could not be serialised",
    }


def emit(obj):
    print(dump_json(obj))


def public_decision(decision):
    plan = {k: decision[k] for k in ("role", "seat", "model", "harness", "skipped", "write")}
    plan["argv"] = [decision["argv"][0], *decision["module"].argv(decision["request"])[1:]]
    return plan


def emit_refusal(refusal, extra=None):
    envelope = refusal_envelope(refusal.reason, refusal.skipped, refusal.role, refusal.job_id)
    envelope.update(extra or {})
    emit(envelope)
    return EXIT_REFUSED


def guarded(function):
    def wrapper(args, roles):
        try:
            return function(args, roles)
        except Refusal as refusal:
            return emit_refusal(refusal, {"plan": None} if args.command == "plan" else None)
        except Exception as exc:  # shape surprises become a typed refusal, never a traceback
            return emit_refusal(Refusal(f"dispatch failure: {exc.__class__.__name__}: {exc}"),
                                {"plan": None} if args.command == "plan" else None)
    return wrapper


@guarded
def cmd_plan(args, roles):
    emit({"plan": public_decision(decide(args, roles))})
    return 0


@guarded
def prepare_and_launch(args, roles):
    """Everything up to and including the launch. A refusal becomes an exit code; a launched job an envelope.

    A refusal raised before `launch()` carries the caller's `--job-id` when it is a valid id; `launch()`
    attaches the id itself, except for a reuse refusal and an invalid id, which stay null.
    """
    launching = False
    try:
        need(args.run_dir, "run needs --run-dir")
        decision = decide(args, roles)
        try:
            try:
                prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else (args.intent or "")
            except (OSError, ValueError) as error:
                raise Refusal(f"prompt file unreadable: {error.__class__.__name__}")
            need(prompt.strip(), "empty prompt")
            prior = get_subreaper()
            launching = True
            try:
                return launch(decision, prompt, args.run_dir, Path(args.workdir).resolve(), args.job_id, subreaper=True)
            finally:
                # An in-process caller of main() keeps its own flag; where it cannot be read, the flag is left off.
                set_subreaper(bool(prior))
        except Refusal as refusal:
            refusal.role, refusal.skipped = refusal.role or decision["role"], refusal.skipped or decision["skipped"]
            raise
    except Refusal as refusal:
        if not launching and isinstance(args.job_id, str) and NAME.fullmatch(args.job_id):
            refusal.job_id = refusal.job_id or args.job_id
        raise


def cmd_run(args, roles):
    """The final emit is outside `guarded`: worker-controlled shapes cannot turn a launched job into `unsupported`."""
    result = prepare_and_launch(args, roles)
    if isinstance(result, int):
        return result
    envelope, ok = result
    emit(envelope)  # already serialised once in `supervise`, which swapped in the stand-in if that failed
    return 0 if ok else EXIT_FAILED


def role_report(config, roles):
    report = {}
    for role in sorted(set(config["permit"]) | set(config["roles"])):
        try:
            load_role_file(role, roles[role])
            report[role] = {"permitted": role in config["permit"], "role_available": True, "reason": None}
        except Refusal as refusal:
            report[role] = {"permitted": role in config["permit"], "role_available": False, "reason": refusal.reason}
    return report


@guarded
def cmd_status(args, roles):
    config = load_config(args.config, roles)
    seats = {}
    for name, seat in config["seats"].items():
        paid = seat_is_paid(seat)
        exe = find_exe(seat_executable(seat))
        missing_model = PAID[seat["harness"]] and not seat.get("model")
        seats[name] = {
            "harness": seat["harness"], "model": seat.get("model"), "paid": paid,
            "executable_found": exe is not None,
            "spend_permitted": (not paid) or (config.get("allow_spend", False) and args.allow_spend),
            "available": exe is not None and not missing_model,
            "reason": "missing model" if missing_model else (None if exe else f"executable not found: {seat_executable(seat)}"),
        }
    emit({"seats": seats, "permit": config["permit"], "roles": role_report(config, roles)})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("plan", "run", "status"))
    parser.add_argument("--config")
    parser.add_argument("--role")
    parser.add_argument("--intent")
    parser.add_argument("--model")
    parser.add_argument("--prompt-file")
    parser.add_argument("--run-dir")
    parser.add_argument("--job-id")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--allow-spend", action="store_true")
    args = parser.parse_args(argv)
    roles = load_roles()
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status}[args.command](args, roles)


if __name__ == "__main__":
    sys.exit(main())
