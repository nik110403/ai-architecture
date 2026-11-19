"""Runner state: strict JSON, schema shape check, hash chain, atomic write, flock, liveness, redaction.

Nothing here starts a process. state.json is written only by the lock holder; every reader treats it as untrusted.
"""

import hashlib
import json
import os
import re
import socket
import stat
import time
from pathlib import Path

SCHEMA = "autopilot-state/1"
MAX_STATE_BYTES = 2 * 1024 * 1024
MARKER = "state-corrupt"
TYPES = {"string": str, "int": int, "number": (int, float), "bool": bool, "object": dict, "list": list, "null": type(None)}


class Corrupt(Exception):
    pass


def expected_uid():
    return os.geteuid()


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key}")
        out[key] = value
    return out


def strict_loads(text):
    return json.loads(text, object_pairs_hook=_unique, parse_constant=_no_constant)


def check_shape(doc, schema):
    """`schema` is {"required": {field: "type|type"}}; every field must be present with one of the listed types."""
    if not isinstance(doc, dict):
        raise Corrupt("not an object")
    for field, kinds in schema["required"].items():
        if field not in doc:
            raise Corrupt(f"missing field {field}")
        names = kinds.split("|")
        value = doc[field]
        ok = any(isinstance(value, TYPES[n]) and not (n in ("int", "number") and isinstance(value, bool)) for n in names)
        if "bool" in names and isinstance(value, bool):
            ok = True
        if not ok:
            raise Corrupt(f"field {field} has the wrong type")


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def chain_sha(prev, record):
    body = {k: v for k, v in record.items() if k not in ("sha", "prev")}
    return sha(prev + "\n" + canonical(body))


def chain_append(doc, record):
    prev = doc["chain_head"]
    record["prev"] = prev
    record["sha"] = chain_sha(prev, record)
    doc["rounds"].append(record)
    doc["chain_head"] = record["sha"]


def chain_check(doc):
    prev = "0" * 64
    for record in doc["rounds"]:
        if not isinstance(record, dict) or record.get("prev") != prev or record.get("sha") != chain_sha(prev, record):
            raise Corrupt("hash chain broken")
        prev = record["sha"]
    if doc["chain_head"] != prev:
        raise Corrupt("hash chain head mismatch")


def schema_dir():
    return Path(__file__).resolve().parent / "schemas"


def load_schema(name):
    return strict_loads((schema_dir() / f"{name}.json").read_text(encoding="utf-8"))


def load(state_dir):
    """state.json of `state_dir`, fully validated. Raises Corrupt for anything wrong; never repairs."""
    d = Path(state_dir)
    try:
        info = os.lstat(d)
    except OSError:
        raise Corrupt("state dir unreadable")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Corrupt("state dir is not a real directory")
    if info.st_uid != expected_uid():
        raise Corrupt("state dir owner is not the current user")
    f = d / "state.json"
    try:
        fi = os.lstat(f)
    except OSError:
        raise Corrupt("state.json missing")
    if stat.S_ISLNK(fi.st_mode) or not stat.S_ISREG(fi.st_mode):
        raise Corrupt("state.json is not a regular file")
    if fi.st_uid != expected_uid():
        raise Corrupt("state.json owner is not the current user")
    if fi.st_size > MAX_STATE_BYTES:
        raise Corrupt("state.json is larger than the size cap")
    try:
        doc = strict_loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as error:
        raise Corrupt(f"state.json unreadable: {error.__class__.__name__}")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise Corrupt("state.json does not carry this tool's schema field")
    check_shape(doc, load_schema("state"))
    chain_check(doc)
    return doc


def write(state_dir, doc):
    """Atomic replace: temp file, fsync, rename, fsync dir. 0600."""
    d = Path(state_dir)
    data = json.dumps(doc, indent=1, sort_keys=True, allow_nan=False)
    tmp = d / "state.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, d / "state.json")
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_marker(state_dir, why):
    try:
        (Path(state_dir) / MARKER).write_text(why[:200] + "\n", encoding="utf-8")
    except OSError:
        pass


def marker_exists(state_dir):
    return (Path(state_dir) / MARKER).exists()


# --- processes ------------------------------------------------------------------------------------------------------

def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def start_ticks(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def owner_record(pid, kickoff_id):
    return {"pid": pid, "start_ticks": start_ticks(pid), "boot_id": boot_id(), "kickoff_id": kickoff_id}


def alive(owner):
    """True only when pid, start ticks and boot id all match: a reused pid or another boot is never the controller."""
    if not isinstance(owner, dict):
        return False
    pid, ticks = owner.get("pid"), owner.get("start_ticks")
    if not isinstance(pid, int) or ticks is None or owner.get("boot_id") != boot_id():
        return False
    return start_ticks(pid) == ticks


def lock_path(state_dir):
    return Path(state_dir) / "lock"


# --- redaction --------------------------------------------------------------------------------------------------------

def redactor(*paths):
    """A function that rewrites known absolute paths, $HOME, the user name and the host name out of printed text."""
    import getpass
    pairs = []
    for label, p in paths:
        if p:
            pairs.append((str(p), label))
            try:
                pairs.append((os.path.realpath(p), label))
            except OSError:
                pass
    home = os.environ.get("HOME")
    if home and home != "/":
        pairs.append((home, "~"))
    pairs.sort(key=lambda x: -len(x[0]))
    words = []
    try:
        words.append((getpass.getuser(), "<user>"))
    except Exception:  # noqa: BLE001 - no passwd entry
        pass
    words.append((socket.gethostname(), "<host>"))

    def redact(text):
        text = str(text)
        for needle, label in pairs:
            text = text.replace(needle, label)
        for word, label in words:
            if len(word) > 2:
                text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_-])", label, text)
        return text
    return redact


def now():
    return time.time()
