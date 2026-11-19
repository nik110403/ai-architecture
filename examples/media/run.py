#!/usr/bin/env python3
"""Media example, offline: builds and inspects every obligation of the media module and prints one line each.

Usage: run.py --offline [--out DIR] [--format text|json]
Read README.md first. `--offline` is required. Slides, the four visuals and the video are built from the synthetic specs in
specs/ and rendered by the Chrome and FFmpeg you installed; illustration runs only the stub provider (a fixture) and stays
BLOCKED. Nothing here uses the network, a hosted model or a paid provider. Exit codes: 0 ok, 1 inspection failed,
2 refused, 3 a required renderer is unavailable (only when AIARCH_REQUIRE_MEDIA=1)."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MEDIA = ROOT / "modules" / "media" / "media.py"


def say(doc, as_json, code):
    print(json.dumps(doc, sort_keys=True) if as_json else "\n".join(doc.get("lines") or [f"{doc['status']}: {doc['reason']}"]))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    as_json = args.format == "json"
    if not args.offline:
        return say({"status": "refused", "reason": "offline flag required"}, as_json, 2)
    cmd = [sys.executable, "-B", str(MEDIA), "receipts", "--format", "json", "--providers", str(HERE / "providers.json"),
           "--allow-provider"] + (["--out", args.out] if args.out else [])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        return say({"status": "failed", "reason": "no result from the media module", "lines": [proc.stderr[-300:]]}, as_json, 1)
    lines = []
    for row in doc.get("rows", []):
        tail = f" {row['blocker_class']}: {row['reason']}" if row["status"] == "BLOCKED" else ""
        lines.append(f"{row['id']} {row['status']}{tail}")
    interface = next((r.get("interface") for r in doc.get("rows", []) if r["id"] == "illustration"), None) or {}
    if "rows" in doc:
        lines.append(f"interface: {interface.get('backend', 'none')} ({interface.get('backend_reason') or interface.get('reason', 'not run')})")
    doc["lines"], doc["interface"] = lines, interface
    return say(doc, as_json, proc.returncode)


if __name__ == "__main__":
    sys.exit(main())
