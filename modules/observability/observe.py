#!/usr/bin/env python3
"""Usage, cost, quota and session/resource reports (stdlib only; no network; no process or model is launched).

  observe.py spend --prices FILE [--receipts JSONL]... [--envelopes JSONL]...
  observe.py session --project DIR --session HARNESS:ID...
  observe.py resources --pid N... [--proc-root DIR]
  observe.py quota status|gate --config FILE [--provider NAME]

Every command prints one JSON object. Exit: 0 ok, 2 refused or bad usage, 3 quota gate `blocked`.
`--out DIR` additionally writes the report as <command>.json (0600) into DIR (created 0700; a symlink or a directory
owned by someone else is refused). Unknown usage and unknown cost stay null; nothing here ranks anything.
"""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.observability import cost, probes, quota, reports, usage  # noqa: E402


class Refused(Exception):
    pass


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("spend")
    s.add_argument("--prices", required=True)
    s.add_argument("--receipts", action="append", default=[])
    s.add_argument("--envelopes", action="append", default=[])
    s = sub.add_parser("session")
    s.add_argument("--project", required=True)
    s.add_argument("--session", action="append", required=True)
    s = sub.add_parser("resources")
    s.add_argument("--pid", type=int, action="append", required=True)
    s.add_argument("--proc-root", default="/proc")
    for name in ("quota",):
        s = sub.add_parser(name)
        s.add_argument("action", choices=("status", "gate"))
        s.add_argument("--config", required=True)
        s.add_argument("--provider")
    for s in sub.choices.values():
        s.add_argument("--out")
    return p


def prepare_out(out):
    out = Path(out).expanduser()
    if out.is_symlink() or os.path.abspath(out) != os.path.realpath(out):
        raise Refused("--out is, or lies below, a symlink")
    if out.exists():
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Refused("--out is not a directory owned by you")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(mode=0o700)
    return out


def write_report(out, name, doc):
    out = prepare_out(out)
    fd = os.open(out / f"{name}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n")


def run(args):
    if args.command == "spend":
        rows = []
        for path in args.receipts:
            rows += usage.load_receipts([path])
        for path in args.envelopes:
            rows += usage.load_envelopes(path)
        return 0, reports.spend_report(rows, cost.load_prices(args.prices))
    if args.command == "session":
        return 0, reports.session_report(args.project, args.session)
    if args.command == "resources":
        return 0, probes.resource_report(args.pid, args.proc_root)
    config = quota.load_config(args.config)
    base = Path(args.config).resolve().parent
    if args.action == "status":
        return 0, quota.status(config, base)
    if not args.provider:
        raise Refused("quota gate needs --provider")
    return quota.gate(config, base, args.provider)


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    try:
        code, doc = run(args)
        if args.out:
            write_report(args.out, args.command, doc)
    except (Refused, ValueError, OSError, KeyError, TypeError, UnicodeDecodeError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {str(error)[:200]}"}))
        return 2
    print(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
