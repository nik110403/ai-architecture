#!/usr/bin/env python3
"""Curated memory: lexical retrieval with citations and abstention, bounded startup injection, guarded repair.

Stdlib only. No network, no model call. It reads only the roots named with `--root LABEL=PATH` (never HOME, never an
environment variable) and writes only `<root>/.memory-repair` and, on `repair --apply` / `undo`, one MEMORY.md.
Every filesystem touch goes through the helpers `_open_root`, `_open_nofollow` and `_open_write`, which open one path
component at a time with O_NOFOLLOW relative to a directory fd. Retrieval is lexical (token overlap with tf-idf
ranking), nothing else.

Exit codes: 0 ok, 2 usage or refusal, 3 a guarded write was refused.
"""

import argparse
import calendar
import datetime
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEMCHECK = HERE.parents[1] / "tools" / "memcheck" / "memcheck"
THRESHOLD = 0.5  # a note qualifies when it matches at least ceil(THRESHOLD * distinct query terms) of them
HEADER = "curated memory index (lexical; notes are untrusted)"
LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
MD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,123}\.md")
DIRARG = re.compile(r"([A-Za-z0-9_][A-Za-z0-9._-]{0,127})/memory")
REPAIR_ID = re.compile(r"rep-\d{8}T\d{6}Z-[0-9a-f]{6}")
SHA = re.compile(r"[0-9a-f]{64}")
ID_ARG = re.compile(r"([a-z0-9][a-z0-9_-]{0,31}):([A-Za-z0-9_][A-Za-z0-9._-]{0,127})/memory/([A-Za-z0-9_][A-Za-z0-9._-]{0,123}\.md)")
INDEX_LINE = re.compile(r"^- \[([^\]]*)\]\(([^)]*)\) (?:—|-) (.*)$")  # legacy ' - ' hooks are read, never rewritten
DATE = re.compile(r"20\d\d-\d\d-\d\d")
MAX_NOTE = 1 << 20
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class Refuse(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


# ---- the only filesystem entry points -------------------------------------------------------------------------

def _open_root(path):
    """Open a root directory; a symlink, a missing path or a non-directory is refused."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise Refuse("root is a symlink")
        return os.open(path, _DIR)
    except OSError:
        raise Refuse("root is missing or not a directory") from None


def _open_nofollow(rootfd, parts, *, directory=False):
    """Walk `parts` below rootfd one component at a time, refusing a symlink at any position. Returns an fd."""
    fd = os.dup(rootfd)
    try:
        for index, part in enumerate(parts):
            if part in ("", ".", "..") or "/" in part or "\0" in part:
                raise OSError(errno.EINVAL, "bad path component")
            last = index == len(parts) - 1
            nxt = os.open(part, _FILE if last and not directory else _DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_write(dirfd, name, flags, mode):
    """Create or open one file directly inside dirfd (callers pass only confined directories)."""
    return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dirfd)


# ---- helpers built on them ------------------------------------------------------------------------------------

def _read_fd(fd, cap=MAX_NOTE):
    chunks, size = [], 0
    try:
        while size < cap:
            chunk = os.read(fd, min(65536, cap - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _read(rootfd, parts):
    return _read_fd(_open_nofollow(rootfd, parts))


def _kind(rootfd, parts):
    """'symlink' / 'dir' / 'file' / 'other' / None (missing); never follows a link, never raises on a missing path."""
    try:
        dirfd = _open_nofollow(rootfd, parts[:-1], directory=True) if parts[:-1] else os.dup(rootfd)
    except OSError:
        return None
    try:
        mode = os.stat(parts[-1], dir_fd=dirfd, follow_symlinks=False).st_mode
    except OSError:
        return None
    finally:
        os.close(dirfd)
    return "symlink" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"


def _listdir(rootfd, parts):
    fd = _open_nofollow(rootfd, parts, directory=True)
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def _write_new(dirfd, name, data, mode=0o600):
    fd = _open_write(dirfd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def emit(data):
    if isinstance(data, bytes):
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(data)
        sys.stdout.flush()


def sanitize(text, cap):
    """Port of tools/memcheck/lib/sanitize.sh: controls, zero-width/bidi, [](), backticks, whitespace, cap."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Zl", "Zp"))
    text = re.sub(r"[\[\]()`]", "", text)
    return " ".join(text.split())[:cap]


