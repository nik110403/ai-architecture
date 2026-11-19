#!/usr/bin/env python3
"""Optional resource-control adapter: bounded memory, CPU and process count with recoverable cancellation (stdlib only).

  resources.py probe  [--systemctl P]
  resources.py run    --backend systemd-scope|rlimit [--limits FILE] [--memory-max-mb N] [--cpu-quota PERCENT] [--tasks-max N]
                      [--address-space-mb N] [--cpu-seconds N] [--timeout S] [--unit-file F] [--evidence-file F]
                      [--systemd-run P] [--systemctl P] -- CMD [ARG...]
  resources.py stop   --unit aiarch-<hex>.scope [--systemctl P]
  resources.py verdict --evidence FILE

Backends. `systemd-scope`: `systemd-run --user --scope` with MemoryMax, MemorySwapMax=0, CPUQuota, TasksMax (Linux, cgroup v2, a
user manager that delegates cpu memory pids). `rlimit`: setrlimit RLIMIT_AS / RLIMIT_CPU in the child; declared partial - there
is no per-tree task bound (RLIMIT_NPROC is per user) and no CPU share. Exit: the command's status; 124 timeout; 125 killed by a
limit (evidence says which); 2 refused (including a run with no limit at all); 3 backend unavailable - the command never runs,
there is never an unbounded fallback. Nothing here starts a scheduler entry or leaves a unit behind: a scope ends with its
command, and `stop` ends a running one. Telemetry (modules/observability `observe.py resources`) is read-only /proc data and
is not a bound: `verdict` says `not-bounded` for it.
"""

import argparse
import json
import os
import re
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

EVIDENCE = "resource-evidence/1"
UNIT = re.compile(r"aiarch-[0-9a-f]{8,32}\.scope\Z")
CGROOT = Path("/sys/fs/cgroup")
NEED = ("cpu", "memory", "pids")


def emit(code, reason, **kw):
    print(json.dumps(dict(kw, reason=reason), sort_keys=True, allow_nan=False))
    if code:
        print(f"resources: {reason}: {kw.get('message', '')}", file=sys.stderr)
    return code


def tool(explicit, name):
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    return shutil.which(name)


