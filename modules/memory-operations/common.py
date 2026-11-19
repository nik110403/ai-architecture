"""Shared helpers for memory-operations: refusals, the 0700 receipt directory, append-only lane receipts, sibling loading.

Stdlib only. Receipts live under --out, never inside a store or a memory root, so a restore or rollback cannot erase them.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKENDS = HERE.parent / "memory-backends"
MEMORY = HERE.parent / "memory"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
URI_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/._-")
LANES = ("promotion", "snapshots", "sync", "knowledge")


class Refuse(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def load_path(key, path):
    """Load a module by file path under a unique key (reuse by path, never by copy)."""
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_env():
    """Name of the first agent-session variable that is set, else None (approval is refused from agent sessions)."""
    for name in sorted(os.environ):
        if name in ("AI_ARCH_JOB_MARKER", "CLAUDECODE") or name.startswith(("CLAUDE_CODE_", "OPENCODE")):
            return name
    return None


def open_out(path):
    """The receipt directory: created 0700 if missing; a symlink, a foreign owner or a wrong mode is refused."""
    if not isinstance(path, str) or not path or "\0" in path or ".." in path.split("/"):
        raise Refuse("--out is empty or contains '..'")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        os.makedirs(path, mode=0o700)
        info = os.lstat(path)
    except OSError:
        raise Refuse("--out is unreadable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refuse("--out must be a real directory, not a symlink")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refuse("--out must be owned by you with mode 0700")
    return os.open(path, _DIR)


def subdir(dirfd, *names):
    """Open (creating 0700 when missing) nested directories below dirfd without following a symlink."""
    fd = os.dup(dirfd)
    try:
        for name in names:
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            nxt = os.open(name, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_new_at(dirfd, name, data, mode=0o600):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dirfd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_at(dirfd, name, data, mode=0o600):
    """Atomically replace name with data (temp file in the same directory, then rename)."""
    temp = f".{name}.tmp-{os.getpid()}"
    try:
        os.unlink(temp, dir_fd=dirfd)
    except FileNotFoundError:
        pass
    write_new_at(dirfd, temp, data, mode)
    os.replace(temp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)


def read_at(dirfd, name, cap=1 << 24):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        chunks, size = [], 0
        while size <= cap:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > cap:
            raise Refuse("input too large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_file(path, cap=1 << 24):
    """Read a regular file whose final component is not a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise Refuse("file is missing or a symlink") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refuse("not a regular file")
        chunks, size = [], 0
        while size <= cap:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > cap:
        raise Refuse("input too large")
    return b"".join(chunks)


def append_receipt(outfd, lane, record):
    """One JSON line per event in <out>/<lane>.jsonl (append-only, fsynced). Lanes never share a file."""
    if lane not in LANES:
        raise ValueError(lane)
    line = (json.dumps({**record, "at": now()}, sort_keys=True) + "\n").encode()
    fd = os.open(f"{lane}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=outfd)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_receipts(outfd, lane):
    try:
        data = read_at(outfd, f"{lane}.jsonl")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def ro_uri(path):
    """A mode=ro SQLite URI. Never skips locking: a reader must see a consistent database."""
    raw = os.fsencode(path)
    return "file:" + "".join(chr(b) if chr(b) in URI_SAFE else "%%%02X" % b for b in raw) + "?mode=ro"


def check_pattern(value, pattern, what):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise Refuse(f"{what} is malformed")
    return value