def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def default_stale_before(today=None):
    """Today minus six calendar months. Approximates memcheck's `date -d '-6 months'`, which overflows a short
    month into the next one where this clamps to the month end."""
    today = today or datetime.date.today()
    month, year = today.month - 6, today.year
    if month < 1:
        month, year = month + 12, year - 1
    return datetime.date(year, month, min(today.day, calendar.monthrange(year, month)[1])).isoformat()


# ---- corpus ---------------------------------------------------------------------------------------------------

class Note:
    def __init__(self, label, slug, file, raw):
        self.label, self.slug, self.file = label, slug, file
        self.cite = f"{label}:{slug}/memory/{file}"
        self.text = raw.decode("utf-8", "replace")
        self.lines = self.text.split("\n")
        start, self.name = 0, ""
        if self.lines[0] == "---":
            for end in range(1, len(self.lines)):
                if self.lines[end] == "---":
                    start = end + 1
                    for line in self.lines[1:end]:
                        found = re.match(r"name:\s*(.*)$", line)
                        if found:
                            self.name = found.group(1).strip().strip('"\'')
                    break
        self.body_start = start
        self.body_sha = hashlib.sha256("\n".join(self.lines[start:]).strip().encode()).hexdigest()
        dates = DATE.findall(self.text)
        self.newest = max(dates) if dates else None
        self.hook = ""
        self.index_line = None


class Corpus:
    def __init__(self):
        self.notes, self.skipped, self.indexes, self.flags = [], [], {}, {}

    def skip(self, path, reason):
        self.skipped.append({"path": path, "reason": reason})


def parse_roots(specs):
    roots, seen = [], set()
    for spec in specs or []:
        label, sep, path = spec.partition("=")
        if not sep or not path or not LABEL.fullmatch(label) or label in seen:
            raise Refuse("--root needs unique LABEL=PATH (label [a-z0-9_-])")
        seen.add(label)
        roots.append((label, path))
    if not roots:
        raise Refuse("at least one --root LABEL=PATH is required (there is no default and no environment fallback)")
    return roots


def load_corpus(roots):
    corpus = Corpus()
    for label, path in roots:
        try:
            rootfd = _open_root(path)
        except Refuse as error:
            raise Refuse(f"root {label}: {error}") from None
        try:
            _load_root(corpus, label, rootfd)
        finally:
            os.close(rootfd)
    by_cite = {n.cite: n for n in corpus.notes}
    for note in corpus.notes:
        note.flags = []
    corpus.by_cite = by_cite
    return corpus


def _load_root(corpus, label, rootfd):
    for slug in sorted(os.listdir(rootfd)):
        if not NAME.fullmatch(slug):
            continue
        kind = _kind(rootfd, [slug])
        if kind == "symlink":
            corpus.skip(f"{label}:{slug}", "symlink")
            continue
        if kind != "dir":
            continue
        mem_kind = _kind(rootfd, [slug, "memory"])
        if mem_kind == "symlink":
            corpus.skip(f"{label}:{slug}/memory", "symlink")
            continue
        if mem_kind != "dir":
            continue
        files = _listdir(rootfd, [slug, "memory"])
        entries = []
        if "MEMORY.md" in files:
            ikind = _kind(rootfd, [slug, "memory", "MEMORY.md"])
            if ikind == "symlink":
                corpus.skip(f"{label}:{slug}/memory/MEMORY.md", "symlink")
            elif ikind == "file":
                try:
                    text = _read(rootfd, [slug, "memory", "MEMORY.md"]).decode("utf-8", "replace")
                except OSError:
                    corpus.skip(f"{label}:{slug}/memory/MEMORY.md", "unreadable")
                    text = ""
                for number, line in enumerate(text.split("\n"), 1):
                    found = INDEX_LINE.match(line)
                    if found:
                        entries.append((number, found.group(1), found.group(2), found.group(3)))
        corpus.indexes[(label, slug)] = entries
        hooks = {file: (number, hook) for number, _, file, hook in entries}
        for file in files:
            if file == "MEMORY.md" or not file.endswith(".md") or "sync-conflict" in file or not MD.fullmatch(file):
                continue
            cite = f"{label}:{slug}/memory/{file}"
            kind = _kind(rootfd, [slug, "memory", file])
            if kind == "symlink":
                corpus.skip(cite, "symlink")
                continue
            if kind != "file":
                continue
            try:
                raw = _read(rootfd, [slug, "memory", file])
            except OSError:
                corpus.skip(cite, "unreadable")
                continue
            note = Note(label, slug, file, raw)
            if file in hooks:
                note.index_line, note.hook = hooks[file]
            corpus.notes.append(note)


