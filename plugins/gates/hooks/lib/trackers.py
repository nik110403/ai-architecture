"""Shared plan-pack tracker discovery for the resume and Stop hooks.

active_trackers(cwd) -> list[(path, mtime)], newest mtime first.

Candidates: <cwd>/TRACKER.md, <cwd>/plans/*/TRACKER.md, <cwd>/HANDOFF.md. Realpath-deduped.
Dropped: anything under /.claude/worktrees/ (duplicate trackers inside a worktree checkout),
anything whose realpath resolves outside the repo root (a symlink escaping the repo -- the
legit `plans/topic -> ../topic` symlink stays inside and passes), and
anything whose header marks the pack `Status: done|abandoned|promoted` (regex copied verbatim
from clean-state-check.py's `tracker_status`).
"""
import glob
import os
import re
import stat
import subprocess

_STATUS_RE = re.compile(r"(?m)^\*{0,2}Status:\s*\**\s*((?i:active|done|abandoned|promoted))")


def printable(s, cap):
    """Sanitize text before it enters a model-facing additionalContext string or user-facing
    stderr message (F1a): keep only str.isprintable() chars (drops ANSI ESC, BEL, NUL, BS,
    newlines), collapse whitespace runs to one space, hard-cap at `cap` chars. Truncation
    prefers the last word boundary at/before the cap so output never ends mid-token, then
    appends a trailing "...". NOT used for on-disk paths (those stay raw for realpath checks).
    """
    s = "".join(c for c in str(s) if c.isprintable())
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= cap:
        return s
    head = s[:cap]
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + "…"


def _repo_root(cwd):
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.strip())
    except Exception:
        pass
    return os.path.realpath(cwd)


def _read_regular(path, limit):
    """Read up to `limit` chars of a REGULAR file, else None. O_NONBLOCK + fstat on the same
    fd: a FIFO/device named like a tracker (or swapped in after a stat) never hangs the caller."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except Exception:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        fh = os.fdopen(fd, encoding="utf-8", errors="ignore")
    except Exception:
        os.close(fd)
        return None
    try:
        with fh:
            return fh.read(limit)
    except Exception:
        return None


def tracker_status(path):
    # First ~4KB is plenty for a header field; matches `**Status: active**` and bare
    # `Status: done` alike. No match (old trackers, migration pending) -> None -> active.
    head = _read_regular(path, 4000)
    if head is None:
        return None
    m = _STATUS_RE.search(head)
    return m.group(1).lower() if m else None


_NEXT_RE = re.compile(r"(?m)^-?\s*\*\*Next(?:\s+action)?:\*\*")
_STATE_RE = re.compile(r"(?m)^-\s*\*\*State\b[^\n]*?:\*\*")


def _capture_bullet(text, start):
    """From a matched label start, capture through end-of-bullet: the next top-level `- `
    bullet, a blank line, or a heading (`^#`) -- whichever comes first. Continuation
    (wrapped/indented) lines stay in the capture and are joined by the caller.
    """
    tail = text[start:]
    end = len(tail)
    for pat in (r"(?m)^[ \t]*\n", r"(?m)^-\s", r"(?m)^#"):
        for m in re.finditer(pat, tail):
            if m.start() > 0:
                end = min(end, m.start())
                break
    return tail[:end].rstrip("\n")


def next_bullet(path, fallback_state=False):
    """Capture a tracker's Next-variant bullet (`- **Next:**` / `- **Next action:**` / bare
    `**Next:**`, F1d), spanning continuation lines. Falls back to the `- **State` bullet
    (same capture rules) when `fallback_state` is True and no Next-variant bullet exists.
    Returns the RAW joined body (label stripped, continuation lines joined with a single
    space) -- UNSANITIZED: callers must run it through printable() at the point the text
    enters a message/context string (house rule -- trackers.py stays a discovery module, not
    a sanitizer). None if neither bullet exists, or on any read error (fail-open).
    """
    text = _read_regular(path, 200_000)
    if text is None:
        return None

    m = _NEXT_RE.search(text)
    label_re = _NEXT_RE
    if not m and fallback_state:
        m = _STATE_RE.search(text)
        label_re = _STATE_RE
    if not m:
        return None

    raw = _capture_bullet(text, m.start())
    body = label_re.sub("", raw, count=1)
    body = body.replace("\n", " ")
    body = re.sub(r"[ \t]+", " ", body).strip()
    return body or None


def topic_name(path):
    """Topic name for a tracker path: plans/<topic>/TRACKER.md -> <topic>;
    <repo>/TRACKER.md -> the tracker's parent dir name; HANDOFF.md -> "HANDOFF". Both
    TRACKER.md cases reduce to "basename of the tracker's parent dir" -- no repo-root lookup
    needed. Uses the path as given (not realpath) so a `plans/<topic> -> ../<topic>` symlink
    still reports the logical <topic> name.
    """
    if os.path.basename(path) == "HANDOFF.md":
        return "HANDOFF"
    return os.path.basename(os.path.dirname(path)) or os.path.basename(path)


def active_trackers(cwd):
    cwd = os.path.realpath(cwd)
    root = _repo_root(cwd)

    candidates = []
    root_tracker = os.path.join(cwd, "TRACKER.md")
    if os.path.isfile(root_tracker):
        candidates.append(root_tracker)
    try:
        candidates.extend(sorted(glob.glob(os.path.join(cwd, "plans", "*", "TRACKER.md"))))
    except Exception:
        pass
    handoff = os.path.join(cwd, "HANDOFF.md")
    if os.path.isfile(handoff):
        candidates.append(handoff)

    seen = set()
    out = []
    for c in candidates:
        real = os.path.realpath(c)
        if real in seen:
            continue
        seen.add(real)
        if "/.claude/worktrees/" in real:
            continue
        if not os.path.isfile(real):  # FIFO/device/dir named like a tracker
            continue
        if real != root and not real.startswith(root + os.sep):
            continue
        if tracker_status(real) in ("done", "abandoned", "promoted"):
            continue
        try:
            mtime = os.path.getmtime(real)
        except Exception:
            continue
        out.append((c, mtime))

    out.sort(key=lambda t: t[1], reverse=True)
    return out
