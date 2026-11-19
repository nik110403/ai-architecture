#!/usr/bin/env python3
"""Optional Linux sandbox/cleanup adapter around bubblewrap (`bwrap`). Stdlib only; no root, no setuid helper.

  sandbox.py probe [--format text|json]
  sandbox.py run --rw DIR --timeout SECONDS -- CMD [ARG...]

It NEVER falls back to running a command unsandboxed: no Linux, no `bwrap`, or a `bwrap` that cannot
create a sandbox here is reported `unavailable` (exit 3) and the command does not run.
Exit: the command's own status; 124 on timeout (the whole process tree is killed); 2 usage or refused
input; 3 unavailable.

What the sandbox is (and is not) is in README.md: it is process isolation for a command you chose to run,
not a security boundary against hostile code.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

PROBE_TIMEOUT = 10
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
# Top-level directories a $HOME value must never be masked as (they hold the system itself).
_SYSTEM_TOP = {"usr", "bin", "sbin", "lib", "lib32", "lib64", "etc", "proc", "dev", "sys", "run", "tmp", "var", "opt", "boot"}


def _unavailable(reason):
    return f"unavailable: {reason}"


def _home_dirs():
    """Home directories to mask: the passwd entry and $HOME, when they are real, non-system directories."""
    found = []
    try:
        import pwd

        found.append(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        pass
    if os.environ.get("HOME"):
        found.append(os.environ["HOME"])
    out = []
    for h in found:
        if not h or not os.path.isabs(h) or not os.path.isdir(h):
            continue
        real = os.path.realpath(h)
        parts = [p for p in real.split("/") if p]
        if not parts or parts[0] in _SYSTEM_TOP or real in out:
            continue
        out.append(real)
    return out


def base_args(rw=None):
    """bwrap flags shared by probe and run. `/` is read-only; HOME, /tmp and /run are private tmpfs masks."""
    args = [
        "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--tmpfs", "/run",
    ]
    if os.path.isdir("/var/run") and not os.path.islink("/var/run"):
        args += ["--tmpfs", "/var/run"]
    for h in _home_dirs():
        args += ["--tmpfs", h]
    if rw:
        args += ["--bind", rw, rw, "--chdir", rw]  # after the masks, so a DIR under HOME or /tmp stays visible
    args += ["--clearenv", "--setenv", "PATH", SAFE_PATH, "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8"]
    return args


def probe():
    """Returns 'available' or 'unavailable: <reason>'. Really tries to start a sandbox."""
    if not sys.platform.startswith("linux"):
        return _unavailable(f"platform {sys.platform!r} is not supported (Linux with bubblewrap is required)")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return _unavailable("bwrap not found on PATH")
    try:
        r = subprocess.run([bwrap, *base_args(), "--", "true"], capture_output=True, text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return _unavailable(f"bwrap did not start a sandbox within {PROBE_TIMEOUT}s")
    except OSError as exc:
        return _unavailable(f"bwrap could not be run: {type(exc).__name__}")
    if r.returncode != 0:
        why = (r.stderr.strip().splitlines() or ["no output"])[0][:200]
        return _unavailable(f"bwrap cannot create a sandbox here ({why}); user namespaces may be disabled or restricted")
    return "available"


def check_rw(path):
    """The writable directory: an existing, non-symlink directory that is not / , $HOME or an ancestor of either."""
    if not path or not os.path.isabs(path):
        raise ValueError("--rw must be an absolute directory")
    if os.path.islink(path):
        raise ValueError(f"--rw refuses a symlinked directory: {path}")
    if not os.path.isdir(path):
        raise ValueError(f"--rw is not a directory: {path}")
    real = os.path.realpath(path)
    homes = set(_home_dirs())
    homes.update(os.path.realpath(h) for h in (os.environ.get("HOME"),) if h)
    try:
        import pwd

        homes.add(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    except Exception:
        pass
    for h in homes:
        if real == "/" or h == real or h.startswith(real.rstrip("/") + "/"):
            raise ValueError(f"--rw refuses {real}: it is / , the home directory or an ancestor of it")
    return real


def run(rw, timeout, cmd):
    status = probe()
    if status != "available":
        print(status, file=sys.stderr)
        return 3
    bwrap = shutil.which("bwrap")
    rfd, wfd = os.pipe()
    argv = [bwrap, "--json-status-fd", str(wfd), *base_args(rw), "--", *cmd]
    proc = subprocess.Popen(argv, start_new_session=True, pass_fds=(wfd,), stdin=subprocess.DEVNULL)
    os.close(wfd)
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # --die-with-parent then takes down the pid namespace's init and everything in it
        except ProcessLookupError:
            pass
        rc = proc.wait()
    with os.fdopen(rfd, "r") as fh:
        started = any('"child-pid"' in line for line in fh.read().splitlines())
    if timed_out:
        print(f"sandbox: timed out after {timeout}s; the process tree was killed", file=sys.stderr)
        return 124
    if not started:
        print(_unavailable("bwrap setup failed before the command started"), file=sys.stderr)
        return 3
    return rc if rc >= 0 else 128 - rc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="sandbox.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="sub", required=True)
    pp = sub.add_parser("probe")
    pp.add_argument("--format", choices=["text", "json"], default="text")
    pr = sub.add_parser("run")
    pr.add_argument("--rw", required=True)
    pr.add_argument("--timeout", type=float, required=True)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.sub == "probe":
        status = probe()
        if args.format == "json":
            print(json.dumps({"status": status.split(":")[0], "detail": status}))
        else:
            print(status)
        return 0 if status == "available" else 3
    if not cmd or args.timeout <= 0:
        print("usage: sandbox.py run --rw DIR --timeout SECONDS -- CMD [ARG...]", file=sys.stderr)
        return 2
    try:
        rw = check_rw(args.rw)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return run(rw, args.timeout, cmd)


if __name__ == "__main__":
    sys.exit(main())