def annotate(corpus, stale_before):
    names = {}
    for note in corpus.notes:
        if note.name:
            names.setdefault(note.name, []).append(note)
    for note in corpus.notes:
        note.flags = []
        note.conflicts = sorted(o.cite for o in names.get(note.name, []) if o is not note and o.body_sha != note.body_sha)
        if note.newest is not None and note.newest < stale_before:
            note.flags.append("stale")
        if note.conflicts:
            note.flags.append("conflict")


# ---- query ----------------------------------------------------------------------------------------------------

def best_line(note, terms):
    top, found = 0, None
    for number, line in enumerate(note.lines, 1):
        count = len(terms & set(tokens(line)))
        if count > top:
            top, found = count, number
    if found is not None:
        return note.cite, found, note.lines[found - 1]
    if note.index_line is not None:
        return f"{note.label}:{note.slug}/memory/MEMORY.md", note.index_line, note.hook
    return note.cite, 1, note.lines[0]


def run_query(corpus, terms, max_tokens, by_id=None):
    notes = corpus.notes
    if by_id is not None:
        note = corpus.by_cite.get(by_id)
        ranked = []
        if note is not None:
            number = next((i for i in range(note.body_start, len(note.lines)) if note.lines[i].strip()), None)
            number = (number if number is not None else 0) + 1
            ranked.append((0.0, note, note.cite, number, note.lines[number - 1]))
    else:
        df = {t: 0 for t in terms}
        docs = []
        for note in notes:
            counts = {}
            for token in tokens(note.text) + tokens(note.hook):
                if token in df:
                    counts[token] = counts.get(token, 0) + 1
            docs.append(counts)
            for token in counts:
                df[token] += 1
        need = math.ceil(THRESHOLD * len(terms))
        ranked = []
        for note, counts in zip(notes, docs):
            if len(counts) < need:
                continue
            score = sum(tf * math.log(1 + len(notes) / df[t]) for t, tf in counts.items())
            cite, number, line = best_line(note, terms)
            ranked.append((score, note, cite, number, line))
        ranked.sort(key=lambda r: (-r[0], r[2], r[3]))
    hits, used, limit = [], 0, 4 * max_tokens
    for score, note, cite, number, line in ranked:
        citation, snippet = f"{cite}#L{number}", sanitize(line, 160)
        size = len(citation.encode()) + len(snippet.encode())
        if used + size > limit:
            break
        used += size
        hits.append({"citation": citation, "score": round(score, 6), "snippet": snippet,
                     "flags": list(note.flags), "conflicts": list(note.conflicts)})
    return {"retrieval": "lexical", "abstained": not ranked, "hits": hits, "omitted": len(ranked) - len(hits),
            "budget_used_tokens": math.ceil(used / 4), "token_estimate": "utf8_bytes/4", "skipped": corpus.skipped}


def render_text(result, searched):
    if result["abstained"]:
        return f"NO MATCH (lexical, {searched} notes searched)\n"
    out = []
    for hit in result["hits"]:
        out.append(f"{hit['citation']}  score={hit['score']}  {' '.join('[' + f + ']' for f in hit['flags'])}".rstrip())
        out.append(f"    {hit['snippet']}")
    if result["omitted"]:
        out.append(f"+{result['omitted']} more (budget)")
    return "\n".join(out) + "\n"


