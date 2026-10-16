"""Per-repo last-worked-topic state, written by the Stop hook (clean-state-check.py) and read
by resume-inject.py so the resume follows the tracker THIS session actually worked on, instead
of blind mtime-newest (which picks the wrong primary whenever another session or process
touches a different tracker).

state_path(cwd) -> <state dir>/resume-topic-<sha1(repo_root)[:16]>
write_topic(cwd, tracker_path) -> None, fail-open
read_topic(cwd) -> absolute path str | None, fail-open (also None once the state has aged
past CLAUDE_RESUME_TOPIC_TTL_HOURS, default 72 -- mtime order takes back over)
"""
import hashlib
import os
import time

from trackers import _repo_root
import gates_config


def state_path(cwd):
    root = _repo_root(cwd)
    state_dir = gates_config.state_dir()
    digest = hashlib.sha1(root.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return os.path.join(state_dir, f"resume-topic-{digest}")


def write_topic(cwd, tracker_path):
    try:
        path = state_path(cwd)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if os.path.lexists(path) and not os.path.isfile(path):
            return  # refuse to write into a FIFO/socket/dir at this path (would block/misbehave)
        content = f"{os.path.realpath(tracker_path)}\n{int(time.time())}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, content.encode("utf-8", "surrogateescape"))
        finally:
            os.close(fd)
    except Exception:
        pass


def read_topic(cwd):
    try:
        path = state_path(cwd)
        if not os.path.isfile(path):   # rejects FIFO/socket/dir (same guard style as
            return None                # clean-state-check's has_resume_block) -- never blocks on open
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            raw = os.read(fd, 4096)
        finally:
            os.close(fd)
        lines = raw.decode("utf-8", "replace").split("\n")
        line = (lines[0] if lines else "").strip()
        if not line or not os.path.isabs(line) or not os.path.isfile(line):
            return None
        ttl_hours = float(os.environ.get("CLAUDE_RESUME_TOPIC_TTL_HOURS", "72"))
        try:
            epoch = int((lines[1] if len(lines) > 1 else "").strip())
        except (ValueError, IndexError):
            return None  # missing/unparseable epoch -- mtime order takes over
        if (time.time() - epoch) / 3600.0 > ttl_hours:
            return None  # stale state -- mtime order takes over
        root = _repo_root(cwd)
        real = os.path.realpath(line)
        if real != root and not real.startswith(root + os.sep):
            return None
        return line
    except Exception:
        return None
