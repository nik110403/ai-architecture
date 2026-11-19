#!/usr/bin/env python3
"""Claude Code SessionStart hook: inject a bounded curated memory index for declared scopes (stdlib only).

Usage: session_start.py --root LABEL=PATH ... --scope LABEL:SLUG ... [--child-scope LABEL:SLUG ...] [--max-bytes N]

Child rule: when the payload names a subagent (`agent_id`) or the environment has AI_ARCH_JOB_MARKER, only the
`--child-scope` scopes are injected, and only if they are a subset of `--scope`; with none, a child gets nothing.
It reads no HOME and no environment path, prints nothing and exits 0 on any error (SessionStart cannot block).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import curated  # noqa: E402


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--child-scope", action="append", default=[])
    parser.add_argument("--max-bytes", type=curated.max_bytes_type, default=2048)
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.read(1 << 20))
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict):
        return
    roots = curated.parse_roots(args.root)
    scopes = args.scope
    if payload.get("agent_id") or "AI_ARCH_JOB_MARKER" in os.environ:
        if not args.child_scope or not set(args.child_scope) <= set(args.scope):
            return
        scopes = args.child_scope
    text = curated.inject_text(roots, curated.parse_scopes(scopes, roots), args.max_bytes)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - a hook must never block the session or print a traceback
        pass
    sys.exit(0)