def cmd_query(args):
    roots = parse_roots(args.root)
    if args.max_tokens < 1:
        raise Refuse("--max-tokens must be at least 1")
    stale_before = args.stale_before or default_stale_before()
    try:
        datetime.date.fromisoformat(stale_before)
    except ValueError:
        raise Refuse("--stale-before must be an ISO date") from None
    terms, by_id = set(), None
    if args.id is not None:
        found = ID_ARG.fullmatch(args.id)
        if not found or found.group(1) not in {label for label, _ in roots}:
            raise Refuse("--id must be LABEL:<slug>/memory/<file>.md for a declared root")
        by_id = args.id
    else:
        terms = set(tokens(args.q))
        if not terms:
            raise Refuse("--q has no searchable terms")
    corpus = load_corpus(roots)
    annotate(corpus, stale_before)
    result = run_query(corpus, terms, args.max_tokens, by_id)
    if args.format == "text":
        emit(render_text(result, len(corpus.notes)))
    else:
        emit(json.dumps(result, indent=2) + "\n")
    return 0


# ---- injection ------------------------------------------------------------------------------------------------

def parse_scopes(values, roots):
    labels = {label for label, _ in roots}
    scopes = []
    for value in values or []:
        label, _, slug = value.partition(":")
        if label not in labels or not NAME.fullmatch(slug):
            raise Refuse("--scope must be LABEL:<slug> for a declared root")
        scopes.append((label, slug))
    return scopes


def build_block(lines, max_bytes):
    best = None
    size = 0
    for count in range(len(lines) + 1):
        if count:
            size += len(lines[count - 1].encode()) + 1
            if size > max_bytes:
                break
        more = len(lines) - count
        body = ["```text", HEADER, *lines[:count], *([f"+{more} more"] if more else []), "```"]
        text = "\n".join(body) + "\n"
        if len(text.encode()) <= max_bytes:
            best = text
    return best or f"```text\n{HEADER}\n```\n"


def inject_text(roots, scopes, max_bytes, stale_before=None):
    corpus = load_corpus(roots)
    annotate(corpus, stale_before or default_stale_before())
    lines = []
    for label, slug in scopes:
        for _, title, file, hook in corpus.indexes.get((label, slug), []):
            if not MD.fullmatch(file):
                continue
            note = corpus.by_cite.get(f"{label}:{slug}/memory/{file}")
            tags = "".join(f" [{flag}]" for flag in (note.flags if note else []))
            lines.append(f"- {sanitize(title, 80)} ({file}): {sanitize(hook, 120)}{tags}")
    return build_block(lines, max_bytes)


def max_bytes_type(value):
    number = int(value)
    if not 256 <= number <= 8192:
        raise argparse.ArgumentTypeError("--max-bytes must be 256..8192")
    return number


def cmd_inject(args):
    roots = parse_roots(args.root)
    scopes = parse_scopes(args.scope, roots)
    if not scopes:
        raise Refuse("--scope LABEL:<slug> is required")
    emit(inject_text(roots, scopes, args.max_bytes))
    return 0


# ---- repair, undo, audit --------------------------------------------------------------------------------------

def run_memcheck(rootpath, *extra):
    if not MEMCHECK.is_file():
        raise Refuse("needs tools/memcheck/memcheck next to modules/ (use the full collection checkout)")
    return subprocess.run([str(MEMCHECK), "--root", ".", *extra], cwd=rootpath, capture_output=True, env=dict(os.environ))


def single_root(args):
    roots = parse_roots(args.root)
    if len(roots) != 1:
        raise Refuse("this command takes exactly one --root")
    return roots[0]


def sha_of(data):
    return hashlib.sha256(data).hexdigest()


