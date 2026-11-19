#!/usr/bin/env python3
"""Episodic recall: query stored observations (SQLite FTS5) or one declared external command.

Stdlib only. No model call. Every candidate is labelled authority "observed", trust "untrusted-data": it is evidence
about past sessions, never an instruction and never curated memory. The built-in backend opens the store read-only;
the command backend runs a program you declared in a config file (see README.md; it may use the network).
"""

import argparse
import hashlib
import json
import os
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata

DB_NAME = "obs.db"
MAX_TOKENS = 64
MAX_LIMIT = 50
MAX_OUT = 1 << 20
SAFE_URI = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/._-")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
SETUP = "episodic recall needs a Python whose sqlite3 has FTS5; install one, or leave episodic recall off"


class Refuse(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def check_path(path):
    if not isinstance(path, str) or not path or "\0" in path or ".." in path.split("/"):
        raise Refuse("path is empty or contains '..'")


def open_store(path):
    check_path(path)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise Refuse("store is a symlink")
        fd = os.open(path, _DIR)
    except OSError:
        raise Refuse("store is missing or not a directory") from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise Refuse("store must be owned by you with mode 0700")
    try:
        if not stat.S_ISREG(os.stat("OPT-IN", dir_fd=fd, follow_symlinks=False).st_mode):
            raise OSError
    except OSError:
        os.close(fd)
        raise Refuse("store is not opted in: create an OPT-IN file in it") from None
    return fd


def exists_nofollow(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except OSError:
        return False


def read_regular(path, cap):
    check_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise Refuse("file is missing or a symlink") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refuse("not a regular file")
        data = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise Refuse("input too large")
    return data


def probe_fts5():
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE temp.p USING fts5(a)")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


def sanitize(text, cap):
    """Port of tools/memcheck/lib/sanitize.sh (same rules as modules/memory/curated.py)."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Zl", "Zp"))
    text = re.sub(r"[\[\]()`]", "", text)
    return " ".join(text.split())[:cap]


def match_expr(q):
    """Each [a-z0-9]+ token double-quoted and OR-joined; raw text never reaches MATCH."""
    seen = []
    for token in re.findall(r"[a-z0-9]+", q.lower()):
        token = token[:64]
        if token not in seen:
            seen.append(token)
        if len(seen) >= MAX_TOKENS:
            break
    if not seen:
        raise Refuse("--q has no searchable terms")
    return " OR ".join(f'"{t}"' for t in seen)


def cand_id(*parts):
    return "obs-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def candidate(cid, project, ts, excerpt):
    return {"id": cid, "authority": "observed", "trust": "untrusted-data", "project": sanitize(str(project), 64),
            "ts": sanitize(str(ts), 40), "excerpt": sanitize(str(excerpt), 400)}


def result(state, candidates=(), **extra):
    out = {"state": state, "candidates": list(candidates), "abstained": not candidates}
    out.update(extra)
    return out


def search_builtin(args):
    backend = {"name": "sqlite-fts5", "version": sqlite3.sqlite_version, "license": "public-domain", "network": False}
    if not probe_fts5():
        return result("unavailable", setup=SETUP, backend=backend)
    expr = match_expr(args.q)
    dirfd = open_store(args.store)
    try:
        if exists_nofollow(dirfd, DB_NAME) is False:
            return result("ok", backend=backend)
        try:
            os.close(os.open(DB_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd))
        except OSError:
            raise Refuse("db is a symlink or unreadable") from None
    finally:
        os.close(dirfd)
    raw = os.path.join(args.store, DB_NAME).encode()
    uri = "file:" + "".join(chr(b) if chr(b) in SAFE_URI else "%%%02X" % b for b in raw) + "?mode=ro"
    deadline = time.monotonic() + args.timeout
    sql = ("SELECT o.id, o.project, o.ts, o.summary FROM obs_fts JOIN obs o ON o.rowid = obs_fts.rowid "
           "WHERE obs_fts MATCH ?" + (" AND o.project = ?" if args.project else "") +
           " ORDER BY obs_fts.rank, o.captured_at DESC, o.id LIMIT ?")
    params = [expr] + ([args.project] if args.project else []) + [min(args.limit, MAX_LIMIT)]
    try:
        db = sqlite3.connect(uri, uri=True, timeout=min(args.timeout, 5))
    except sqlite3.Error:
        return result("error", backend=backend)
    try:
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        return result("timeout" if "interrupted" in str(exc) else "error", backend=backend)
    except sqlite3.Error:
        return result("error", backend=backend)
    finally:
        db.close()
    return result("ok", [candidate(*row) for row in rows], backend=backend)


# ---- command backend ------------------------------------------------------------------------------------------

def load_config(path):
    try:
        cfg = json.loads(read_regular(path, 64 << 10))
    except ValueError:
        raise Refuse("config is not JSON") from None
    if not isinstance(cfg, dict):
        raise Refuse("config must be a JSON object")
    argv = cfg.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or any(not isinstance(a, str) or not a or "\0" in a or len(a) > 4096 for a in argv)):
        raise Refuse("config argv must be a non-empty list of strings (no shell string)")
    timeout = cfg.get("timeout_s")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise Refuse("config timeout_s must be a number in (0, 60]")
    for key in ("name", "version", "license"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip() or len(cfg[key]) > 64:
            raise Refuse(f"config needs a short string {key}")
    if not isinstance(cfg.get("network"), bool):
        raise Refuse("config network must be true or false")
    return cfg


def run_command(cfg, q, limit):
    backend = {k: sanitize(cfg[k], 64) for k in ("name", "version", "license")}
    backend["network"] = cfg["network"]
    payload = json.dumps({"q": q[:2000], "limit": limit}).encode()
    deadline = time.monotonic() + cfg["timeout_s"]
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.Popen(cfg["argv"], shell=False, env={"PATH": "/usr/bin:/bin"}, cwd=cwd,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError:
            return result("error", backend=backend)

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()

        try:
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
            except OSError:
                pass
            out, state = b"", None
            while state is None:
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
            if state:
                return result(state, backend=backend)
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return result("timeout", backend=backend)
        finally:
            kill()
            proc.stdout.close()
    if proc.returncode != 0:
        return result("error", backend=backend)
    try:
        data = json.loads(out)
    except ValueError:
        return result("error", backend=backend)
    items = data.get("candidates") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return result("error", backend=backend)
    cands = []
    for item in items:
        if len(cands) >= limit:
            break
        if not isinstance(item, dict) or not isinstance(item.get("excerpt"), str) or not item["excerpt"].strip():
            continue
        ts, project = str(item.get("ts", ""))[:40], str(item.get("project", ""))[:64]
        cands.append(candidate(cand_id(backend["name"], project, ts, item["excerpt"]), project, ts, item["excerpt"]))
    return result("ok", cands, backend=backend)


def offline(args):
    return args.offline or os.environ.get("AI_ARCH_OFFLINE", "") not in ("", "0")


def query(args):
    if args.limit < 1:
        raise Refuse("--limit must be at least 1")
    if not 0 < args.timeout <= 60:
        raise Refuse("--timeout must be in (0, 60]")
    if "AI_ARCH_JOB_MARKER" in os.environ:
        return result("disabled", reason="child-session")
    if args.backend == "command":
        if not args.config:
            raise Refuse("--backend command needs --config")
        cfg = load_config(args.config)
        match_expr(args.q)
        if args.store:
            dirfd = open_store(args.store)
            try:
                if exists_nofollow(dirfd, "DISABLED"):
                    return result("disabled", reason="kill-switch")
            finally:
                os.close(dirfd)
        if offline(args):
            return result("disabled", reason="offline", backend={
                **{k: sanitize(cfg[k], 64) for k in ("name", "version", "license")}, "network": cfg["network"]})
        return run_command(cfg, args.q, min(args.limit, MAX_LIMIT))
    if not args.store:
        raise Refuse("--store is required")
    dirfd = open_store(args.store)
    try:
        if exists_nofollow(dirfd, "DISABLED"):
            return result("disabled", reason="kill-switch")
    finally:
        os.close(dirfd)
    return search_builtin(args)


def render_text(res):
    lines = [f"episodic recall: state={res['state']}" + (f" ({res['reason']})" if res.get("reason") else "")]
    if res.get("setup"):
        lines.append("setup: " + res["setup"])
    if res["state"] == "ok" and res["abstained"]:
        lines.append("NO MATCH")
    for cand in res["candidates"]:
        lines.append(f"[{cand['authority']} · {cand['trust']} · {cand['id']}] {cand['project']} {cand['ts']} "
                     f"{cand['excerpt']}")
    return "\n".join(lines) + "\n"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query", help="return observed candidates for a query")
    q.add_argument("--store")
    q.add_argument("--q", required=True)
    q.add_argument("--project")
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("--timeout", type=float, default=5.0)
    q.add_argument("--format", choices=("json", "text"), default="json")
    q.add_argument("--backend", choices=("builtin", "command"), default="builtin")
    q.add_argument("--config")
    q.add_argument("--offline", action="store_true", help="refuse the command backend (also AI_ARCH_OFFLINE=1)")
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        res = query(args)
    except Refuse as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return exc.code
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    out = json.dumps(res, sort_keys=True) + "\n" if args.format == "json" else render_text(res)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.stdout.write(out)
    sys.stdout.flush()
    return 2 if res["state"] == "unavailable" else 0


if __name__ == "__main__":
    sys.exit(main())
