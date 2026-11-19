#!/usr/bin/env python3
"""Claude Code SessionStart hook: inject the latest checkpoint's Next once (stdlib only).

Wire it by hand (see INTEGRATION.md). It consumes through `resume.consume` for `claude-code:<session_id>`; it prints
nothing and exits 0 on any error (SessionStart cannot block), when the payload names a subagent, when a dispatched
worker's own harness session runs it (AI_ARCH_JOB_MARKER is set), or when no ledger for this project is found.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main():
    if "AI_ARCH_JOB_MARKER" in os.environ:
        return
    try:
        payload = json.loads(sys.stdin.read(1 << 20))
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return
    source, session_id = payload.get("source"), payload.get("session_id")
    if source not in ("startup", "resume", "clear", "compact") or not isinstance(session_id, str):
        return
    from modules.continuity import jobs, resume
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
    state = resume.find_state(cwd)
    if state is None:
        return
    ctx = jobs.Context(state)
    key = f"claude-code:{session_id}"
    _, out = resume.consume(ctx, key, cwd, source, defer=True)
    if out.get("injection"):
        try:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": out["injection"]}}))
            sys.stdout.flush()
        except (OSError, ValueError):
            return  # not written: the Next stays unconsumed for the next session
    if out.get("mark_pending"):
        resume.commit_consumed(ctx, key, out["checkpoint_revision"])  # only after a successful write


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - a hook must never block the session or print a traceback
        pass
    sys.exit(0)
