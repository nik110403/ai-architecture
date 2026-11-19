"""Session freeze: a per-session record that denies file edits outside a root, and Bash, for that session only.

The record lives in a user-level directory (`freeze_dir()`, mirrored exactly by the OpenCode plugin) so it applies
wherever the session's working directory goes. There is no session-less or global freeze. The record is a
same-user file: it stops a well-behaved session, it is not a security boundary against a Bash-holding worker.
"""

import fnmatch
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.continuity import ledger as L  # noqa: E402

HARNESSES = ("claude-code", "opencode")
BASH_MODES = ("deny", "unscoped")
FILE_TOOLS = {"claude-code": ("Edit", "Write", "MultiEdit", "NotebookEdit"),
              "opencode": ("write", "edit", "patch", "apply_patch")}
BASH_TOOL = {"claude-code": "Bash", "opencode": "bash"}
RECORD_KEYS = ("version", "harness", "session_id", "root", "bash", "created_at")
NO_SESSION = "no session id: a freeze must name the session it applies to"
NO_ADAPTER = "unsupported: no enforcement adapter for this harness"
MAX_RECORD = 16 * 1024
PATH_KEY = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
PROTECTED_FILES = (Path(__file__).resolve().parent / "hooks" / "freeze_guard.py",)


def freeze_dir():
    """AI_ARCH_FREEZE_DIR (absolute), else $XDG_STATE_HOME/ai-architecture/freeze, else ~/.local/state/..."""
    env = os.environ.get("AI_ARCH_FREEZE_DIR")
    if env:
        if not os.path.isabs(env):
            raise L.Refused("AI_ARCH_FREEZE_DIR must be an absolute path")
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "ai-architecture" / "freeze"
    return Path(os.path.expanduser("~")) / ".local" / "state" / "ai-architecture" / "freeze"


def ensure_dir():
    path = freeze_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    L.check_dir(path, "freeze directory")
    return path


def record_path(directory, harness, session_id):
    digest = hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32]
    return Path(directory) / f"{harness}-{digest}.json"


def validate_record(record, harness=None, session_id=None):
    """The record's parts or ValueError. With harness/session_id given, the record must name exactly them."""
    if not isinstance(record, dict) or set(record) != set(RECORD_KEYS):
        raise ValueError("record keys")
    if record["version"] != 1 or isinstance(record["version"], bool):
        raise ValueError("record version")
    if record["harness"] not in HARNESSES or record["bash"] not in BASH_MODES:
        raise ValueError("record harness or bash")
    if not isinstance(record["session_id"], str) or not record["session_id"]:
        raise ValueError("record session_id")
    if not isinstance(record["root"], str) or not os.path.isabs(record["root"]) or "\x00" in record["root"]:
        raise ValueError("record root")
    if not isinstance(record["created_at"], str) or not L.ISO.fullmatch(record["created_at"]):
        raise ValueError("record created_at")
    if (harness is not None and record["harness"] != harness) or (
            session_id is not None and record["session_id"] != session_id):
        raise ValueError("record names another session")
    return record


def load_record(directory, harness, session_id):
    """The valid record, None when there is none; ValueError when the file exists but cannot be trusted."""
    path = record_path(directory, harness, session_id)
    try:
        data = L.read_file(path, MAX_RECORD)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"record unreadable: {error}")
    try:
        record = L.strict_loads(data)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"record is not strict JSON: {error}")
    return validate_record(record, harness, session_id)


def inside(root, target):
    """`target` (already resolved) is `root` or below it."""
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def is_settings_name(name):
    return fnmatch.fnmatch(name.casefold(), "settings*.json")


def is_settings(path):
    """A Claude Code settings file by name: a direct child `<any>/.claude/settings*.json`, case-folded."""
    parent, base = os.path.split(path)
    return os.path.basename(parent).casefold() == ".claude" and is_settings_name(base)


PRUNE = (".git", "node_modules", "__pycache__", ".venv")  # not walked: a `.claude` inside them is not protected


def claude_dirs(root):
    """Real paths of every `.claude` entry at or under `root`, found without following links.

    A symlink is found whether it is listed as a directory or (dangling) as a file; realpath resolves a missing target.
    """
    found = set()
    for here, names, files in os.walk(root, onerror=lambda e: None):
        names[:] = [n for n in names if n.casefold() not in PRUNE]
        for n in names:
            if n.casefold() == ".claude":
                found.add(os.path.realpath(os.path.join(here, n)))
        for n in files:
            if n.casefold() == ".claude" and os.path.islink(os.path.join(here, n)):
                found.add(os.path.realpath(os.path.join(here, n)))
    return found


