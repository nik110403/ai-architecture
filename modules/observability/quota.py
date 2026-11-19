"""Quota state from operator-written snapshot files. No network, no stored credentials, no provider API.

Only the generic `snapshot-file` adapter is supported: a JSON file the operator (or their own script) keeps current,
{provider, fetched_at, windows: [{name, used_percent, resets_at}]}. Every other adapter is listed `unsupported` and
is never read. A gate never blocks on stale, malformed or unreadable data: that is `unavailable`, never headroom.

Config: {"version": 1, "providers": {NAME: {"adapter": "snapshot-file", "path": FILE, "gate": bool,
"threshold_percent": 0..100 (needed when gate is true), "max_age_seconds": number > 0 (default 900)}}}
"""

import datetime
import json
import math
import os
import stat
import time
from pathlib import Path

MAX_BYTES = 1_000_000
DEFAULT_MAX_AGE = 900
ADAPTERS = {
    "snapshot-file": {"supported": True},
    "claude-statusline": {"supported": False},
    "codex-sidecar": {"supported": False},
    "opencode-quota-cache": {"supported": False},
}


def is_supported(adapter):
    return ADAPTERS.get(adapter, {}).get("supported", False)


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def read_json_file(path):
    """Strict JSON from a regular, non-symlink file of at most 1 MB."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("not a regular file of at most 1 MB")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ValueError("file over 1 MB")
    return json.loads(data.decode("utf-8"), parse_constant=_no_constant)


def load_config(path):
    doc = read_json_file(path)
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("providers"), dict):
        raise ValueError('quota config must be {"version": 1, "providers": {...}}')
    return doc


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_time(value):
    if _number(value):
        return float(value)
    if isinstance(value, str):
        try:
            when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return when.timestamp() if when.tzinfo else None
    return None


def _windows(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("windows must be a non-empty list")
    out = []
    for w in raw:
        used = w.get("used_percent") if isinstance(w, dict) else None
        if not isinstance(w.get("name"), str) or not _number(used) or not 0 <= used <= 100:
            raise ValueError("window needs a name and used_percent in 0..100")
        out.append({"name": w["name"], "used_percent": used, "resets_at": w.get("resets_at")})
    return out


def evaluate(name, entry, base, now):
    """State of one configured provider: available, stale, unavailable or unsupported."""
    adapter = entry.get("adapter") if isinstance(entry, dict) else None
    result = {"provider": name, "adapter": adapter if isinstance(adapter, str) else None, "supported": is_supported(adapter),
              "gate": entry.get("gate") is True if isinstance(entry, dict) else False, "state": "unsupported",
              "reason": "adapter is not supported", "windows": None}
    if not result["supported"]:
        return result
    try:
        max_age = entry.get("max_age_seconds", DEFAULT_MAX_AGE)
        if not _number(max_age) or max_age <= 0 or not isinstance(entry.get("path"), str):
            raise ValueError("invalid provider config")
        if result["gate"] and not (_number(entry.get("threshold_percent")) and 0 <= entry["threshold_percent"] <= 100):
            raise ValueError("gate needs threshold_percent in 0..100")
        snap = read_json_file(Path(base) / entry["path"])
        if not isinstance(snap, dict) or snap.get("provider") != name:
            raise ValueError("snapshot provider does not match")
        fetched = parse_time(snap.get("fetched_at"))
        windows = _windows(snap.get("windows"))
        if fetched is None:
            raise ValueError("fetched_at unreadable")
        age = now - fetched
        if age < 0:
            raise ValueError("fetched_at is in the future")
    except (OSError, ValueError, AttributeError, TypeError) as error:
        return dict(result, state="unavailable", reason=f"snapshot unusable: {str(error)[:80]}")
    result["windows"] = windows
    if age > max_age:
        return dict(result, state="stale", reason="snapshot older than max_age_seconds")
    return dict(result, state="available", reason="fresh snapshot")


def status(config, base, now=None):
    now = time.time() if now is None else now
    return {"providers": [evaluate(n, e, base, now) for n, e in sorted(config["providers"].items())]}


def gate(config, base, name, now=None):
    """-> (exit code, report). not_applicable unless supported, gate: true and fresh; blocked is exit 3."""
    now = time.time() if now is None else now
    entry = config["providers"].get(name)
    if entry is None:
        return 0, {"provider": name, "gate": "not_applicable", "state": "unsupported", "reason": "provider not configured"}
    st = evaluate(name, entry, base, now)
    report = {"provider": name, "state": st["state"], "reason": st["reason"]}
    if st["state"] == "unsupported" or not st["gate"]:
        return 0, dict(report, gate="not_applicable")
    if st["state"] != "available":
        return 0, dict(report, gate="unavailable")
    threshold = entry["threshold_percent"]
    hot = [w["name"] for w in st["windows"] if w["used_percent"] >= threshold]
    if hot:
        return 3, dict(report, gate="blocked", windows_over=hot)
    return 0, dict(report, gate="ok")