def user_env_ok(systemctl):
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not rt or not os.path.isdir(rt):
        return "XDG_RUNTIME_DIR is not set or missing (no user manager to talk to)"
    try:
        r = subprocess.run([systemctl, "--user", "show-environment"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "the user manager did not answer"
    return None if r.returncode == 0 else "the user manager did not answer"


def delegated():
    uid = os.getuid()
    f = CGROOT / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "cgroup.controllers"
    try:
        have = set(f.read_text().split())
    except OSError:
        return "cgroup v2 user delegation not readable"
    missing = [c for c in NEED if c not in have]
    return f"controllers not delegated: {' '.join(missing)}" if missing else None


def probe_scope(systemd_run, systemctl):
    if not sys.platform.startswith("linux"):
        return f"unavailable: platform {sys.platform!r} is not supported"
    if not systemd_run:
        return "unavailable: systemd-run not found"
    if not systemctl:
        return "unavailable: systemctl not found"
    why = user_env_ok(systemctl) or delegated()
    return f"unavailable: {why}" if why else "available"


def probe_rlimit():
    return "available" if sys.platform.startswith("linux") and hasattr(resource, "RLIMIT_AS") else "unavailable: setrlimit not available"


# --- limits -------------------------------------------------------------------------------------------------------------------

def load_limits(a):
    lim = {}
    if a.limits:
        try:
            doc = json.loads(Path(a.limits).read_text())
        except (OSError, ValueError):
            raise SystemExit(emit(2, "limits-invalid", message="--limits FILE unreadable"))
        lim.update({k: doc[k] for k in ("memory_max_mb", "cpu_quota", "tasks_max", "address_space_mb", "cpu_seconds", "timeout_seconds",
                                        "systemd_run", "systemctl", "backend") if k in doc})
    for k, v in (("memory_max_mb", a.memory_max_mb), ("cpu_quota", a.cpu_quota), ("tasks_max", a.tasks_max),
                 ("address_space_mb", a.address_space_mb), ("cpu_seconds", a.cpu_seconds), ("timeout_seconds", a.timeout),
                 ("systemd_run", a.systemd_run), ("systemctl", a.systemctl), ("backend", a.backend)):
        if v is not None:
            lim[k] = v
    for k in ("memory_max_mb", "cpu_quota", "tasks_max", "address_space_mb", "cpu_seconds", "timeout_seconds"):
        if k in lim:
            try:
                lim[k] = float(lim[k])
            except (TypeError, ValueError):
                raise SystemExit(emit(2, "limits-invalid", message=f"{k} must be a number"))
            if not (lim[k] > 0) or lim[k] == float("inf"):
                raise SystemExit(emit(2, "limits-invalid", message=f"{k} must be a finite number > 0"))
    return lim


def read_int(path):
    try:
        raw = Path(path).read_text().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def read_kv(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, _, v = line.partition(" ")
            out[k] = int(v)
    except (OSError, ValueError):
        pass
    return out


def write_evidence(path, doc):
    if not path:
        return
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, allow_nan=False))
    os.replace(tmp, path)


# --- the scope's inner wrapper: runs inside the cgroup and reads its own counters ----------------------------------------------

def inner(a, cmd):
    lim = json.loads(a.inner_limits)
    cg = None
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            cg = CGROOT / line[3:].lstrip("/")
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=sys.stdin)
    rc = proc.wait()
    wall = time.monotonic() - t0
    cpu_max = (cg / "cpu.max").read_text().split() if cg and (cg / "cpu.max").exists() else None
    events = read_kv(cg / "memory.events") if cg else {}
    applied = {
        "memory_max_bytes": read_int(cg / "memory.max") if cg else None,
        "memory_swap_max_bytes": read_int(cg / "memory.swap.max") if cg else None,
        "tasks_max": read_int(cg / "pids.max") if cg else None,
        "cpu_quota_percent": (round(int(cpu_max[0]) / int(cpu_max[1]) * 100) if cpu_max and cpu_max[0] != "max" else None),
    }
    observed = {"memory_peak_bytes": read_int(cg / "memory.peak") if cg else None, "pids_peak": read_int(cg / "pids.peak") if cg else None,
                "cpu_usage_ns": (read_kv(cg / "cpu.stat").get("usage_usec", 0) * 1000) if cg else None,
                "oom_kill": events.get("oom_kill", 0), "pids_max_hits": read_kv(cg / "pids.events").get("max", 0) if cg else 0,
                "wall_seconds": round(wall, 3)}
    sig = -rc if rc < 0 else None
    killed = observed["oom_kill"] > 0 and (sig == signal.SIGKILL or rc != 0)
    doc = {"schema": EVIDENCE, "backend": "systemd-scope", "unit": a.inner_unit, "limits_requested": lim, "limits_applied": applied,
           "observed": observed, "exit": {"status": rc if rc >= 0 else None, "signal": sig}, "killed_by_limit": killed,
           "reason": "oom-kill" if killed else None, "unbounded": []}
    write_evidence(a.evidence_file, doc)
    return 125 if killed else (rc if rc >= 0 else 128 - rc)


# --- run ----------------------------------------------------------------------------------------------------------------------