def in_settings_dir(dirs, target):
    """`target` (resolved; a not-yet-existing tail is fine) is a settings*.json DIRECT child of a `.claude` real dir."""
    if is_settings_name(os.path.basename(target)) and os.path.dirname(target) in dirs:
        return True
    for d in dirs:  # an existing settings*.json entry (a symlink included) protects its destination too
        try:
            names = os.listdir(d)
        except OSError:
            continue
        if any(os.path.realpath(os.path.join(d, n)) == target for n in names if is_settings_name(n)):
            return True
    return False


def decide_tool(record, tool, path=None, cwd=None):
    """(allowed, reason) for one tool call of the frozen session."""
    harness = record["harness"]
    if tool == BASH_TOOL[harness]:
        if record["bash"] == "deny":
            return False, "frozen session: Bash is denied (it cannot be scoped to the root)"
        return True, "Bash unscoped: not enforced"
    if tool not in FILE_TOOLS[harness]:
        return True, "not a frozen tool"
    if not isinstance(path, str) or not path or "\x00" in path:
        return False, "frozen session: the call names no usable target path"
    if path.startswith("~") or "$" in path:  # a harness may expand these; the guard cannot know to what
        return False, "frozen session: a target path starting with ~ or containing $ is denied"
    try:
        if not os.path.isabs(path):
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                return False, "frozen session: relative target path without a working directory"
            path = os.path.join(cwd, path)
        lexical = os.path.normpath(path)
        targets = list(dict.fromkeys((os.path.realpath(path), os.path.realpath(lexical))))  # both readings
        root = os.path.realpath(record["root"])
        protected = [os.path.realpath(freeze_dir())] + [os.path.realpath(f) for f in PROTECTED_FILES]
        dirs = claude_dirs(root)
    except (OSError, ValueError, RuntimeError):
        return False, "frozen session: the target path does not resolve"
    except L.LedgerError:
        return False, "frozen session: the freeze directory cannot be consulted"
    if any(inside(p, t) for p in protected for t in targets):
        return False, "frozen session: the freeze record, its directory and the guard are protected"
    if is_settings(lexical) or any(is_settings(t) or in_settings_dir(dirs, t) for t in targets):
        return False, "frozen session: .claude/settings*.json under the root is protected"
    if all(inside(root, t) for t in targets):
        return True, "inside the frozen root"
    return False, f"frozen session: {tool} outside {root} is denied"


def any_record(directory):
    try:
        return any(name.endswith(".json") for name in os.listdir(directory))
    except OSError:
        return False


def decide_payload(raw, directory=None):
    """The Claude Code PreToolUse decision for a raw stdin payload: (allowed, reason)."""
    directory = directory if directory is not None else freeze_dir()
    try:
        payload = L.strict_loads(raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace"))
    except (ValueError, RecursionError):
        payload = None
    session = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session, str) or not session:
        if any_record(directory):
            return False, "frozen: the hook payload is unreadable and a freeze exists, so it cannot be ruled out"
        return True, "no freeze"
    try:
        record = load_record(directory, "claude-code", session)
    except ValueError as error:
        return False, f"frozen: the freeze record of this session is invalid ({error})"
    if record is None:
        return True, "no freeze for this session"
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        return False, "frozen: the hook payload names no tool"
    key = PATH_KEY.get(tool)
    allowed, reason = decide_tool(record, tool, tool_input.get(key) if key else None, payload.get("cwd"))
    if not allowed or tool not in FILE_TOOLS["claude-code"]:
        return allowed, reason
    for name, value in tool_input.items():  # any other path-like key must not point outside either
        if name != key and re.search(r"path|file", name, re.I):
            if not isinstance(value, str):
                return False, f"frozen: {name} is not a string"
            allowed, reason = decide_tool(record, tool, value, payload.get("cwd"))
            if not allowed:
                return False, reason
    return True, "inside the frozen root"


