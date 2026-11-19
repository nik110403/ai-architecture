"""Companion side of the discovery inbox: a versioned manifest, a git-archive sandbox and the one spawner.

Stdlib only. A companion is a program set you installed yourself; this file never imports it. Only the three command
roles (handoff, set, list) can be spawned, always through the knowledge-adapters spawner (no shell, scrubbed env,
deadline, output cap) and always behind a Python-level network denial that is verified by a real connect attempt.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
INTERFACE = "handoff-brief/1"
ROLES = ("handoff", "set", "list")
INTERP = "/usr/bin/python3"
KEYS = {"schema_version", "interface", "name", "root", "rev", "commands", "support", "dispositions", "eligible_status"}
PUBLIC = ("adopted", "harvested", "deferred", "rejected")
REL = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}")
STATUS = re.compile(r"[a-z][a-z-]{0,30}")
REV = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,100}")
GIT_AT_IMPORT = shutil.which("git")
SPAWNS = 0  # role spawns in this process (the verification probe is not counted)

DENY = """import socket
def _deny(*a, **k):
    raise OSError('network denied')
socket.socket.connect = socket.socket.connect_ex = _deny
socket.create_connection = socket.getaddrinfo = _deny
"""
RUN = """import runpy, sys
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name='__main__')
"""
PROBE = "try:\n    socket.socket().connect(('192.0.2.1', 9))\nexcept OSError:\n    print('denied')\n"


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


graph = _load("kad_graph", HERE.parent / "knowledge-adapters" / "graph.py")


class Refuse(Exception):
    """A controlled refusal: exit 2, nothing spawned."""

    code = 2


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _rel(value, label):
    if (not isinstance(value, str) or not REL.fullmatch(value) or ".." in value.split("/") or "//" in value
            or value.endswith("/")):
        raise Refuse(f"manifest: {label} must be a plain relative path without '..'")
    return value


def declared_files(manifest):
    return [*(manifest["commands"][role] for role in ROLES), *manifest["support"]]


def load_manifest(path):
    """Read and validate a companion manifest. Every defect is a refusal (exit 2)."""
    try:
        if os.stat(path).st_size > 65536:
            raise Refuse("manifest: file is larger than 64 KiB")
        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, ValueError) as error:
        raise Refuse(f"manifest: unreadable ({type(error).__name__})") from None
    if not isinstance(data, dict) or set(data) != KEYS:
        raise Refuse("manifest: keys must be exactly " + ", ".join(sorted(KEYS)))
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise Refuse("manifest: schema_version must be 1")
    if data["interface"] != INTERFACE:
        raise Refuse(f"manifest: interface must be {INTERFACE}")
    if not isinstance(data["name"], str) or not TOKEN.fullmatch(data["name"]):
        raise Refuse("manifest: name must be a short token")
    if not isinstance(data["root"], str) or not os.path.isabs(data["root"]) or "\0" in data["root"]:
        raise Refuse("manifest: root must be an absolute path")
    if not isinstance(data["rev"], str) or not REV.fullmatch(data["rev"]):
        raise Refuse("manifest: rev must be a plain git revision")
    if not isinstance(data["commands"], dict) or set(data["commands"]) != set(ROLES):
        raise Refuse("manifest: commands must hold exactly " + ", ".join(ROLES))
    for role in ROLES:
        _rel(data["commands"][role], f"commands.{role}")
    if not isinstance(data["support"], list):
        raise Refuse("manifest: support must be a list")
    for item in data["support"]:
        _rel(item, "support entry")
    if len(set(declared_files(data))) != len(declared_files(data)):
        raise Refuse("manifest: a file is declared twice")
    disp = data["dispositions"]
    if (not isinstance(disp, dict) or set(disp) != set(PUBLIC)
            or not all(isinstance(v, str) and STATUS.fullmatch(v) for v in disp.values())
            or len(set(disp.values())) != len(PUBLIC)):
        raise Refuse("manifest: dispositions must map " + ", ".join(PUBLIC) + " to four distinct status tokens")
    if not isinstance(data["eligible_status"], str) or not STATUS.fullmatch(data["eligible_status"]) \
            or data["eligible_status"] in disp.values():
        raise Refuse("manifest: eligible_status must be a status token that is not a disposition value")
    return data


def _git(root, *args):
    exe = shutil.which("git") or GIT_AT_IMPORT
    if not exe:
        raise Refuse("git was not found")
    with tempfile.TemporaryDirectory() as home:
        env = {"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0"}
        try:
            done = subprocess.run([os.path.abspath(exe), "-c", "core.hooksPath=/dev/null", "-C", root, *args], env=env,
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise Refuse("git did not run") from None
    if done.returncode != 0:
        raise Refuse("git refused the companion root or revision")
    return done.stdout


def _checked_root(manifest):
    root = os.path.realpath(manifest["root"])
    top = _git(root, "rev-parse", "--show-toplevel").decode().strip() if os.path.isdir(root) else ""
    if not top or os.path.realpath(top) != root:
        raise Refuse("manifest: root is not a git work tree")
    return root


def fetch(manifest):
    """Return (archive bytes, identity). The identity is the git tree of each command dir plus the file hashes."""
    root = _checked_root(manifest)
    rev = manifest["rev"]
    _git(root, "rev-parse", "--verify", rev + "^{commit}")
    files = declared_files(manifest)
    blob = _git(root, "archive", "--format=tar", rev, "--", *files)
    trees = {}
    for rel in files:
        directory = str(PurePosixPath(rel).parent)
        spec = f"{rev}:{directory}" if directory != "." else f"{rev}^{{tree}}"
        trees[directory] = _git(root, "rev-parse", spec).decode().strip()
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isreg() or member.name not in files:
                raise Refuse("companion archive holds an entry that is not a declared regular file")
            hashes[member.name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    if set(hashes) != set(files):
        raise Refuse("a declared file is missing from the companion revision")
    return blob, {"name": manifest["name"], "trees": dict(sorted(trees.items())), "files": dict(sorted(hashes.items()))}


def build_sandbox(manifest, sandbox):
    """Install the declared files (and an empty data/repos) into the sandbox. Re-running checks, never overwrites."""
    if not hasattr(tarfile, "data_filter"):
        raise Refuse("this Python lacks the tarfile data filter")
    blob, identity = fetch(manifest)
    sandbox = Path(sandbox)
    if sandbox.is_symlink() or (sandbox.exists() and not sandbox.is_dir()):
        raise Refuse("sandbox must be a real directory")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            regular = [m for m in archive.getmembers() if m.isreg()]
            archive.extractall(tmp, members=regular, filter="data")
        if sandbox.exists() and sandbox.stat().st_uid != os.getuid():
            raise Refuse("sandbox is owned by someone else")
        sandbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        for rel in declared_files(manifest):
            dest = sandbox / rel
            if dest.is_symlink():
                raise Refuse("sandbox holds a symlink where a declared file belongs")
            if dest.exists():
                if hashlib.sha256(dest.read_bytes()).hexdigest() != identity["files"][rel]:
                    raise Refuse("sandbox does not match the companion revision")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(tmp) / rel, dest)
    (sandbox / "data" / "repos").mkdir(parents=True, exist_ok=True)
    return identity


class Runner:
    """Spawns only the three command roles, only after the network denial was checked by a real connect attempt."""

    def __init__(self, manifest, sandbox, probe=PROBE, timeout_s=30):
        self.manifest, self.sandbox, self.probe, self.timeout_s = manifest, Path(sandbox), probe, timeout_s
        self.isolation = None
        self.count = 0

    def attest(self):
        if self.isolation is None:
            if not os.access(INTERP, os.X_OK):
                raise Refuse("the system interpreter was not found")
            state, out = graph.run_child([INTERP, "-B", "-I", "-c", DENY + self.probe], [], self.timeout_s)
            if state != "ok" or out.decode("utf-8", "replace").strip() != "denied":
                raise Refuse("network denial could not be verified; nothing was spawned")
            self.isolation = "net-denied"
        return self.isolation

    def spawn(self, role, args):
        global SPAWNS
        if role not in ROLES:
            raise Refuse(f"role {role!r} is not allowed; only {', '.join(ROLES)}")
        self.attest()
        script = self.sandbox / self.manifest["commands"][role]
        SPAWNS += 1
        self.count += 1
        state, out = graph.run_child([INTERP, "-B", "-I", "-c", DENY + RUN, str(script)], [str(a) for a in args], self.timeout_s)
        return state, out.decode("utf-8", "replace")


ROW = re.compile(r"^\s+(\S+) → (\S+)\s+\[(written|unchanged|conflict)\]\s*$")
PLACED = re.compile(r"^\s+(\S+) → (.+?)\s+\[([^\]]+)\]\s*$")


def parse_handoff(text):
    """Parse the companion's own handoff report: staged rows and placements. Unknown lines are ignored."""
    staged, rows, placements, placing = 0, [], [], False
    for line in text.splitlines():
        count = re.match(r"(\d+) brief\(s\) staged", line)
        if count:
            staged = int(count.group(1))
        elif "placement(s) applied" in line:
            placing = True
        elif placing and PLACED.match(line):
            repo, dest, result = PLACED.match(line).groups()
            placements.append({"repo": repo, "dest": dest, "result": result})
        elif not placing and ROW.match(line):
            src, dst, result = ROW.match(line).groups()
            rows.append({"src": src, "dst": dst, "result": result})
    return {"staged": staged, "rows": rows, "placements": placements}