def run_scope(a, lim, cmd):
    if not any(k in lim for k in ("memory_max_mb", "cpu_quota", "tasks_max")):
        return emit(2, "no-limit", message="systemd-scope needs at least one of --memory-max-mb, --cpu-quota, --tasks-max")
    systemd_run, systemctl = tool(lim.get("systemd_run"), "systemd-run"), tool(lim.get("systemctl"), "systemctl")
    status = probe_scope(systemd_run, systemctl)
    if status != "available":
        return emit(3, "unavailable", message=status)
    unit = f"aiarch-{secrets.token_hex(6)}.scope"
    if a.unit_file:
        Path(a.unit_file).write_text(unit + "\n")
    props = ["-p", "MemorySwapMax=0", "-p", "CollectMode=inactive-or-failed", "-p", "OOMPolicy=continue"]
    requested = {}
    if "memory_max_mb" in lim:
        n = int(lim["memory_max_mb"] * 1024 * 1024)
        props += ["-p", f"MemoryMax={n}"]
        requested["memory_max_bytes"] = n
    if "cpu_quota" in lim:
        props += ["-p", f"CPUQuota={int(lim['cpu_quota'])}%"]
        requested["cpu_quota_percent"] = int(lim["cpu_quota"])
    if "tasks_max" in lim:
        props += ["-p", f"TasksMax={int(lim['tasks_max'])}"]
        requested["tasks_max"] = int(lim["tasks_max"])
    ev = str(a.evidence_file) if a.evidence_file else None
    argv = [systemd_run, "--user", "--scope", "--quiet", "--unit", unit, *props, "--", sys.executable, "-B", str(Path(__file__).resolve()), "_inner",
            "--inner-limits", json.dumps(requested), "--inner-unit", unit, *(["--evidence-file", ev] if ev else []), "--", *cmd]
    proc = subprocess.Popen(argv)
    try:
        rc = proc.wait(timeout=lim.get("timeout_seconds"))
    except subprocess.TimeoutExpired:
        subprocess.run([systemctl, "--user", "stop", unit], capture_output=True, timeout=60)
        proc.wait()
        write_evidence(ev, {"schema": EVIDENCE, "backend": "systemd-scope", "unit": unit, "limits_requested": requested, "limits_applied": {},
                            "observed": {}, "exit": {"status": None, "signal": None}, "killed_by_limit": False, "reason": "timeout", "unbounded": []})
        return 124
    return rc


def run_rlimit(a, lim, cmd):
    if probe_rlimit() != "available":
        return emit(3, "unavailable", message=probe_rlimit())
    if any(k in lim for k in ("memory_max_mb", "cpu_quota", "tasks_max")):
        return emit(2, "backend-cannot-bound", message="rlimit bounds address space and CPU seconds only; no task or CPU-share bound exists")
    if not any(k in lim for k in ("address_space_mb", "cpu_seconds")):
        return emit(2, "no-limit", message="rlimit needs --address-space-mb or --cpu-seconds")
    applied = {}

    def child_limits():
        if "address_space_mb" in lim:
            n = int(lim["address_space_mb"] * 1024 * 1024)
            resource.setrlimit(resource.RLIMIT_AS, (n, n))
        if "cpu_seconds" in lim:
            n = int(lim["cpu_seconds"])
            resource.setrlimit(resource.RLIMIT_CPU, (n, n + 1))

    if "address_space_mb" in lim:
        applied["address_space_bytes"] = int(lim["address_space_mb"] * 1024 * 1024)
    if "cpu_seconds" in lim:
        applied["cpu_seconds"] = int(lim["cpu_seconds"])
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, preexec_fn=child_limits, start_new_session=True)
    timed_out = False
    try:
        rc = proc.wait(timeout=lim.get("timeout_seconds"))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        rc = proc.wait()
    sig = -rc if rc < 0 else None
    killed = sig == signal.SIGXCPU
    reason = "timeout" if timed_out else ("rlimit-cpu" if killed else None)
    write_evidence(a.evidence_file, {"schema": EVIDENCE, "backend": "rlimit", "unit": None, "limits_requested": dict(applied), "limits_applied": applied,
                                     "observed": {"wall_seconds": round(time.monotonic() - t0, 3)}, "exit": {"status": rc if rc >= 0 else None, "signal": sig},
                                     "killed_by_limit": killed, "reason": reason, "unbounded": ["tasks", "cpu-share"]})
    return 124 if timed_out else (125 if killed else (rc if rc >= 0 else 128 - rc))


