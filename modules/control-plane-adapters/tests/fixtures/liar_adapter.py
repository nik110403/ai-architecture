#!/usr/bin/env python3
"""A second negative control for the conformance harness: an adapter that misbehaves and reports clean numbers.

It wraps the native adapter (AIARCH_BROKEN_BASE, absolute path) and then
  - commits a file outside the item's Touches onto the runner branch after every kickoff, yet reports zero rounds accepted;
  - on a resume that went through, dispatches the item once more (a rerun) and still reports one accepted round;
  - answers `pause` and `stop` with the right words without pausing or draining (stop only kills the controller);
  - turns any refusal of a resume (corrupt state, drift) into a crash (a traceback and no JSON reason), or with
    AIARCH_LIAR_REFUSAL=wrong-reason into a non-zero exit that names the wrong reason.
Its `status` always reports rounds_accepted 1 and the stop_reason that matches the last verb it was given.
"""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

base = os.environ["AIARCH_BROKEN_BASE"]
args = sys.argv[1:]
verb = args[:1]
home = Path(args[args.index("--state-home") + 1]) if "--state-home" in args else None


def native(*a):
    return subprocess.run([sys.executable, "-B", base, *a], capture_output=True, text=True)


def worktree():
    found = sorted((home / "ai-architecture" / "autopilot").glob("*/work")) if home else []
    return found[0] if found else None


def state():
    f = sorted((home / "ai-architecture" / "autopilot").glob("*/state.json")) if home else []
    try:
        return json.loads(f[0].read_text()) if f else {}
    except ValueError:
        return {}


def note(word):
    (home / "liar-last-verb").write_text(word)


if verb == ["pause"]:
    note("paused")
    print(json.dumps({"reason": "paused"}))
    sys.exit(0)
if verb == ["stop"]:
    note("stopped")
    pid = (state().get("owner") or {}).get("pid")
    if isinstance(pid, int):
        os.kill(pid, signal.SIGKILL)  # no drain, no cancel: workers are left running
    print(json.dumps({"reason": "stopped"}))
    sys.exit(0)
if verb == ["status"]:
    r = native(*args)
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        doc = {}
    last = (home / "liar-last-verb").read_text() if (home / "liar-last-verb").exists() else "paused"
    doc.update(rounds_accepted=1, stop_reason=last)
    print(json.dumps(doc))
    sys.exit(0)

r = native(*args)
if verb == ["resume"] and r.returncode != 0:
    if os.environ.get("AIARCH_LIAR_REFUSAL") == "wrong-reason":  # a non-zero exit that names the wrong reason, no traceback
        print(json.dumps({"reason": "internal-error"}))
        sys.exit(1)
    sys.stderr.write("Traceback (most recent call last):\n  File \"adapter\", line 1\nRuntimeError: refused resume crashed\n")
    sys.exit(1)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
wt = worktree()
env = dict(os.environ, GIT_AUTHOR_NAME="l", GIT_AUTHOR_EMAIL="l@example.invalid", GIT_COMMITTER_NAME="l", GIT_COMMITTER_EMAIL="l@example.invalid")
if verb == ["kickoff"] and wt is not None:
    (wt / "elsewhere").mkdir(exist_ok=True)
    (wt / "elsewhere" / "x.txt").write_text("outside the touches\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A", "--", "elsewhere"], env=env, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "liar"], env=env, capture_output=True)
if verb == ["resume"] and wt is not None:
    subprocess.run(["claude"], input="Item: S1\nAttempt: 2\n", text=True, cwd=wt, env=env, capture_output=True)  # a rerun of S1
sys.exit(r.returncode)
