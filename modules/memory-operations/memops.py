#!/usr/bin/env python3
"""memory-operations: reviewed promotion, frozen snapshots with evaluation, and database-aware sync of observation stores.

Stdlib only. No network, no model call, no process control. Every command needs --out DIR (a 0700 receipt directory that
is never inside a store or memory root) and prints one JSON object. Exit codes: 0 ok, 1 failed verification or I/O,
2 usage or refusal, 3 a guarded write was refused, 4 conflict or sha mismatch. See README.md.
"""

import argparse
import importlib.util
import json
import os
import sqlite3
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


common = _load("memops_common", HERE / "common.py")
redact = _load("memops_redact", HERE / "redact.py")
promote = _load("memops_promote", HERE / "promote.py")
snapshot = _load("memops_snapshot", HERE / "snapshot.py")
sync = _load("memops_sync", HERE / "sync.py")


def build_parser():
    parser = argparse.ArgumentParser(prog="memops.py", description=__doc__.splitlines()[0])
    groups = parser.add_subparsers(dest="group", required=True)
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--out", required=True, metavar="DIR", help="0700 receipt directory (created if missing)")

    def action(group, name, *flags, **kw):
        p = group.add_parser(name, parents=[base], **kw)
        for flag, opts in flags:
            p.add_argument(flag, **opts)
        return p

    req = {"required": True}
    pr = groups.add_parser("promote").add_subparsers(dest="action", required=True)
    action(pr, "stage", ("--from-recall", req), ("--candidate-id", req), ("--name", req), ("--type", req), ("--description", req))
    action(pr, "approve", ("--id", req), ("--sha", req), ("--approver", req), ("--type-confirm", {}))
    action(pr, "apply", ("--id", req), ("--root", req), ("--dir", req))
    action(pr, "rollback", ("--receipt", req), ("--root", req))
    sn = groups.add_parser("snapshot").add_subparsers(dest="action", required=True)
    action(sn, "freeze", ("--store", req))
    action(sn, "query", ("--snapshot", req), ("--pin", req), ("--q", req), ("--limit", {"type": int, "default": 8}))
    action(sn, "eval", ("--snapshot", req), ("--pin", req), ("--goldset", req), ("--min-questions", {"type": int, "default": 5}))
    action(sn, "compare", ("--a", req), ("--a-pin", req), ("--b", req), ("--b-pin", req), ("--eval-a", {}), ("--eval-b", {}))
    sy = groups.add_parser("sync").add_subparsers(dest="action", required=True)
    action(sy, "status", ("--store", req))
    action(sy, "backup", ("--store", req))
    action(sy, "merge", ("--target", req), ("--replica", req), ("--apply", {"action": "store_true"}))
    action(sy, "recover", ("--id", req), ("--target", req), ("--restore", {"action": "store_true"}),
           ("--accept-loss", {"action": "store_true"}))
    action(sy, "restore", ("--backup", req), ("--expect-sha", req), ("--target", req))
    return parser


def dispatch(args, outfd):
    out = args.out
    table = {
        ("promote", "stage"): lambda: promote.stage(args, outfd),
        ("promote", "approve"): lambda: promote.approve(args, outfd),
        ("promote", "apply"): lambda: promote.apply(args, outfd, out),
        ("promote", "rollback"): lambda: promote.rollback(args, outfd),
        ("snapshot", "freeze"): lambda: snapshot.freeze(args, outfd, out),
        ("snapshot", "query"): lambda: snapshot.query(args, outfd, out),
        ("snapshot", "eval"): lambda: snapshot.evaluate(args, outfd, out),
        ("snapshot", "compare"): lambda: snapshot.compare(args, outfd, out),
        ("sync", "status"): lambda: sync.status(args, outfd, out),
        ("sync", "backup"): lambda: sync.backup(args, outfd, out),
        ("sync", "merge"): lambda: sync.merge(args, outfd, out),
        ("sync", "recover"): lambda: sync.recover(args, outfd, out),
        ("sync", "restore"): lambda: sync.restore(args, outfd, out),
    }
    return table[(args.group, args.action)]()


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    outfd = None
    try:
        outfd = common.open_out(args.out)
        result = dispatch(args, outfd)
    except Exception as exc:
        if exc.__class__.__name__ == "Refuse":  # common, curated, observe and recall each define their own Refuse
            print(f"refused: {exc}", file=sys.stderr)
            return exc.code
        if isinstance(exc, sqlite3.Error):
            print(f"failed: database error ({type(exc).__name__})", file=sys.stderr)
            return 1
        if isinstance(exc, OSError):
            print(f"failed: {exc.strerror or type(exc).__name__}", file=sys.stderr)
            return 1
        raise
    finally:
        if outfd is not None:
            os.close(outfd)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
