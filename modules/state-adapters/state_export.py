#!/usr/bin/env python3
"""Export the state of registered projects as one versioned JSON file. Stdlib only.

Reads only the explicit --registry and the roots it lists; lint and resurvey
come from the stewardship module run as a subprocess. No hostname or username
is recorded; project roots are the only paths in the output.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

STEWARD = Path(__file__).resolve().parents[1] / "stewardship" / "steward.py"
NEXT = re.compile(r"^\s*(?:[-*]\s+)?\**Next\b[^:]*:\**\s*(.*?)\s*$")
STATUS = re.compile(r"Status:\s*\**\s*([A-Za-z-]+)")


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def steward(*args):
    return subprocess.run(
        [sys.executable, "-B", str(STEWARD), *map(str, args)],
        capture_output=True, text=True, timeout=300,
    )


def plan_summary(tracker):
    text = tracker.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    status = next((m.group(1) for m in map(STATUS.search, lines) if m), "unknown")
    nxt, inside = "", False
    for line in lines:
        if line.startswith("## "):
            inside = line.strip().lower() == "## resume here"
        elif inside and (m := NEXT.match(line)):
            nxt = m.group(1)
            break
    return {"topic": tracker.parent.name, "status": status, "next": nxt}


def project_state(entry, resurvey):
    root = Path(entry["root"])
    lint = {"findings": 0, "by_code": {}}
    plans = []
    if root.is_dir():
        done = steward("lint", "--root", root, "--format", "json")
        if done.returncode not in (0, 1):
            raise RuntimeError(f"lint failed for {entry.get('id', root.name)}: {done.stderr.strip()}")
        findings = json.loads(done.stdout)["findings"]
        lint["findings"] = len(findings)
        for finding in findings:
            lint["by_code"][finding["code"]] = lint["by_code"].get(finding["code"], 0) + 1
        plans = [plan_summary(t) for t in sorted(root.glob("plans/*/TRACKER.md"))]
    return {
        "id": entry.get("id", root.name),
        "root": str(root),
        "resurvey": resurvey,
        "lint": lint,
        "plans": plans,
    }


def export(registry, out):
    done = steward("resurvey", "--registry", registry, "--format", "json")
    if done.returncode not in (0, 1):
        raise RuntimeError(f"resurvey failed: {done.stderr.strip()}")
    status = {e["root"]: e["status"] for e in json.loads(done.stdout)["entries"]}
    data = json.loads(Path(registry).read_text(encoding="utf-8"), object_pairs_hook=duplicate_keys)
    state = {
        "schema_version": 1,
        "projects": [project_state(p, status[p["root"]]) for p in data["projects"]],
    }
    Path(out).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None):
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export")
    p.add_argument("--registry", required=True)
    p.add_argument("--out", required=True)
    args = top.parse_args(argv)
    try:
        export(args.registry, args.out)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"state-export: invalid input: {error}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
