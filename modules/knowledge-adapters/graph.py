"""Graph adapter: a graphify-style read-only command run under a scrubbed environment, with graph-hash-stamped facts.

Also holds the one spawner (`run_child`) the brain adapter reuses. The child gets exactly PATH=/usr/bin:/bin and a
throwaway HOME (no API keys, no base URLs), a timeout and a 1 MiB output cap. Only read operations are allowed.
"""

import hashlib
import importlib.util
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


recall = _load("kad_recall", HERE.parent / "memory-backends" / "recall.py")

MAX_OUT = 1 << 20
MAX_GRAPH = 256 << 20
READ_OPS = ("query", "path", "explain", "affected")
MAX_FACTS = 50


class Refuse(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def load_config(path, kind):
    """recall.py's command-config shape plus a `kind`. Recall's own validation runs first."""
    try:
        cfg = recall.load_config(path)
    except recall.Refuse as error:
        raise Refuse(str(error)) from None
    if cfg.get("kind") != kind:
        raise Refuse(f"config kind must be {kind!r}")
    return cfg


def graph_sha(path):
    """sha256 of the graph file, or None when it cannot be read as a regular file (the fact is then unverified)."""
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_GRAPH:
                return None
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def run_child(argv, extra, timeout_s, stdin=None):
    """Spawn argv+extra without a shell. Returns (state, output bytes): state is ok, timeout or error."""
    deadline = time.monotonic() + timeout_s
    with tempfile.TemporaryDirectory() as home:
        env = {"PATH": "/usr/bin:/bin", "HOME": home}
        try:
            proc = subprocess.Popen([*argv, *extra], shell=False, env=env, cwd=home,
                                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return "error", b""

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()

        out, state = b"", "ok"
        try:
            if stdin is not None:
                try:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                except OSError:
                    pass
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    state = "timeout"
                    break
                if not select.select([proc.stdout], [], [], min(left, 0.1))[0]:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                out += chunk
                if len(out) > MAX_OUT:
                    state = "error"
                    break
            if state == "ok":
                try:
                    proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    state = "timeout"
        finally:
            kill()
            proc.stdout.close()
        if state == "ok" and proc.returncode != 0:
            state = "error"
    return state, out


def parse_version(text):
    found = re.search(r"\d+\.\d+\.\d+", text)
    return found.group(0) if found else None


def handshake(cfg):
    state, out = run_child(cfg["argv"], ["--version"], cfg["timeout_s"])
    version = parse_version(out.decode("utf-8", "replace")) if state == "ok" else None
    return {"state": state if version or state != "ok" else "error", "backend": recall.sanitize(cfg["name"], 64),
            "version": version, "graph_sha256": graph_sha(cfg.get("graph"))}


def query(cfg, op, args):
    if op not in READ_OPS:
        raise Refuse(f"operation {op!r} is not allowed; only {', '.join(READ_OPS)}")
    if not args or len(args) > 4 or any(not isinstance(a, str) or not a or a.startswith("-") or len(a) > 500 or "\0" in a for a in args):
        raise Refuse("arguments must be 1-4 plain strings that do not start with '-'")
    graph = cfg.get("graph")
    if not isinstance(graph, str) or not graph or "\0" in graph:
        raise Refuse("config needs a graph path")
    sha = graph_sha(graph)
    state, out = run_child(cfg["argv"], [op, *args, "--graph", graph], cfg["timeout_s"])
    facts = []
    if state == "ok":
        for line in out.decode("utf-8", "replace").splitlines():
            text = recall.sanitize(line, 400)
            if text and len(facts) < MAX_FACTS:
                facts.append({"text": text, "backend": recall.sanitize(cfg["name"], 64),
                              "version": recall.sanitize(cfg["version"], 64), "graph_sha256": sha,
                              "status": "verified" if sha else "unverified"})
    return {"state": state, "facts": facts}
