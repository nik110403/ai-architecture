"""Shared config loader for the ai-architecture-gates hooks. Stdlib only.

Layers, in order:
  1. defaults.json in the plugin root (ships with the plugin; the baseline).
  2. ONE user-scope override file: $AIARCH_CONFIG if set, else
     ~/.config/ai-architecture/gates.json.

Project scope is NEVER read for configuration: a repository you open cannot loosen,
disable or re-point a gate by shipping a config file. (The destructive-command guard
additionally reads <cwd>/.claude/destructive-patterns.json, which can only ADD
deny/ask regexes -- it is consulted after the built-in rules and cannot suppress one.)

Merge rules: lists are unioned (an override can add entries, never remove a default);
dict values are replaced per key.

Fail closed, visibly: an override file that exists but cannot be parsed, is not a JSON object,
or carries a known key of the wrong type is NOT silently ignored. load() keeps the shipped
defaults for everything and records the problem under the "_config_error" key; every hook
reports it (the destructive guard denies each Bash call until the file is fixed). A missing
override file is normal and means "defaults".

The override path must name a regular file (checked with stat, following symlinks): a FIFO or
device is refused as an invalid config. A path that is, or whose final component symlinks to, /dev/std*, /dev/fd/*,
/proc/<pid>/fd/* or /proc/self/* is refused (checked on the raw path and along the final component's symlink chain, not
only after realpath), so it cannot swallow the hook payload.
Hooks read their stdin payload FIRST (init()), and only then load the config.

An unknown top-level key (not starting with "_") is an invalid config too: a misspelled key
must not silently leave a protection switched off.

AIARCH_CONFIG is ignored when it points inside the current project directory (hook cwd or
CLAUDE_PROJECT_DIR), because a project's .claude/settings.json can set environment variables.
"""
import atexit
import io
import json
import os
import re
import stat
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def home():
    """The account's home directory from the passwd database, NOT $HOME: a project's
    .claude/settings.json can set env vars, and HOME would move every protected root."""
    seam = os.environ.get("GATES_TEST_PW_HOME")
    if seam:
        # Test seam: honoured only while tests/run.sh holds its flag file (inside the plugin tree,
        # which a project's env settings cannot write) and the flag names this exact directory.
        fd = -1
        try:
            flag = os.path.join(PLUGIN_ROOT, "tests", ".pwseam-active")
            # O_NOFOLLOW|O_NONBLOCK: a symlink or FIFO planted at the flag path cannot redirect or block the read.
            fd = os.open(flag, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode):
                # flag format "<pid> <path>": honoured only while that pid is a live tests/run.sh process
                pid_s, _, named = os.read(fd, 4096).decode("utf-8", "replace").strip().partition(" ")
                pid = int(pid_s)
                os.kill(pid, 0)
                with open("/proc/%d/cmdline" % pid, "rb") as cf:
                    argv = cf.read().split(b"\0")
                a1 = os.path.join(os.readlink("/proc/%d/cwd" % pid), argv[1].decode("utf-8", "replace")) if len(argv) > 1 else ""
                if os.path.realpath(a1) != os.path.realpath(os.path.join(PLUGIN_ROOT, "tests", "run.sh")):
                    raise ValueError("seam pid is not tests/run.sh")
                d = os.stat(seam)
                # the named dir must exist and be owned by us (no mtime check: dir mtime moves as tests write into it)
                if named == seam and stat.S_ISDIR(d.st_mode) and d.st_uid == os.getuid():
                    return seam
        except (OSError, ValueError):
            pass
        finally:
            if fd >= 0:
                os.close(fd)
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.environ.get("HOME") or os.path.expanduser("~")


def _inside(path, root):
    try:
        path, root = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([path, root]) == root
    except Exception:
        return False


def project_dirs():
    dirs = [os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()]
    return [d for d in dirs if d]


def user_config_path():
    default = os.path.join(home(), ".config", "ai-architecture", "gates.json")
    env = os.environ.get("AIARCH_CONFIG")
    if not env:
        return default
    if any(_inside(env, d) for d in project_dirs()):
        return default  # project-scope pick of the config file: ignored
    return env


_STREAM_RE = re.compile(r"/dev/std(?:in|out|err)\Z|/(?:proc/[^/]+(?:/task/[^/]+)?|dev)/fd(?:/|\Z)|/proc/self(?:/|\Z)|/proc/thread-self(?:/|\Z)")


def _is_stream_path(path):
    """True if the raw path, any hop of the final component's symlink chain, or the final realpath names a standard stream
    or fd entry (/dev/std*, /dev/fd/*, /proc/<pid>/fd/*, /proc/self/*). realpath alone is not enough:
    /dev/stdin resolves to whatever file the fd currently points at."""
    cur, seen = os.path.abspath(path), 0
    while True:
        if _STREAM_RE.match(cur) or _STREAM_RE.match(os.path.normpath(cur)):
            return True
        if not os.path.islink(cur) or seen > 40:
            break
        seen += 1
        cur = os.path.abspath(os.path.join(os.path.dirname(cur), os.readlink(cur)))
    return bool(_STREAM_RE.match(os.path.realpath(path)))


def _read(path, required):
    """Returns (dict, error). A missing optional file is ({}, None)."""
    if _is_stream_path(path):
        return {}, f"config {path} refused: refers to a standard stream or file descriptor \u2014 fix it"
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}, (f"config {path} invalid: file missing \u2014 fix it" if required else None)
    except OSError as exc:
        return {}, f"config {path} invalid: {type(exc).__name__}: {exc} \u2014 fix it"
    if not stat.S_ISREG(st.st_mode):
        return {}, f"config {path} invalid: not a regular file \u2014 fix it"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {}, f"config {path} invalid: {type(exc).__name__}: {exc} \u2014 fix it"
    if not isinstance(data, dict):
        return {}, f"config {path} invalid: top level must be a JSON object \u2014 fix it"
    return data, None


