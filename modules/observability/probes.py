"""Read-only process probes: /proc/<pid>/{stat,status} only. Unreadable or absent fields are None.

Never opens cmdline, environ or fd, never writes, never starts a process, never touches the network.
"""

import os

READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
FIELDS = ("state", "ppid", "utime_ticks", "stime_ticks", "threads", "start_ticks", "rss_kb")


def _read(path, limit=65536):
    try:
        fd = os.open(path, READ_FLAGS)
    except OSError:
        return None
    try:
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_stat(text):
    if not text or ")" not in text:
        return {}
    f = text[text.rindex(")") + 2:].split()
    if len(f) < 20:
        return {}
    return {"state": f[0], "ppid": _int(f[1]), "utime_ticks": _int(f[11]), "stime_ticks": _int(f[12]),
            "threads": _int(f[17]), "start_ticks": _int(f[19])}


def parse_status(text):
    if not text:
        return {}
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            return {"rss_kb": _int(parts[1]) if len(parts) >= 2 else None}
    return {}


def read_pid(pid, proc_root="/proc"):
    base = os.path.join(proc_root, str(pid))
    row = {"pid": pid, **dict.fromkeys(FIELDS)}
    row.update(parse_stat(_read(os.path.join(base, "stat"))))
    row.update(parse_status(_read(os.path.join(base, "status"))))
    return row


def resource_report(pids, proc_root="/proc"):
    for pid in pids:
        if type(pid) is not int or pid < 1:
            raise ValueError("pid must be an integer >= 1")
    return {"proc_root": "/proc" if proc_root == "/proc" else "fixture", "processes": [read_pid(p, proc_root) for p in pids]}
