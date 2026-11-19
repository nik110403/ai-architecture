"""Context manifest consumer: turn a `context_packet.py` packet into a worker prompt and a job record.

The producer only warns about its budget; this consumer obeys it. Exactly the packet's `read` files are read, in
order, and nothing else. The packet is not trusted: it is validated, then every path is re-checked with the
producer's own safe-path rules (symlinks refused). The check and the read are not raceproof against a concurrent
filesystem change, the same limit the producer states.
"""

import importlib.util
import os
import posixpath
import re
import stat
from pathlib import Path

from modules.continuity import ledger as L

OPEN, CLOSE = "<untrusted-file-content>", "</untrusted-file-content>"
PACKET_KEYS = ("schema_version", "disposition", "task", "project", "room", "read", "exclude", "skills", "agent",
               "budget_tokens", "expected_turns", "provenance", "missing_paths", "estimated_tokens", "over_budget")
MAX_PACKET = 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
EXCLUDE_OK = re.compile(r"[A-Za-z0-9._/@+=%~-]{1,256}")


def producer():
    """`skills/context/scripts/context_packet.py` loaded by path (its safe-path rules are reused, not copied)."""
    path = L.ROOT / "skills" / "context" / "scripts" / "context_packet.py"
    spec = importlib.util.spec_from_file_location("_continuity_context_packet", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def str_list(value, name):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise L.Refused(f"context packet: {name} must be a list of non-empty strings")
    if any(ord(c) < 32 or ord(c) == 127 for v in value for c in v):
        raise L.Refused(f"context packet: {name} holds a control character")
    return value


def load_packet(path):
    try:
        raw = L.read_file(path, MAX_PACKET)
        packet = L.strict_loads(raw)
    except (OSError, ValueError, RecursionError) as error:
        raise L.Refused(f"context packet unreadable: {error.__class__.__name__}: {error}")
    if not isinstance(packet, dict):
        raise L.Refused("context packet: must be an object")
    allowed = set(PACKET_KEYS) | {"note"}
    if set(PACKET_KEYS) - set(packet) or set(packet) - allowed:
        raise L.Refused("context packet: keys differ from the documented set "
                        f"(missing {sorted(set(PACKET_KEYS) - set(packet))}, unknown {sorted(set(packet) - allowed)})")
    if not L.is_int(packet["schema_version"]) or packet["schema_version"] != 1:
        raise L.Refused("context packet: schema_version must be 1")
    if not isinstance(packet["project"], str) or not os.path.isabs(packet["project"]):
        raise L.Refused("context packet: project must be an absolute path")
    for name in ("read", "exclude", "missing_paths"):
        str_list(packet[name], name)
    if not L.is_int(packet["budget_tokens"]) or packet["budget_tokens"] < 0:
        raise L.Refused("context packet: budget_tokens must be an integer >= 0")
    return packet, L.sha256_hex(raw)


def read_below(root_fd, parts, limit):
    """Bytes of the regular file `parts` below `root_fd`, no symlink followed at any step."""
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return data
            data += chunk
        raise OSError("file too large")
    finally:
        os.close(fd)


def strip_delimiters(text):
    while OPEN in text or CLOSE in text:
        text = text.replace(OPEN, "").replace(CLOSE, "")
    return text


def build(packet_path, intent, project_root):
    """(prompt, job `context` record) for `--context PACKET --intent T`; L.Refused before anything is launched."""
    if not isinstance(intent, str) or not intent.strip():
        raise L.Refused("--context needs --intent (the task text the files are attached to)")
    packet, packet_sha = load_packet(packet_path)
    root = os.path.realpath(packet["project"])
    ledger_root = os.path.realpath(project_root)
    if root != ledger_root and not root.startswith(ledger_root.rstrip(os.sep) + os.sep):
        raise L.Refused("context packet: project is not inside this ledger's project root")
    if packet["missing_paths"]:
        raise L.Refused(f"context packet: missing paths {packet['missing_paths']}")
    read, exclude = packet["read"], packet["exclude"]
    mod = producer()
    excluded = []
    for entry in exclude:  # printed unfenced in the trusted part of the prompt: validate like a read path
        try:
            mod.safe_parts(entry.rstrip("/") or entry)
        except ValueError as error:
            raise L.Refused(f"context packet: unsafe exclude path: {error}")
        if not EXCLUDE_OK.fullmatch(entry):
            raise L.Refused(f"context packet: exclude entry has characters outside the allowed set: {entry!r}")
        excluded.append(posixpath.normpath(entry))
    normal = [posixpath.normpath(p) for p in read]
    if len(set(normal)) != len(normal):
        raise L.Refused("context packet: a path is listed twice in read")
    clash = [p for p, n in zip(read, normal) if any(n == e or n.startswith(e + "/") for e in excluded)]
    if clash:
        raise L.Refused(f"context packet: paths both read and excluded: {clash}")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise L.Refused(f"context packet: project unusable: {error.__class__.__name__}")
    try:
        sizes = []
        for path in read:
            try:
                parts = mod.safe_parts(path)
                info = mod.target_stat(root_fd, path)
            except (ValueError, OSError) as error:
                raise L.Refused(f"context packet: unsafe read path: {error}")
            if info is None:
                raise L.Refused(f"context packet: read file is missing: {path!r}")
            sizes.append((parts, info.st_size))
        budget = packet["budget_tokens"]
        if sum(size for _, size in sizes) // 4 > budget:  # checked from the sizes: nothing is read when it is over
            raise L.Refused(f"context packet: estimated tokens exceed the budget of {budget} tokens")
        files, blocks, total = [], [], 0
        for path, (parts, _) in zip(read, sizes):
            try:
                data = read_below(root_fd, parts, MAX_FILE)
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise L.Refused(f"context packet: read file unusable: {path!r}: {error.__class__.__name__}")
            total += len(data)
            files.append({"path": path, "sha256": L.sha256_hex(data), "bytes": len(data)})
            blocks.append(f"### {path}\n{OPEN}\n{strip_delimiters(text)}\n{CLOSE}")
    finally:
        os.close(root_fd)
    if total // 4 > budget:  # a file that grew between the stat and the read
        raise L.Refused(f"context packet: estimated tokens exceed the budget of {budget} tokens")
    prompt = "\n\n".join([intent, *blocks, "Do not load: " + (", ".join(exclude) if exclude else "(none)")]) + "\n"
    record = {"packet_sha256": packet_sha, "files": files, "estimated_tokens": total // 4, "budget_tokens": budget}
    return prompt, record
