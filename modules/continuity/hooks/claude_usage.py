#!/usr/bin/env python3
"""Claude Code Stop hook: record the session's context usage in the ledger (stdlib only).

Reads at most the last 256 KiB of the transcript, takes the last assistant entry's `message.usage` (all four counts
must be integers) and `message.model`, and records them; anything it cannot parse (or fail to record) is recorded as `unknown`, never
guessed; a `<synthetic>` placeholder entry counts as unknown. No output, exit 0 always; a no-op for dispatched workers (AI_ARCH_JOB_MARKER), subagents and projects
without a ledger.
"""

import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
TAIL = 256 * 1024
COUNTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def read_tail(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        start = max(0, info.st_size - TAIL)
        os.lseek(fd, start, os.SEEK_SET)
        data = b""
        while len(data) < TAIL:
            chunk = os.read(fd, TAIL - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if start > 0:
        data = data.partition(b"\n")[2]  # the first line is cut mid-way
    return data


def last_usage(path):
    """(tokens or None, model or None) from the last assistant entry in the transcript tail."""
    try:
        data = read_tail(path)
    except (OSError, TypeError, ValueError):
        return None, None
    for raw in reversed(data.split(b"\n")):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
        if not isinstance(message, dict):
            continue
        model = message.get("model") if isinstance(message.get("model"), str) and message.get("model") else None
        if model == "<synthetic>":  # a client-made placeholder entry: its zero counts are not a measurement
            return None, None
        usage = message.get("usage")
        if not isinstance(usage, dict) or not all(
                isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and usage[k] >= 0 for k in COUNTS):
            return None, model
        return sum(usage[k] for k in COUNTS), model
    return None, None


def main():
    if "AI_ARCH_JOB_MARKER" in os.environ:
        return
    try:
        payload = json.loads(sys.stdin.read(1 << 20))
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict) or payload.get("agent_id") or not isinstance(payload.get("session_id"), str):
        return
    from modules.continuity import jobs, resume
    from modules.continuity import usage as U
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
    state = resume.find_state(cwd)
    if state is None:
        return
    path = payload.get("transcript_path")
    tokens, model = last_usage(path) if isinstance(path, str) and path else (None, None)
    ctx, key = jobs.Context(state), f"claude-code:{payload['session_id']}"
    try:
        U.record(ctx, key, tokens, None, model, "claude-transcript")
    except Exception:  # noqa: BLE001 - a failed write must not leave the previous count standing as the current one
        U.record(ctx, key, None, None, None, "claude-transcript")  # unknown; if this fails too the hook gives up


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - a hook must never block the session or print a traceback
        pass
    sys.exit(0)
