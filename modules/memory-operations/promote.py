"""Reviewed promotion: stage a redacted candidate, approve its exact content, apply it to curated memory, roll it back.

Apply order: snapshot MEMORY.md, create the note with O_EXCL|O_NOFOLLOW, memcheck fix-index, write the receipt. A failure
at any step undoes what was done, so no note exists without a receipt. Approval is a record, not authentication: it is
refused from agent sessions, nothing more.
"""

import importlib.util
import json
import os
import re
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


common = _load("memops_common", HERE / "common.py")
redact = _load("memops_redact", HERE / "redact.py")
curated = _load("memops_curated", HERE.parent / "memory" / "curated.py")
Refuse = common.Refuse

CAND_ID = r"cand-[0-9a-f]{12}"
RECEIPT_ID = r"rcpt-\d{8}T\d{6}Z-[0-9a-f]{6}"
NAME = r"[a-z0-9][a-z0-9_-]{0,62}"
TYPES = ("user", "feedback", "project", "reference")
CONFIRM_TYPES = ("user", "feedback")
NOTE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def append_receipt(outfd, lane, record):
    return common.append_receipt(outfd, lane, record)


def candidate_sha(cand):
    """The hash approval is bound to, recomputed from the content every time (a stored sha256 field is never trusted)."""
    try:
        material = {k: cand[k] for k in ("name", "type", "description", "body")}
    except (KeyError, TypeError):
        raise Refuse("candidate is malformed") from None
    if not all(isinstance(v, str) for v in material.values()):
        raise Refuse("candidate is malformed")
    return common.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode())


def note_bytes(cand):
    return (f"---\nname: {cand['name']}\ndescription: {cand['description']}\nmetadata:\n  type: {cand['type']}\n---\n"
            f"{cand['body']}\n").encode()


def load_candidate(outfd, cid):
    common.check_pattern(cid, CAND_ID, "candidate id")
    try:
        cdir = common.subdir(outfd, "candidates", cid)
        try:
            cand = json.loads(common.read_at(cdir, "candidate.json"))
        finally:
            os.close(cdir)
    except (OSError, ValueError):
        raise Refuse("candidate is missing or unreadable") from None
    if not isinstance(cand, dict):
        raise Refuse("candidate is malformed")
    candidate_sha(cand)
    return cand


# ---- stage ----------------------------------------------------------------------------------------------------

