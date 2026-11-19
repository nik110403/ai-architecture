"""Host-side Verify: the tree is built from the object store (no checkout, so no filter or hook can run), then the item's
frozen Verify command runs inside modules/isolation (bwrap). Exit 3 from the sandbox means it was unavailable: the command
never ran, and the caller must stop `unverifiable` - there is no unsandboxed fallback anywhere in this file.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "modules" / "isolation" / "sandbox.py"
SEAM = "AIARCH_AUTOPILOT_SANDBOX_PATH"  # tightens only: the sandbox child's PATH (tests point it at an empty dir)
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def git_env():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/nonexistent"), "LANG": "C.UTF-8"}
    env.update(GIT_ENV)
    return env


def git(repo, *args, check=True, input=None):
    cmd = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    r = subprocess.run(cmd, capture_output=True, env=git_env(), input=input, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r


def safe_rel(path):
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and ".." not in parts and ".git" not in parts and "" not in parts


def materialize(repo, rev, dest):
    """Write the tree of `rev` under `dest` from blobs. Returns the number of entries skipped as unsafe."""
    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", rev).stdout
    entries = []
    for rec in listing.split(b"\0"):
        if not rec:
            continue
        meta, _, name = rec.partition(b"\t")
        mode, kind, obj = meta.decode().split()
        entries.append((mode, kind, obj, name.decode("utf-8", "surrogateescape")))
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    skipped = 0
    blobs = [e for e in entries if e[0] in ("100644", "100755", "120000")]
    proc = subprocess.Popen(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env())
    try:
        for mode, kind, obj, name in blobs:
            proc.stdin.write(obj.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)
            if not safe_rel(name):
                skipped += 1
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                link = data.decode("utf-8", "surrogateescape")
                if link.startswith("/") or ".." in link.split("/"):
                    skipped += 1
                    continue
                os.symlink(link, target)
            else:
                target.write_bytes(data)
                target.chmod(0o755 if mode == "100755" else 0o644)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    for mode, kind, obj, name in entries:
        if mode == "160000" and safe_rel(name):
            (dest / name).mkdir(parents=True, exist_ok=True)
    return skipped


def run(command, workdir, timeout):
    """(rc, sandboxed). rc 3 = sandbox unavailable (command never ran); 124 = timed out."""
    env = {"PATH": os.environ.get(SEAM) if SEAM in os.environ else os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/nonexistent"), "LANG": "C.UTF-8"}
    argv = [sys.executable, "-B", str(SANDBOX), "run", "--rw", str(workdir), "--timeout", str(int(timeout)), "--", "/bin/sh", "-c", command]
    r = subprocess.run(argv, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=timeout + 60)
    return r.returncode, r.stderr.decode("utf-8", "replace")[-400:]


def cleanup(path):
    shutil.rmtree(path, ignore_errors=True)
