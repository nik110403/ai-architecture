#!/usr/bin/env python3
"""Claude Code PreToolUse hook (matcher Edit|Write|MultiEdit|NotebookEdit|Bash): enforce the session's freeze record.

No record for the payload's session: no output, exit 0. A frozen session's out-of-root edits and Bash are denied.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.continuity import freeze  # noqa: E402


def main():
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    except OSError:
        raw = b""
    if len(raw) > 1024 * 1024:
        raw = b""  # an oversized payload is unparseable: fail closed only when a freeze exists
    try:
        allowed, reason = freeze.decide_payload(raw)
    except freeze.L.LedgerError as error:  # e.g. an unusable AI_ARCH_FREEZE_DIR: the freeze cannot be consulted
        allowed, reason = False, f"frozen: the freeze directory cannot be consulted ({error})"
    if not allowed:
        print(json.dumps(freeze.deny_output(reason)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