def cmd_repair(args):
    label, path = single_root(args)
    found = DIRARG.fullmatch(args.dir)
    if not found:
        raise Refuse("--dir must be <slug>/memory relative to the root")
    slug = found.group(1)
    rootfd = _open_root(path)
    try:
        if _kind(rootfd, [slug, "memory"]) != "dir":
            raise Refuse("--dir is not a plain directory inside the root")
        index_kind = _kind(rootfd, [slug, "memory", "MEMORY.md"])
        if index_kind == "symlink":
            raise Refuse("MEMORY.md is a symlink", 3)
        if not args.apply:
            result = run_memcheck(path, "--fix-index", *(["--shorten"] if args.shorten else []), "--dir", args.dir)
            emit(result.stdout)
            return 0 if result.returncode == 0 else 3
        if index_kind != "file":
            raise Refuse("no MEMORY.md to repair")
        return _apply(args, rootfd, path, slug)
    finally:
        os.close(rootfd)


def _apply(args, rootfd, path, slug):
    kind = _kind(rootfd, [".memory-repair"])
    if kind == "symlink" or kind not in (None, "dir"):
        raise Refuse(".memory-repair is not a plain directory", 3)
    if kind is None:
        os.mkdir(".memory-repair", 0o700, dir_fd=rootfd)
    before = _read(rootfd, [slug, "memory", "MEMORY.md"])
    repair_id = f"rep-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    repfd = _open_nofollow(rootfd, [".memory-repair"], directory=True)
    try:
        os.mkdir(repair_id, 0o700, dir_fd=repfd)
        snapfd = _open_nofollow(repfd, [repair_id], directory=True)
        try:
            _write_new(snapfd, "MEMORY.md.before", before)
            result = run_memcheck(path, "--fix-index", *(["--shorten"] if args.shorten else []), "--apply", "--dir", args.dir,
                                  "--audit-log", os.path.join(".memory-repair", "audit.log"), "--actor", "human")
            after = _read(rootfd, [slug, "memory", "MEMORY.md"]) if result.returncode == 0 else before
            if result.returncode != 0 or after == before:
                os.unlink("MEMORY.md.before", dir_fd=snapfd)
                os.rmdir(repair_id, dir_fd=repfd)
                if result.returncode != 0:
                    raise Refuse("memcheck refused the repair; snapshot removed", 3)
                emit(result.stdout)
                emit("no change\n")
                return 0
            manifest = {"repair_id": repair_id, "dir": args.dir, "before_sha256": sha_of(before), "after_sha256": sha_of(after)}
            _write_new(snapfd, "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        finally:
            os.close(snapfd)
    finally:
        os.close(repfd)
    emit(result.stdout)
    emit(f"repair_id: {repair_id}\n")
    return 0


def cmd_undo(args):
    label, path = single_root(args)
    if not REPAIR_ID.fullmatch(args.repair_id):
        raise Refuse("--repair-id has the form rep-YYYYMMDDTHHMMSSZ-xxxxxx")
    rootfd = _open_root(path)
    try:
        try:
            raw = _read(rootfd, [".memory-repair", args.repair_id, "manifest.json"])
            manifest = json.loads(raw)
            found = DIRARG.fullmatch(manifest["dir"]) if isinstance(manifest, dict) and isinstance(manifest.get("dir"), str) else None
            if (not found or manifest.get("repair_id") != args.repair_id or not SHA.fullmatch(str(manifest.get("before_sha256")))
                    or not SHA.fullmatch(str(manifest.get("after_sha256")))):
                raise Refuse("manifest is not trustworthy", 3)
            if manifest.get("undone") is True:
                emit("already undone\n")
                return 0
            slug = found.group(1)
            snapshot = _read(rootfd, [".memory-repair", args.repair_id, "MEMORY.md.before"])
            if sha_of(snapshot) != manifest["before_sha256"]:
                raise Refuse("snapshot does not match the manifest", 3)
            memfd = _open_nofollow(rootfd, [slug, "memory"], directory=True)
        except (OSError, ValueError, KeyError):
            raise Refuse("repair record missing or unsafe", 3) from None
        try:
            try:
                lock = _open_write(memfd, "MEMORY.md.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            except OSError:
                raise Refuse("lock file refused", 3) from None
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    current = _read(rootfd, [slug, "memory", "MEMORY.md"])
                except OSError:
                    raise Refuse("MEMORY.md unreadable or a symlink", 3) from None
                if sha_of(current) != manifest["after_sha256"]:
                    raise Refuse("MEMORY.md changed since the repair; undo refused", 3)
                temp = f".MEMORY.md.undo-{secrets.token_hex(4)}"
                _write_new(memfd, temp, snapshot, 0o600)
                os.replace(temp, "MEMORY.md", src_dir_fd=memfd, dst_dir_fd=memfd)
            finally:
                os.close(lock)
        finally:
            os.close(memfd)
        manifest["undone"] = True
        repfd = _open_nofollow(rootfd, [".memory-repair", args.repair_id], directory=True)
        try:
            temp = f"manifest.json.tmp-{secrets.token_hex(4)}"
            _write_new(repfd, temp, (json.dumps(manifest, indent=2) + "\n").encode())
            os.replace(temp, "manifest.json", src_dir_fd=repfd, dst_dir_fd=repfd)
        finally:
            os.close(repfd)
    finally:
        os.close(rootfd)
    emit(f"undone {args.repair_id}\n")
    return 0


def cmd_audit(args):
    roots = parse_roots(args.root)
    stale_before = args.stale_before or default_stale_before()
    corpus = load_corpus(roots)
    annotate(corpus, stale_before)
    reports = []
    for label, path in roots:
        result = run_memcheck(path)
        text = result.stdout.decode("utf-8", "replace")
        reports.append(text if len(roots) == 1 else f"## root {label}\n{text}")
    groups = {}
    for note in corpus.notes:
        if note.conflicts:
            groups.setdefault(note.name, set()).update([note.cite, *note.conflicts])
    stale = [{"citation": n.cite, "date": n.newest} for n in corpus.notes if "stale" in n.flags]
    emit(json.dumps({"memcheck_report": "\n".join(reports), "stale": stale,
                     "conflicts": [{"name": k, "citations": sorted(v)} for k, v in sorted(groups.items())],
                     "skipped": corpus.skipped, "review": "human or reviewer seat; no model call"}, indent=2) + "\n")
    return 0


# ---- CLI ------------------------------------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="curated.py", description="Curated memory (lexical retrieval; reads only declared roots).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, scope=False):
        p.add_argument("--root", action="append", required=True, metavar="LABEL=PATH", help="declared root (repeatable; no default)")

    q = sub.add_parser("query", help="lexical search with citations; abstains when too few terms match")
    common(q)
    group = q.add_mutually_exclusive_group(required=True)
    group.add_argument("--q", help="query text")
    group.add_argument("--id", help="fetch one note: LABEL:<slug>/memory/<file>.md")
    q.add_argument("--max-tokens", type=int, default=800, help="budget; 1 token = 4 UTF-8 bytes of citation+snippet")
    q.add_argument("--stale-before", help="ISO date (default: six calendar months ago)")
    q.add_argument("--format", choices=("json", "text"), default="json")
    q.set_defaults(func=cmd_query)

    i = sub.add_parser("inject", help="print a bounded, fenced index block for declared scopes")
    common(i)
    i.add_argument("--scope", action="append", required=True, metavar="LABEL:SLUG")
    i.add_argument("--max-bytes", type=max_bytes_type, default=2048)
    i.set_defaults(func=cmd_inject)

    r = sub.add_parser("repair", help="memcheck index repair; dry-run unless --apply (apply is snapshotted)")
    common(r)
    r.add_argument("--dir", required=True, metavar="SLUG/memory")
    r.add_argument("--shorten", action="store_true")
    r.add_argument("--apply", action="store_true")
    r.set_defaults(func=cmd_repair)

    u = sub.add_parser("undo", help="restore the snapshot of one applied repair")
    common(u)
    u.add_argument("--repair-id", required=True)
    u.set_defaults(func=cmd_undo)

    a = sub.add_parser("audit", help="review packet: memcheck report plus stale and conflict candidates (no model call)")
    common(a)
    a.add_argument("--stale-before")
    a.set_defaults(func=cmd_audit)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Refuse as error:
        sys.stderr.write(f"curated: {error}\n")
        return error.code


if __name__ == "__main__":
    sys.exit(main())
