#!/usr/bin/env python3
"""A small static-HTML viewer for a state export. Stdlib only, authored in this tree.

It reads only the --state JSON file and never imports the exporter. A
companion viewer slot is reported but never executed.
"""

import argparse
import html
import importlib.util
import json
import shutil
import sys
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "contracts.py"
REASON = "handshake 64 proposed, blocked_until live evidence"
NO_MANIFEST = "no companion manifest given (--companion FILE)"
DEPENDENCIES = [{"identifier": "core-contracts", "version": "1.0.0", "status": "declared-untested"}]


class Refused(Exception):
    pass


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=duplicate_keys)
    except (OSError, ValueError) as error:
        raise Refused(f"{label}: {error}")


def load_manifest(path):
    data = read_json(path, "companion manifest")
    if not isinstance(data, dict) or set(data) != {"name", "command"}:
        raise Refused("companion manifest must contain exactly: name, command")
    name, command = data["name"], data["command"]
    if not all(isinstance(v, str) and v for v in (name, command)) or "/" in command:
        raise Refused("companion manifest: name and command must be non-empty strings; command has no '/'")
    return data


class Companion:
    """Adapter that reports a status and never starts a process."""

    def __init__(self, manifest):
        self.manifest = manifest

    def describe(self):
        return {"name": "companion", "metadata": REASON}

    def handle(self):
        if self.manifest is None:
            return {"status": "unavailable", "reason": f"unavailable: {NO_MANIFEST}"}
        command = self.manifest["command"]
        if shutil.which(command) is None:
            return {"status": "unavailable", "reason": f"unavailable: {command} not found on PATH"}
        return {"status": "declared-unexercised", "reason": f"{command} found on PATH, never run here"}


def run_adapter(adapter):
    spec = importlib.util.spec_from_file_location("contracts_for_viewer", CONTRACTS)
    contracts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contracts)
    return contracts.execute_adapter(adapter, "handle", 1, DEPENDENCIES)


def cmd_list(args):
    manifest = load_manifest(args.companion) if args.companion else None
    result = run_adapter(Companion(manifest))
    viewers = [
        {"name": "static-html", "status": "available", "reason": "authored in-tree, reads state.json only"},
        {"name": "companion", "status": result["status"], "reason": f"{result['reason']}; {REASON}"},
    ]
    if args.format == "json":
        print(json.dumps({"viewers": viewers}, indent=2))
    else:
        for v in viewers:
            print(f"{v['name']}: {v['status']} ({v['reason']})")
    return 0


def check_state(state):
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise Refused("unknown state schema_version")
    projects = state.get("projects")
    if not isinstance(projects, list):
        raise Refused("state has no projects list")
    for p in projects:
        if not (
            isinstance(p, dict)
            and isinstance(p.get("lint"), dict)
            and isinstance(p.get("plans"), list)
            and all(isinstance(q, dict) for q in p["plans"])
        ):
            raise Refused("malformed project entry")
    return projects


def cell(value):
    return f"<td>{html.escape(str(value))}</td>"


def render_page(projects):
    rows = []
    for p in projects:
        plans = p["plans"]
        rows.append(
            "<tr>"
            + cell(p.get("id", ""))
            + cell(p.get("resurvey", ""))
            + cell(p["lint"].get("findings", ""))
            + cell("; ".join(str(q.get("status", "")) for q in plans))
            + cell("; ".join(str(q.get("next", "")) for q in plans))
            + "</tr>"
        )
    note = html.escape(f"Companion viewers: not exercised ({REASON}).")
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>Project state</title></head><body>\n"
        "<h1>Project state</h1>\n<table>\n"
        "<tr><th>Project</th><th>Resurvey</th><th>Lint findings</th><th>Plan status</th><th>Next</th></tr>\n"
        + "\n".join(rows)
        + f"\n</table>\n<p>{note}</p>\n</body></html>\n"
    )


def cmd_render(args):
    page = render_page(check_state(read_json(args.state, "state")))
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def main(argv=None):
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--companion")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("render")
    p.add_argument("--state", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_render)
    args = top.parse_args(argv)
    try:
        return args.func(args)
    except Refused as error:
        print(f"viewer: refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