def cmd_stop(a):
    if not UNIT.fullmatch(a.unit or ""):
        return emit(2, "unit-invalid", message="unit must look like aiarch-<hex>.scope")
    systemctl = tool(a.systemctl, "systemctl")
    if not systemctl:
        return emit(3, "unavailable", message="systemctl not found")
    subprocess.run([systemctl, "--user", "stop", a.unit], capture_output=True, timeout=120)
    subprocess.run([systemctl, "--user", "reset-failed", a.unit], capture_output=True, timeout=30)
    r = subprocess.run([systemctl, "--user", "is-active", a.unit], capture_output=True, text=True, timeout=30)
    if r.stdout.strip() == "active":
        return emit(1, "still-active", message="the unit is still active")
    return emit(0, "stopped", unit=a.unit)


def verdict(evidence):
    """bounded only when the limits were actually applied: telemetry alone (no limits_applied) is `not-bounded`."""
    if not isinstance(evidence, dict) or evidence.get("schema") != EVIDENCE:
        return "not-bounded", "not a resource-control receipt (telemetry carries no applied limits)"
    applied = evidence.get("limits_applied") or {}
    if not any(v is not None for v in applied.values()):
        return "not-bounded", "no limit was applied"
    for k, v in (evidence.get("limits_requested") or {}).items():
        if k in applied and applied[k] != v:
            return "not-bounded", f"applied {k} differs from the requested value"
    return "bounded", "limits applied and read back"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="resources.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="sub", required=True)
    pr = sub.add_parser("probe")
    pr.add_argument("--systemctl")
    pr.add_argument("--systemd-run")
    r = sub.add_parser("run")
    r.add_argument("--backend", choices=("systemd-scope", "rlimit"))
    for name in ("limits", "memory-max-mb", "cpu-quota", "tasks-max", "address-space-mb", "cpu-seconds", "timeout", "unit-file", "evidence-file",
                 "systemd-run", "systemctl"):
        r.add_argument(f"--{name}")
    s = sub.add_parser("stop")
    s.add_argument("--unit", required=True)
    s.add_argument("--systemctl")
    v = sub.add_parser("verdict")
    v.add_argument("--evidence", required=True)
    i = sub.add_parser("_inner")
    i.add_argument("--inner-limits")
    i.add_argument("--inner-unit")
    i.add_argument("--evidence-file")
    try:
        a = p.parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    if a.sub == "probe":
        sc = tool(a.systemctl, "systemctl")
        sr = tool(a.systemd_run, "systemd-run") if a.systemd_run or not sc else tool(os.path.join(os.path.dirname(sc), "systemd-run"), "systemd-run")
        print(json.dumps({"backends": {"systemd-scope": probe_scope(sr, sc), "rlimit": probe_rlimit()}}, sort_keys=True))
        return 0
    if a.sub == "stop":
        return cmd_stop(a)
    if a.sub == "verdict":
        try:
            doc = json.loads(Path(a.evidence).read_text())
        except (OSError, ValueError):
            return emit(2, "evidence-invalid", message="evidence file unreadable")
        word, why = verdict(doc)
        print(json.dumps({"verdict": word, "why": why, "reason": word}))
        return 0
    if a.sub == "_inner":
        return inner(a, cmd)
    if not cmd:
        return emit(2, "usage", message="give the command after --")
    lim = load_limits(a)
    backend = lim.get("backend")
    if backend is None:
        return emit(2, "usage", message="--backend is required")
    if backend == "systemd-scope":
        return run_scope(a, lim, cmd)
    if backend == "rlimit":
        return run_rlimit(a, lim, cmd)
    return emit(2, "usage", message="unknown backend")


if __name__ == "__main__":
    sys.exit(main())
