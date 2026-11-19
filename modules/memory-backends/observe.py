#!/usr/bin/env python3
"""Observation producer: turn an opted-in hook event stream into stored, redacted, extractive observations.

Stdlib only. No model call, no network. Reads --events (a file or `-` for stdin), optional --redact-file and the store
directory; writes only inside the store (obs.db, mode 0600). Observations are not curated memory and are never
written to it. See README.md.
"""

import argparse
import contextlib
import datetime
import errno
import hashlib
import io
import json
import os
import posixpath
import re
import sqlite3
import stat
import sys
import unicodedata

MAX_EVENTS_FILE = 8 << 20
MAX_STDIN = 1 << 20
MAX_SUMMARY = 400
DB_NAME = "obs.db"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
KINDS = {"tool": ("PostToolUse",), "stop": ("Stop", "SubagentStop")}
BUILTIN_REDACTIONS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    r"\bsk-[A-Za-z0-9_\-]{8,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"(?i)\b(?:password|passwd|pwd)\s*[=:]\s*\S+",
]


class Refuse(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


# ---- filesystem entry points (mirrors modules/memory/curated.py: no symlink follow, fd-relative) ---------------

def check_path(path):
    if not isinstance(path, str) or not path or "\0" in path or ".." in path.split("/"):
        raise Refuse("path is empty or contains '..'")


def open_store(path):
    """Return a directory fd for an existing, non-symlinked, user-owned 0700 store that carries OPT-IN."""
    check_path(path)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise Refuse("store is a symlink")
        fd = os.open(path, _DIR)
    except OSError:
        raise Refuse("store is missing or not a directory") from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise Refuse("store must be owned by you with mode 0700")
    try:
        if not stat.S_ISREG(os.stat("OPT-IN", dir_fd=fd, follow_symlinks=False).st_mode):
            raise OSError
    except OSError:
        os.close(fd)
        raise Refuse("store is not opted in: create an OPT-IN file in it") from None
    return fd


def exists_nofollow(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except OSError:
        return False


def read_regular(path, cap):
    """Read a regular file (final component never a symlink) up to cap bytes; more than cap is refused."""
    check_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise Refuse("file is missing or a symlink") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refuse("not a regular file")
        data = b""
        while len(data) <= cap:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > cap:
        raise Refuse("input too large")
    return data


def probe_fts5():
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE temp.p USING fts5(a)")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


# ---- text -----------------------------------------------------------------------------------------------------

def light(text, cap=MAX_SUMMARY):
    """Strip controls and zero-width characters, collapse whitespace. Brackets stay ([REDACTED] is stored literally)."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Zl", "Zp"))
    return " ".join(text.split())[:cap]


class Redactor:
    def __init__(self, extra=()):
        self.patterns = [re.compile(p) for p in BUILTIN_REDACTIONS] + list(extra)
        self.count = 0

    def __call__(self, text):
        for pattern in self.patterns:
            text, n = pattern.subn("[REDACTED]", text)
            self.count += n
        return text


def load_user_patterns(path):
    out = []
    for line in read_regular(path, 64 << 10).decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(out) >= 64 or len(line) > 500:
            raise Refuse("redact file has too many or too long patterns")
        try:
            out.append(re.compile(line))
        except re.error:
            raise Refuse("redact file has an invalid regex") from None
    return out


def rel_path(path, scope):
    """A path inside scope becomes scope-relative; anything else becomes <outside>."""
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(scope, path))
    if full == scope:
        return "."
    if full.startswith(scope.rstrip("/") + "/"):
        return full[len(scope.rstrip("/")) + 1:]
    return "<outside>"


# One token per match. A closing tag (</code>) is kept whole and a scheme URL (not file:) keeps its authority; otherwise an absolute (/x), home (~/x, ~user/x) or
# file: path (any case, optional //host) is scrubbed. A directory segment may contain single spaces (it must be
# followed by "/"), so "/a/My Documents/plan.txt" is one token; prose after the last "/" is left alone. The
# lookbehind has no "<" or ">" so paths after redirects and markup are still scrubbed.
PATH_SRC = (
    r"(?P<tag></[A-Za-z][\w:\-]*>)|(?<![\w.])(?P<url>(?!(?i:file|vscode|sqlite|unix):)[A-Za-z][A-Za-z0-9+.\-]*://[^\s<>\"']*)"
    r"|(?<![\w./])(?P<path>(?:(?i:file|vscode|sqlite|unix):(?://[\w.\-]*)?)?(?:~[\w\-]*)?/(?:[\w.@+\-]+(?: [\w.@+\-]+)*/+)*"
    r"[\w.@+\-]*)")
PATH_TOKEN = re.compile(PATH_SRC)
# Same token grammar for a URL tail, minus the predecessor lookbehinds: glued text ("1_/x", "a./x") must not hide a path there.
TAIL_TOKEN = re.compile(PATH_SRC.replace(r"(?<![\w.])", "").replace(r"(?<![\w./])", ""))
FILE_PREFIX = re.compile(r"(?i)(?:file|vscode|sqlite|unix):(?://[\w.\-]*)?")
# Inside a scheme URL only the scheme and a strict authority stay verbatim. The tail (path, query, fragment, glued text)
# goes through the same scrubber as text outside URLs, so ordinary URL paths may become <outside> (deliberate).
URL_AUTH = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[A-Za-z0-9.\-:@\[\]%_]*")

def shown_path(token, scope):
    token = FILE_PREFIX.sub("", token, count=1) if FILE_PREFIX.match(token) else token
    if token.startswith("~"):
        return "<outside>"
    return rel_path(token, scope)


def scrub_url(url, scope):
    n = URL_AUTH.match(url).end()
    return url[:n] + scrub_paths(url[n:], scope, TAIL_TOKEN)


def scrub_paths(text, scope, token=PATH_TOKEN):
    return token.sub(lambda m: m.group("tag") or (scrub_url(m.group("url"), scope) if m.group("url")
                                                       else shown_path(m.group("path"), scope)), text)


def first_sentence(text):
    text = text.strip()
    return re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0][:200]


def event_kind(event):
    name = event.get("hook_event_name")
    for kind, names in KINDS.items():
        if name in names:
            return kind
    return None


def safe_tool(name):
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", name) else ""


def summarize(event, scope, redact=lambda s: s):
    """Deterministic extractive summary: tool name, first sentence of the event's own text, file paths."""
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    kind = event_kind(event) or "event"
    text = ""
    for source in (event.get("summary"), event.get("last_assistant_message"), event.get("message"),
                   tool_input.get("description")):
        if isinstance(source, str) and source.strip():
            text = source
            break
    sentence = first_sentence(scrub_paths(redact(light(text, 4000)), scope))
    paths = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            shown = shown_path(redact(light(value, 500)), scope)
            if shown not in paths:
                paths.append(shown)
    head = redact(safe_tool(event.get("tool_name"))) or kind
    out = head + (f": {sentence}" if sentence else "")
    if paths:
        out += " files: " + ", ".join(paths[:5])
    return redact(light(out))


def parse_ts(value):
    """None when absent; a datetime when valid; raises ValueError when unusable."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    if isinstance(value, str):
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
    raise ValueError


def in_scope(cwd, scope):
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
        return False
    cwd = posixpath.normpath(cwd)
    return cwd == scope or cwd.startswith(scope.rstrip("/") + "/")


def parse_events(data):
    text = data.decode("utf-8", "replace")
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            return [whole], 0
    except ValueError:
        pass
    events, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(item, dict):
            events.append(item)
        else:
            bad += 1
    return events, bad


# ---- store ----------------------------------------------------------------------------------------------------

def open_db(dirfd, store):
    """Create (0600) or reuse obs.db without following a symlink, then connect."""
    try:
        fd = os.open(DB_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    except OSError:
        raise Refuse("db is a symlink or unreadable") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refuse("db is not a regular file")
    finally:
        os.close(fd)
    db = sqlite3.connect(os.path.join(store, DB_NAME), timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("CREATE TABLE IF NOT EXISTS obs(id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, "
               "tool_name TEXT NOT NULL, ts TEXT NOT NULL, captured_at TEXT NOT NULL, summary TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS obs_captured ON obs(captured_at)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(summary, content='obs', content_rowid='rowid')")
    return db


def prune(db, now, retain_days, max_rows):
    cut = (now - datetime.timedelta(days=retain_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doomed = {r[0]: r[1] for r in db.execute("SELECT rowid, summary FROM obs WHERE captured_at < ?", (cut,))}
    for rowid, summary in db.execute("SELECT rowid, summary FROM obs ORDER BY captured_at DESC, rowid DESC "
                                     "LIMIT -1 OFFSET ?", (max_rows,)):
        doomed[rowid] = summary
    for rowid, summary in doomed.items():
        db.execute("INSERT INTO obs_fts(obs_fts, rowid, summary) VALUES('delete', ?, ?)", (rowid, summary))
        db.execute("DELETE FROM obs WHERE rowid = ?", (rowid,))
    if doomed:
        db.execute("INSERT INTO obs_fts(obs_fts) VALUES('optimize')")
    return len(doomed)


def project_name(scope):
    return re.sub(r"[^A-Za-z0-9._-]", "-", posixpath.basename(scope.rstrip("/")))[:64] or "root"


def capture(args, stdin=None):
    if args.summarizer != "extractive":
        raise Refuse("no model summarizer in this collection")
    kinds = [k for k in args.kinds.split(",") if k]
    if not kinds or any(k not in KINDS for k in kinds):
        raise Refuse("--kinds must be a comma list from: " + ",".join(KINDS))
    if args.retain_days < 0 or args.max_rows < 1:
        raise Refuse("--retain-days must be >= 0 and --max-rows >= 1")
    check_path(args.scope)
    if not args.scope.startswith("/"):
        raise Refuse("--scope must be an absolute directory path")
    scope = posixpath.normpath(args.scope)
    if not probe_fts5():
        raise Refuse("SQLite FTS5 is not available in this Python; install a Python whose sqlite3 has FTS5")
    now = datetime.datetime.now(datetime.timezone.utc)
    if args.now:
        try:
            now = parse_ts(args.now)
        except ValueError:
            raise Refuse("--now must be an ISO timestamp") from None
    store = args.store
    dirfd = open_store(store)
    if "AI_ARCH_JOB_MARKER" in os.environ:  # dispatched worker session: observe nothing, touch nothing
        os.close(dirfd)
        return {"captured": 0, "dropped_scope": 0, "dropped_child": 0, "dropped_future": 0, "dropped_invalid": 0,
                "redacted": 0, "pruned": 0, "child_session": True}
    try:
        if exists_nofollow(dirfd, "DISABLED"):
            return None
        if args.events == "-":
            data = (stdin or sys.stdin.buffer).read(MAX_STDIN + 1)
            if len(data) > MAX_STDIN:
                raise Refuse("stdin input too large")
        else:
            data = read_regular(args.events, MAX_EVENTS_FILE)
        extra = load_user_patterns(args.redact_file) if args.redact_file else []
        redact = Redactor(extra)
        events, bad = parse_events(data)
        result = {"captured": 0, "dropped_scope": 0, "dropped_child": 0, "dropped_future": 0,
                  "dropped_invalid": bad, "redacted": 0, "pruned": 0}
        rows = []
        for event in events:
            kind = event_kind(event)
            if event.get("agent_id"):
                result["dropped_child"] += 1
                continue
            if kind not in kinds or not in_scope(event.get("cwd"), scope):
                result["dropped_scope"] += 1
                continue
            try:
                ts = parse_ts(event.get("ts"))
            except (ValueError, OverflowError, OSError):
                result["dropped_invalid"] += 1
                continue
            if ts is not None and ts > now + datetime.timedelta(days=1):
                result["dropped_future"] += 1
                continue
            summary = summarize(event, scope, redact)
            tool = redact(safe_tool(event.get("tool_name")))
            project = project_name(scope)
            ts_id = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""
            oid = "obs-" + hashlib.sha256("\0".join((project, kind, ts_id, summary)).encode()).hexdigest()[:24]
            rows.append((oid, project, kind, tool, ts_id, summary))
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        db = open_db(dirfd, store)
        try:
            for oid, project, kind, tool, ts_id, summary in rows:
                cur = db.execute("INSERT OR IGNORE INTO obs VALUES(?,?,?,?,?,?,?)",
                                 (oid, project, kind, tool, ts_id or stamp, stamp, summary))
                if cur.rowcount == 1:
                    db.execute("INSERT INTO obs_fts(rowid, summary) VALUES(?, ?)", (cur.lastrowid, summary))
                    result["captured"] += 1
            result["pruned"] = prune(db, now, args.retain_days, args.max_rows)
            db.commit()
        finally:
            db.close()
        result["redacted"] = redact.count
        return result
    finally:
        os.close(dirfd)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="store observations from a hook event stream")
    cap.add_argument("--store", required=True)
    cap.add_argument("--events", required=True, help="JSONL file, or - for stdin (one hook object or JSONL, 1 MiB)")
    cap.add_argument("--scope", required=True, help="absolute project directory; events outside it are dropped")
    cap.add_argument("--kinds", default="tool,stop")
    cap.add_argument("--retain-days", type=int, default=30)
    cap.add_argument("--max-rows", type=int, default=5000)
    cap.add_argument("--redact-file")
    cap.add_argument("--summarizer", default="extractive")
    cap.add_argument("--hook", action="store_true",
                     help="hook mode: every refusal or error exits 0 with one stderr line and no stdout, never blocking")
    cap.add_argument("--now", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    hook = "--hook" in (sys.argv[1:] if argv is None else argv)
    try:
        if hook:  # argparse errors must not print usage or exit 2 in a hook
            with contextlib.redirect_stderr(io.StringIO()):
                args = build_parser().parse_args(argv)
        else:
            args = build_parser().parse_args(argv)
        try:
            result = capture(args)
        except sqlite3.Error:
            raise Refuse("store database error") from None
    except Refuse as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 0 if hook else exc.code
    except SystemExit as exc:
        if hook:
            print("refused: bad arguments", file=sys.stderr)
            return 0
        return exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # hook mode never blocks the session
        if not hook:
            raise
        print(f"refused: {type(exc).__name__}", file=sys.stderr)
        return 0
    if hook:
        return 0
    if result is None:
        print("capture disabled")
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