def stage(args, outfd):
    try:
        data = json.loads(common.read_file(args.from_recall, 1 << 20))
        items = data["candidates"]
        item = next(i for i in items if i.get("id") == args.candidate_id)
        excerpt = item["excerpt"]
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
        raise Refuse("recall JSON has no such candidate") from None
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise Refuse("candidate has no excerpt")
    common.check_pattern(args.name, NAME, "--name")
    if args.type not in TYPES:
        raise Refuse("--type must be one of: " + ", ".join(TYPES))
    count = 0
    fields = {}
    for key, text in (("body", excerpt), ("description", args.description)):
        text, n = redact.redact(text)  # redaction first: nothing unredacted is ever built into a candidate or written
        count += n
        fields[key] = redact.observe.light(text, 2000 if key == "body" else 200)
    if not fields["description"]:
        raise Refuse("--description is empty")
    cand = {"name": args.name, "type": args.type, **fields}
    sha = candidate_sha(cand)
    cid = "cand-" + sha[:12]
    record = {"id": cid, **cand, "redacted_count": count, "sha256": sha}
    cdir = common.subdir(outfd, "candidates", cid)
    try:
        try:
            os.unlink("candidate.json", dir_fd=cdir)
        except FileNotFoundError:
            pass
        common.write_new_at(cdir, "candidate.json", (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(cdir)
    append_receipt(outfd, "promotion", {"event": "stage", "id": cid, "sha256": sha, "redacted_count": count})
    return {"id": cid, "sha256": sha, "redacted_count": count}


# ---- approve --------------------------------------------------------------------------------------------------

def approve(args, outfd):
    blocked = common.agent_env()
    if blocked:
        raise Refuse(f"approval is refused from agent sessions ({blocked} is set)")
    cand = load_candidate(outfd, args.id)
    sha = candidate_sha(cand)
    if args.sha != sha:
        raise Refuse("--sha does not match the candidate content")
    if cand["type"] in CONFIRM_TYPES and args.type_confirm != cand["type"]:
        raise Refuse(f"type {cand['type']} needs --type-confirm {cand['type']}")
    common.check_pattern(args.approver, r"[A-Za-z0-9._-]{1,64}", "--approver")
    append_receipt(outfd, "promotion", {"event": "approve", "id": args.id, "sha256": sha, "approver": args.approver})
    return {"id": args.id, "sha256": sha, "approver": args.approver}


# ---- apply ----------------------------------------------------------------------------------------------------

def promoted(records, cid):
    """True when the candidate has an apply receipt that no later rollback receipt cancels."""
    live = False
    for record in records:
        if record.get("id") == cid and record.get("event") == "apply":
            live = True
        elif record.get("id") == cid and record.get("event") == "rollback":
            live = False
    return live


def target_free(rootfd, slug, file):
    return curated._kind(rootfd, [slug, "memory", file]) is None


def single_root(spec):
    roots = curated.parse_roots([spec])
    return roots[0]


def write_note(memfd, file, data):
    try:
        fd = os.open(file, NOTE_FLAGS, 0o600, dir_fd=memfd)
    except FileExistsError:
        raise Refuse("collision: the target appeared while applying", 2) from None
    except OSError:
        raise Refuse("cannot create the note", 3) from None
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def restore_index(memfd, before, lock_existed):
    """Put MEMORY.md back byte-identical (keeping its mode) and remove a lock file we caused."""
    try:
        mode = os.stat("MEMORY.md", dir_fd=memfd, follow_symlinks=False).st_mode & 0o7777
        current = common.read_at(memfd, "MEMORY.md")
    except OSError:
        mode, current = 0o644, None
    if current != before:
        common.replace_at(memfd, "MEMORY.md", before, mode)
    if not lock_existed:
        try:
            os.unlink("MEMORY.md.lock", dir_fd=memfd)
        except FileNotFoundError:
            pass


def undo_apply(memfd, file, before, lock_existed):
    try:
        os.unlink(file, dir_fd=memfd)
    except FileNotFoundError:
        pass
    restore_index(memfd, before, lock_existed)


def apply(args, outfd, out):
    common.check_pattern(args.id, CAND_ID, "--id")
    cand = load_candidate(outfd, args.id)
    sha = candidate_sha(cand)
    records = common.read_receipts(outfd, "promotion")
    approvals = [r for r in records if r.get("event") == "approve" and r.get("id") == args.id]
    if not approvals or approvals[-1].get("sha256") != sha:
        raise Refuse("no approval for this exact content")
    if promoted(records, args.id):
        raise Refuse("already promoted: roll the earlier promotion back first")
    label, path = single_root(args.root)
    found = curated.DIRARG.fullmatch(args.dir)
    if not found:
        raise Refuse("--dir must be <slug>/memory relative to the root")
    slug, file = found.group(1), cand["name"] + ".md"
    rootfd = curated._open_root(path)
    try:
        if curated._kind(rootfd, [slug, "memory"]) != "dir":
            raise Refuse("--dir is not a plain directory inside the root (a symlink is refused)", 3)
        if curated._kind(rootfd, [slug, "memory", "MEMORY.md"]) != "file":
            raise Refuse("MEMORY.md is missing or a symlink", 3)
        if not target_free(rootfd, slug, file):
            raise Refuse("collision: the target note already exists or is a symlink", 2)
        lock_existed = curated._kind(rootfd, [slug, "memory", "MEMORY.md.lock"]) is not None
        before = curated._read(rootfd, [slug, "memory", "MEMORY.md"])
        memfd = curated._open_nofollow(rootfd, [slug, "memory"], directory=True)
        receipt_id = f"rcpt-{common.now().replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
        pdir = common.subdir(outfd, "promotion", receipt_id)
        try:
            common.write_new_at(pdir, "MEMORY.md.before", before)
        finally:
            os.close(pdir)
        written = False
        try:
            write_note(memfd, file, note_bytes(cand))
            written = True
            audit = os.path.join(out, "promotion", "audit.log")
            result = curated.run_memcheck(path, "--fix-index", "--apply", "--dir", args.dir, "--audit-log", audit,
                                          "--actor", "human")
            after = curated._read(rootfd, [slug, "memory", "MEMORY.md"])
            if result.returncode != 0 or not re.search(rb"\]\(" + re.escape(file.encode()) + rb"\)", after):
                raise Refuse("memcheck did not index the note; undone", 3)
            if not lock_existed:
                try:
                    os.unlink("MEMORY.md.lock", dir_fd=memfd)
                except FileNotFoundError:
                    pass
            note = curated._read(rootfd, [slug, "memory", file])
            try:
                append_receipt(outfd, "promotion", {
                    "event": "apply", "id": args.id, "receipt_id": receipt_id, "sha256": sha, "target": f"{slug}/memory/{file}",
                    "note_sha256": common.sha256(note), "index_before_sha256": common.sha256(before),
                    "index_after_sha256": common.sha256(after), "approver": approvals[-1].get("approver")})
            except OSError:
                raise Refuse("receipt write failed; the promotion was undone", 1) from None
        except BaseException:
            if written:
                undo_apply(memfd, file, before, lock_existed)
            _drop_snapshot(outfd, receipt_id)
            raise
        finally:
            os.close(memfd)
    finally:
        os.close(rootfd)
    return {"applied": True, "id": args.id, "receipt_id": receipt_id, "target": f"{slug}/memory/{file}"}


def _drop_snapshot(outfd, receipt_id):
    try:
        pdir = common.subdir(outfd, "promotion", receipt_id)
        try:
            os.unlink("MEMORY.md.before", dir_fd=pdir)
        finally:
            os.close(pdir)
        pfd = common.subdir(outfd, "promotion")
        try:
            os.rmdir(receipt_id, dir_fd=pfd)
        finally:
            os.close(pfd)
    except OSError:
        pass


# ---- rollback -------------------------------------------------------------------------------------------------

def rollback(args, outfd):
    common.check_pattern(args.receipt, RECEIPT_ID, "--receipt")
    records = common.read_receipts(outfd, "promotion")
    applied = [r for r in records if r.get("event") == "apply" and r.get("receipt_id") == args.receipt]
    if not applied:
        raise Refuse("no such apply receipt")
    record = applied[-1]
    if not promoted(records, record["id"]):
        raise Refuse("already rolled back")
    slug, _, file = record["target"].split("/", 2)
    label, path = single_root(args.root)
    rootfd = curated._open_root(path)
    try:
        try:
            note = curated._read(rootfd, [slug, "memory", file])
            index = curated._read(rootfd, [slug, "memory", "MEMORY.md"])
            memfd = curated._open_nofollow(rootfd, [slug, "memory"], directory=True)
        except OSError:
            raise Refuse("note or MEMORY.md is missing or a symlink; rollback refused", 3) from None
        try:
            if common.sha256(note) != record["note_sha256"] or common.sha256(index) != record["index_after_sha256"]:
                raise Refuse("the note or MEMORY.md changed since the promotion; rollback refused", 3)
            pdir = common.subdir(outfd, "promotion", args.receipt)
            try:
                snapshot = common.read_at(pdir, "MEMORY.md.before")
            finally:
                os.close(pdir)
            if common.sha256(snapshot) != record["index_before_sha256"]:
                raise Refuse("the stored MEMORY.md snapshot does not match its receipt", 3)
            os.unlink(file, dir_fd=memfd)
            restore_index(memfd, snapshot, True)
        finally:
            os.close(memfd)
    finally:
        os.close(rootfd)
    append_receipt(outfd, "promotion", {"event": "rollback", "id": record["id"], "receipt_id": args.receipt})
    return {"rolled_back": True, "receipt_id": args.receipt}