def _type_error(path, base, over):
    for key, val in over.items():
        if key.startswith("_"):
            continue
        if key not in base:
            return f"config {path} invalid: unknown key {key!r} \u2014 fix it"
        want = base[key]
        if isinstance(want, dict):
            ok = isinstance(val, dict) and all(isinstance(v, int) and not isinstance(v, bool)
                                               for v in val.values()) if key == "edit_budget_models" \
                else isinstance(val, dict)
        elif isinstance(want, list):
            ok = isinstance(val, list) and all(isinstance(v, str) for v in val)
        else:
            ok = isinstance(val, type(want))
        if not ok:
            return (f"config {path} invalid: key {key!r} must be {type(want).__name__}"
                    f"{' of strings' if isinstance(want, list) else ''} \u2014 fix it")
    return None


def _merge(base, over):
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, list) and isinstance(out.get(key), list):
            out[key] = list(out[key]) + [v for v in val if v not in out[key]]
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(val)
            out[key] = merged
        elif key in out and type(val) is type(out[key]):
            out[key] = val
        elif key not in out:
            out[key] = val
    return out


_CACHE = {}


def load():
    if "cfg" in _CACHE:
        return _CACHE["cfg"]
    cfg, err = _read(os.path.join(PLUGIN_ROOT, "defaults.json"), True)
    path = user_config_path()
    over, oerr = _read(path, False)
    err = err or oerr or (_type_error(path, cfg, over) if over else None)
    if err:
        cfg["_config_error"] = "ai-architecture-gates: " + err  # defaults stay in force
    else:
        cfg = _merge(cfg, over)
    _CACHE["cfg"] = cfg
    return cfg


def config_error():
    return load().get("_config_error")


def warn_if_invalid():
    """Non-blocking hooks: say so on stderr, keep the (restrictive) defaults."""
    err = config_error()
    if err:
        print(err, file=sys.stderr)
    return err


def role(name):
    """Agent/role names match after the last ':' so a namespaced name such as
    'ai-architecture:verifier' compares equal to 'verifier'."""
    return str(name or "").rsplit(":", 1)[-1].strip()


def trusted_role(name):
    """Like role(), but a namespaced name only reduces to its short name when the namespace is
    in config trusted_plugin_prefixes; otherwise the full name is returned (and so matches no role)."""
    name = str(name or "").strip()
    if ":" not in name:
        return name
    prefix, short = name.rsplit(":", 1)
    trusted = load().get("trusted_plugin_prefixes") or []
    return short.strip() if prefix.strip() in trusted else name


def expand(path):
    path = str(path)
    if path == "~" or path.startswith("~/"):
        path = home() + path[1:]
    return path.rstrip("/") or "/"


def state_dir():
    d = os.environ.get("CLAUDE_HOOK_STATE_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    return d or os.path.join(home(), ".cache", "ai-architecture", "gates")


# ---- hook I/O: payload first, fail closed, config errors on a user-visible channel ----

_FAIL = {
    "pretool": lambda m: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": m}},
    "stop": lambda m: {"decision": "block", "reason": m},
    "session": lambda m: {"systemMessage": m},
}


class _Buffered(io.TextIOBase):
    """Collects stdout so a config error can be merged into the hook's single JSON reply."""
    def __init__(self):
        self.parts = []

    def writable(self):
        return True

    def write(self, text):
        self.parts.append(text)
        return len(text)


def _flush_with_config_error(buf, real):
    text = "".join(buf.parts)
    try:
        err = config_error()
    except Exception as exc:  # never let reporting crash the hook
        err = f"ai-architecture-gates: config check failed: {type(exc).__name__}"
    if err:
        try:
            obj = json.loads(text) if text.strip() else {}
            if isinstance(obj, dict):
                prior = obj.get("systemMessage")
                obj["systemMessage"] = f"{prior} | {err}" if prior else err
                text = json.dumps(obj) + "\n"
        except Exception:
            pass
    real.write(text)
    real.flush()


def init(kind):
    """Read and parse the stdin payload (must be a JSON object). On failure answer fail-closed
    for this hook kind ('pretool' deny, 'stop' block, 'session' systemMessage) and exit 0.
    Afterwards stdout is buffered so an invalid user config is added to the reply as a
    systemMessage in every hook. Call BEFORE load(): the payload is read before any config."""
    msg = "ai-architecture-gates: hook payload could not be parsed \u2014 failing closed"
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError("payload is not a JSON object")
    except Exception:
        print(json.dumps(_FAIL[kind](msg)))
        sys.exit(0)
    real, buf = sys.stdout, _Buffered()
    sys.stdout = buf

    def _crash(etype, exc, tb):
        # An uncaught exception would exit 1, which Claude Code treats as "allow". Answer
        # deny/block (SessionStart: systemMessage) and exit 0. A Stop hook that already
        # blocked once (stop_hook_active) must not block again, or the turn never ends.
        try:
            if kind == "stop" and data.get("stop_hook_active"):
                out = {}
            else:
                out = _FAIL[kind](f"ai-architecture-gates: hook crashed ({etype.__name__}) \u2014 failing closed")
            real.write(json.dumps(out) + "\n")
            real.flush()
        finally:
            os._exit(0)
    sys.excepthook = _crash
    atexit.register(lambda: (setattr(sys, "stdout", real), _flush_with_config_error(buf, real)))
    return data
