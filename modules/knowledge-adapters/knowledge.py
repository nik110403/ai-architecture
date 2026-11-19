#!/usr/bin/env python3
"""knowledge-adapters: graph and brain capability handshakes, read-only by default.

Stdlib only. No network code here; backends are programs you declare in a config file and they run under a scrubbed
environment. Every command needs --out DIR (a 0700 receipt directory) and prints one JSON object. `exercised` is
recomputed from run receipts, never read from a config. Exit codes: 0 ok, 1 backend failure, 2 usage or refusal,
3 permission refused (fails closed).
"""

import argparse
import datetime
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


graph = _load("kad_graph", HERE / "graph.py")
brain = _load("kad_brain", HERE / "brain.py")
Refuse = graph.Refuse
ISOLATED = ("netns", "net-denied")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_out(path):
    if not isinstance(path, str) or not path or "\0" in path or ".." in path.split("/"):
        raise Refuse("--out is empty or contains '..'")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        os.makedirs(path, mode=0o700)
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refuse("--out must be a real directory, not a symlink")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refuse("--out must be owned by you with mode 0700")
    return os.open(path, _DIR)


def append_receipt(outfd, record):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (json.dumps({**record, "at": stamp}, sort_keys=True) + "\n").encode()
    fd = os.open("knowledge.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=outfd)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def receipts(outfd):
    try:
        fd = os.open("knowledge.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=outfd)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as handle:
        return [json.loads(line) for line in handle.read().decode().splitlines() if line.strip()]


def run_graph(args, outfd):
    cfg = graph.load_config(args.config, "graphify")
    if args.action == "handshake":
        result = graph.handshake(cfg)
        record = {"event": "handshake", "ok": result["state"] == "ok", "backend_version": result["version"],
                  "graph_sha256": result["graph_sha256"]}
    else:
        result = graph.query(cfg, args.op, args.arg or [])
        record = {"event": "query", "op": args.op, "ok": result["state"] == "ok", "backend_version": None,
                  "graph_sha256": result["facts"][0]["graph_sha256"] if result["facts"] else None}
    append_receipt(outfd, {**record, "lane": "graph", "spawned": True, "isolation": args.isolation, "backend": cfg["name"],
                           "argv0": cfg["argv"][0]})
    return result, 0 if record["ok"] else 1


def run_brain(args, outfd):
    cfg = brain.load_config(args.config)
    base = {"lane": "brain", "isolation": args.isolation, "backend": cfg["name"], "argv0": cfg["argv"][0]}
    if args.action == "handshake":
        result = {"handshake": brain.handshake(cfg)}
        record = {"event": "handshake", "ok": True, "spawned": True, "backend_version": result["handshake"]["version"]}
    elif args.action == "status":
        result = {"status": brain.status(cfg)}
        record = {"event": "status", "ok": True, "spawned": True, "backend_version": None}
    else:
        try:
            result = brain.query(cfg, args.q, args.source_id or [], args.purpose, args.permissions)
        except Refuse as error:
            if error.code == 3:  # a refusal is itself worth a receipt: no process was spawned
                append_receipt(outfd, {**base, "event": "query", "ok": False, "spawned": False, "backend_version": None,
                                       "refused": str(error)})
            raise
        record = {"event": "query", "ok": True, "spawned": True, "backend_version": None}
    append_receipt(outfd, {**base, **record})
    return result, 0


def exercised(outfd):
    """A lane is EXERCISED only when a real run receipt exists with an attested network denial."""
    records = receipts(outfd)
    out = {}
    for lane in ("graph", "brain"):
        runs = [r for r in records if r.get("lane") == lane and r.get("ok") and r.get("spawned")]
        good = [r for r in runs if r.get("isolation") in ISOLATED and r.get("backend_version")]
        if good:
            out[lane] = {"status": "EXERCISED", "backend": good[-1]["backend"], "version": good[-1]["backend_version"],
                         "argv0": good[-1]["argv0"], "isolation": good[-1]["isolation"]}
        else:
            reason = "no run receipt" if not runs else "no run receipt with an attested network denial and a backend version"
            out[lane] = {"status": "NOT RUN", "reason": reason}
    return out


def build_parser():
    parser = argparse.ArgumentParser(prog="knowledge.py", description=__doc__.splitlines()[0])
    groups = parser.add_subparsers(dest="group", required=True)
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--out", required=True, metavar="DIR")
    run = argparse.ArgumentParser(add_help=False, parents=[base])
    run.add_argument("--config", required=True)
    run.add_argument("--isolation", choices=("none", *ISOLATED), default="none",
                     help="record how the network was denied for this run (only the caller can attest it)")
    g = groups.add_parser("graph").add_subparsers(dest="action", required=True)
    g.add_parser("handshake", parents=[run])
    gq = g.add_parser("query", parents=[run])
    gq.add_argument("--op", required=True)
    gq.add_argument("--arg", action="append")
    b = groups.add_parser("brain").add_subparsers(dest="action", required=True)
    b.add_parser("handshake", parents=[run])
    b.add_parser("status", parents=[run])
    bq = b.add_parser("query", parents=[run])
    bq.add_argument("--q", required=True)
    bq.add_argument("--source-id", action="append")
    bq.add_argument("--purpose")
    bq.add_argument("--permissions")
    groups.add_parser("exercised", parents=[base])
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    outfd = None
    try:
        outfd = open_out(args.out)
        if args.group == "exercised":
            result, rc = exercised(outfd), 0
        elif args.group == "graph":
            result, rc = run_graph(args, outfd)
        else:
            result, rc = run_brain(args, outfd)
    except Exception as exc:
        if exc.__class__.__name__ == "Refuse":
            print(f"refused: {exc}", file=sys.stderr)
            return exc.code
        if isinstance(exc, OSError):
            print(f"failed: {exc.strerror or type(exc).__name__}", file=sys.stderr)
            return 1
        raise
    finally:
        if outfd is not None:
            os.close(outfd)
    print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