def deny_output(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# --- CLI -----------------------------------------------------------------------------

def enforcement(record):
    if record["harness"] == "claude-code":
        bash = ("Bash denied" if record["bash"] == "deny" else "Bash unscoped: not enforced")
        return ("Edit, Write, MultiEdit, NotebookEdit outside root denied; " + bash +
                " when hooks/freeze_guard.py is installed as a PreToolUse hook")
    bash = "bash denied" if record["bash"] == "deny" else "bash unscoped: not enforced"
    return ("write, edit, patch, apply_patch outside root denied; " + bash +
            " when the opencode/runtime plugin is loaded")


def describe(record):
    out = {**record, "enforcement": enforcement(record)}
    if record["harness"] == "opencode":
        out["session"] = ("unverified id: an OpenCode session id cannot be checked to exist; child or task sessions "
                          "have their own ids and are NOT frozen by this record")
    else:
        out["session"] = "subagent calls carry this session id and are frozen with it; a worker session is not"
    return out


def resolve_target(args):
    harness = args.harness or "claude-code"
    if harness not in HARNESSES:
        raise L.Refused(NO_ADAPTER)
    session = args.session or (os.environ.get("CLAUDE_CODE_SESSION_ID") if harness == "claude-code" else None)
    if not session:
        raise L.Refused(NO_SESSION)
    if not L.SESSION_ID.fullmatch(session):
        raise L.Refused("--session: the id must match [A-Za-z0-9_-]{1,128}")
    return harness, session


def write_record(directory, record):
    path = record_path(directory, record["harness"], record["session_id"])
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    data = (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def cli(args):
    """`freeze set|clear|status|check`: (exit code, payload)."""
    action = args.freeze_action
    if action == "set":
        harness, session = resolve_target(args)
        if args.bash not in BASH_MODES:
            raise L.Refused("--bash must be deny or unscoped")
        root = os.path.realpath(args.root)
        if not os.path.isdir(root):
            raise L.Refused("--root must be an existing directory")
        if root == os.sep or any(inside(root, os.path.realpath(p)) for p in (freeze_dir(), *PROTECTED_FILES)):
            raise L.Refused("--root must not be / or contain the freeze directory or the freeze guard")
        directory = ensure_dir()
        record = {"version": 1, "harness": harness, "session_id": session, "root": root, "bash": args.bash,
                  "created_at": L.now()}
        path = write_record(directory, record)
        return 0, {"record": str(path), **describe(record)}
    if action == "status" and not args.session:
        directory = freeze_dir()
        records, invalid = [], []
        for name in sorted(os.listdir(directory)) if directory.is_dir() else []:
            if not name.endswith(".json"):
                continue
            try:
                records.append(describe(validate_record(L.strict_loads(L.read_file(directory / name, MAX_RECORD)))))
            except (OSError, ValueError, RecursionError):
                invalid.append(name)
        return 0, {"freeze_dir": str(directory), "records": records, "invalid": invalid}
    if action != "clear" and not args.harness:  # status/check: a freeze of either harness counts
        return lookup_any(args, action)
    harness, session = resolve_target(args)
    directory = freeze_dir()
    if action == "clear":
        path = record_path(directory, harness, session)
        try:
            os.unlink(path)
            cleared = True
        except FileNotFoundError:
            cleared = False
        return 0, {"cleared": cleared, "harness": harness, "session_id": session}
    try:
        record = load_record(directory, harness, session)
    except ValueError as error:
        if action == "check":
            return 0, {"decision": "deny", "reason": f"the freeze record is invalid ({error})"}
        return 0, {"harness": harness, "session_id": session, "frozen": True, "invalid": str(error)}
    if action == "status":
        return 0, {"harness": harness, "session_id": session, "frozen": record is not None,
                   **(describe(record) if record else {})}
    if record is None:
        return 0, {"decision": "allow", "reason": "no freeze for this session"}
    if not args.tool:
        raise L.Refused("freeze check needs --tool")
    allowed, reason = decide_tool(record, args.tool, args.path)
    return 0, {"decision": "allow" if allowed else "deny", "reason": reason}


def lookup_any(args, action):
    """`status`/`check` without --harness: look the session up under every harness; a deny from any record wins."""
    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        raise L.Refused(NO_SESSION)
    if not L.SESSION_ID.fullmatch(session):
        raise L.Refused("--session: the id must match [A-Za-z0-9_-]{1,128}")
    if action == "check" and not args.tool:
        raise L.Refused("freeze check needs --tool")
    directory = freeze_dir()
    found = []
    for harness in HARNESSES:
        try:
            record = load_record(directory, harness, session)
        except ValueError as error:
            found.append((harness, None, str(error)))
            continue
        if record is not None:
            found.append((harness, record, None))
    if action == "status":
        if not found:
            return 0, {"harness": "claude-code", "session_id": session, "frozen": False}
        harness, record, error = found[0]
        if record is None:
            return 0, {"harness": harness, "session_id": session, "frozen": True, "invalid": error}
        return 0, {"harness": harness, "session_id": session, "frozen": True, **describe(record)}
    if not found:
        return 0, {"decision": "allow", "reason": "no freeze for this session"}
    for harness, record, error in found:
        if record is None:
            return 0, {"decision": "deny", "reason": f"the freeze record is invalid ({error})"}
        allowed, reason = decide_tool(record, args.tool, args.path)
        if not allowed:
            return 0, {"decision": "deny", "reason": reason}
    return 0, {"decision": "allow", "reason": reason}
