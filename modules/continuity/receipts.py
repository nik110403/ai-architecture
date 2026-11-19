"""Checkpoint receipts, hashed evidence and the READY decision (stdlib only).

READY is a conjunction: every condition is evaluated, every failing one is listed, and a condition that cannot be
evaluated counts as failing. Unknown usage of the handing-off session is never ready.
"""

import calendar
import hashlib
import importlib.util
import os
import re
import stat
import time
from pathlib import Path

from modules.continuity import ledger as L
from modules.continuity import resume
from modules.continuity import usage as U

MAX_EVIDENCE = 32
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_REASONS = 40
_PACKET = None


def _packet():
    """`safe_parts` and `target_stat` of the context packet script, loaded by path (it is not a package)."""
    global _PACKET
    if _PACKET is None:
        path = L.ROOT / "skills" / "context" / "scripts" / "context_packet.py"
        spec = importlib.util.spec_from_file_location("continuity_context_packet", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PACKET = module
    return _PACKET


def hash_ref(root, ref):
    """(sha256, bytes) when `ref` names a regular file inside `root` (relative, no `..`, no symlink); None for any
    other ref, which is kept as literal text. Raises OSError when the file exists but cannot be hashed."""
    packet = _packet()
    try:
        parts = packet.safe_parts(ref)
    except ValueError:
        return None
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            info = packet.target_stat(root_fd, ref)
        except ValueError:
            return None
    finally:
        os.close(root_fd)
    if info is None:
        return None
    fd = os.open(os.path.join(root, *parts), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        now = os.fstat(fd)
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("not the file that was inspected")
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return digest.hexdigest(), total
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise OSError("file too large to hash")
            digest.update(chunk)
    finally:
        os.close(fd)


PATH_SHAPED = re.compile(r"[^\s:]*/[^\s]*|[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,7}")


def path_shaped(ref):
    """A ref that reads as a file path (a slash, or name.ext, and no whitespace): a mistyped one must not pass as text."""
    return PATH_SHAPED.fullmatch(ref) is not None and "://" not in ref


def evidence_item(root, ref):
    if not isinstance(ref, str) or not 1 <= len(ref) <= L.MAX_PATH:
        raise L.Refused(f"--evidence must be 1..{L.MAX_PATH} characters")
    try:
        hashed = hash_ref(root, ref)
    except OSError as error:
        raise L.Refused(f"evidence {L.cap_text(ref, 100)} cannot be hashed: {error}")
    if hashed is None and path_shaped(ref):
        raise L.Refused(f"evidence {L.cap_text(ref, 100)} looks like a path but is not a regular file inside the "
                        "project (missing, directory, symlink or outside); name an existing file or write plain text")
    return {"ref": ref, "sha256": hashed[0] if hashed else None, "bytes": hashed[1] if hashed else None}


def tracker_item(root, path):
    if path is None:
        return None
    base = os.path.abspath(root)
    full = os.path.abspath(os.path.join(base, path))
    if full != base and not full.startswith(base + os.sep):
        raise L.Refused("--tracker must be inside the project")
    rel = os.path.relpath(full, base)
    try:
        hashed = hash_ref(base, rel)
    except OSError as error:
        raise L.Refused(f"tracker cannot be hashed: {error}")
    if hashed is None:
        raise L.Refused("--tracker must name a regular file inside the project (no symlinks)")
    return {"path": rel, "sha256": hashed[0]}


def jobs_digest(doc):
    rows = sorted([jid, job["state"], (job["result"] or {}).get("outcome"), (job["result"] or {}).get("result_sha256"),
                   len(job["post_bank"])] for jid, job in doc["jobs"].items())
    return L.sha256_hex(L.canonical(rows))


def receipt_hash(cp):
    return L.sha256_hex(L.canonical({k: v for k, v in cp.items() if k not in ("receipt_sha256", "resume")}))


def is_failure(job):
    return job["state"] == "banked" and job["result"]["outcome"] in ("failed", "unsupported")


def usage_age(rec):
    """Whole seconds since the session's usage was observed, or None when `observed_at` is missing or unreadable."""
    try:
        return max(0, int(time.time() - calendar.timegm(time.strptime(rec["observed_at"], "%Y-%m-%dT%H:%M:%SZ"))))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def evaluate(doc, cfg, root, key, check_receipt=True, rehash=True):
    """(reasons, checkpoint-or-None): every failing READY condition for `key`'s hand-off."""
    reasons = []
    if not doc["checkpoints"]:
        return ["no checkpoint exists"], None
    cp = doc["checkpoints"][-1]
    if check_receipt and receipt_hash(cp) != cp["receipt_sha256"]:
        reasons.append("checkpoint receipt does not recompute (tampered or corrupt)")
    accepted = {j for c in doc["checkpoints"] for j in c["accepted_failed"]}
    for jid, job in sorted(doc["jobs"].items()):
        if job["updated_revision"] > cp["revision"]:
            reasons.append(f"job {jid} changed after the checkpoint (revision {job['updated_revision']})")
        if job["state"] != "banked":
            reasons.append(f"job {jid} is not banked ({job['state']})")
            continue
        ack = job["acknowledged_revision"]
        if ack is None:
            reasons.append(f"unacknowledged arrival: job {jid}")
        elif job["post_bank"] and job["updated_revision"] > ack:
            reasons.append(f"late result for job {jid} after its acknowledgement")
        if is_failure(job) and jid not in accepted:
            reasons.append(f"failed job {jid} ({job['result']['outcome']}, {job['result']['cause']}) is not accepted")
    for item in (cp["evidence"] + ([cp["tracker"]] if cp["tracker"] else []) if rehash else []):
        ref = item.get("ref", item.get("path"))
        if item["sha256"] is None:
            continue
        try:
            now = hash_ref(root, ref)
        except OSError:
            now = None
        if now is None or now[0] != item["sha256"]:
            reasons.append(f"{'tracker' if 'path' in item else 'evidence'} {L.cap_text(ref, 80)} changed or is missing")
    rec = doc["sessions"].get(key) if key else None
    if rec is None or rec["usage_tokens"] is None:
        reasons.append("usage UNKNOWN" + ("" if key else " (no session given)"))
    capped = [L.cap_text(r, L.MAX_TEXT) for r in reasons[:MAX_REASONS]]
    if len(reasons) > MAX_REASONS:
        capped.append(f"... and {len(reasons) - MAX_REASONS} more")
    return capped, cp


# --- commands -------------------------------------------------------------------------------------------------

def session_arg(value, required=False):
    if value is None:
        if required:
            raise L.Refused("--session HARNESS:ID is required")
        return None
    owner = L.parse_session(value)
    return L.session_key(owner)


def hash_evidence(root, opts):
    """(evidence items, tracker item) for a checkpoint request. Run before the ledger lock is taken: hashing up to
    32 files of 64 MiB must not hold `lock_wait` hostage."""
    refs = list(opts.evidence or [])
    if not 1 <= len(refs) <= MAX_EVIDENCE:
        raise L.Refused(f"give 1..{MAX_EVIDENCE} --evidence")
    return [evidence_item(root, r) for r in refs], tracker_item(root, getattr(opts, "tracker", None))


def check_texts(opts, cfg):
    """The cheap `--state`/`--next` validation: (state, next), or Refused. Runs before any evidence is hashed."""
    state, nxt = opts.state_text, opts.next
    if not isinstance(state, str) or not state.strip() or len(state) > 2000:
        raise L.Refused("--state must be 1..2000 characters")
    if not isinstance(nxt, str) or not nxt.strip() or len(nxt) > 1000:
        raise L.Refused("--next must be 1..1000 characters")
    if not resume.next_fits_text(nxt, cfg):
        raise L.Refused(f"--next cannot be delivered whole within resume_max_bytes ({cfg['resume_max_bytes']}): "
                        "shorten it or raise the setting")
    return state, nxt


def checkpoint_in(doc, cfg, root, opts, key, hashed=None):
    """Append a receipt to `doc` (inside the caller's transaction) and return it. `hashed` is the result of
    `hash_evidence` computed outside the lock; without it the hashing happens here."""
    state, nxt = check_texts(opts, cfg)
    evidence, tracker = hashed or hash_evidence(root, opts)
    revision = doc["revision"] + 1
    covered = []
    for jid, job in sorted(doc["jobs"].items()):
        if job["state"] == "banked" and (job["acknowledged_revision"] is None
                                         or job["updated_revision"] > job["acknowledged_revision"]):
            job["acknowledged_revision"] = revision
            job["updated_at"], job["updated_revision"] = L.now(), revision
            covered.append(jid)
    accepted = sorted(set(getattr(opts, "accept_failed", None) or []))
    for jid in accepted:
        job = doc["jobs"].get(jid)
        if job is None or not is_failure(job):
            raise L.Refused(f"--accept-failed {L.cap_text(jid, 60)}: not a banked failed or unsupported job")
    cp = {
        "revision": revision, "created_at": L.now(), "session": key, "state": state, "next": nxt,
        "evidence": evidence, "tracker": tracker, "covered_jobs": covered, "accepted_failed": accepted,
        "pending_jobs": sorted(j for j, job in doc["jobs"].items() if job["state"] != "banked"),
        "jobs_digest": jobs_digest(doc), "ready_at_checkpoint": {"ready": False, "reasons": []},
        "resume": {"consumed_by": None, "consumed_at": None}, "receipt_sha256": "0" * 64,
    }
    doc["checkpoints"].append(cp)
    del doc["checkpoints"][:-L.MAX_CHECKPOINTS]
    reasons, _ = evaluate(doc, cfg, root, key, check_receipt=False, rehash=False)  # just hashed
    cp["ready_at_checkpoint"] = {"ready": not reasons, "reasons": reasons}
    cp["receipt_sha256"] = receipt_hash(cp)
    return cp


def checkpoint(ctx, opts):
    key = session_arg(getattr(opts, "session", None))
    check_texts(opts, ctx.cfg)  # cheap refusals first: a bad --state/--next never reads the evidence
    root = ctx.ledger.read()["project_root"]
    hashed = hash_evidence(root, opts)  # outside the lock
    with ctx.ledger.transaction() as doc:
        cp = checkpoint_in(doc, ctx.cfg, doc["project_root"], opts, key, hashed)
        return 0, {"revision": cp["revision"], "receipt_sha256": cp["receipt_sha256"],
                   "covered_jobs": cp["covered_jobs"], "accepted_failed": cp["accepted_failed"],
                   "pending_jobs": cp["pending_jobs"], "ready_at_checkpoint": cp["ready_at_checkpoint"]}


def ready(ctx, key):
    try:
        doc = ctx.ledger.read()
    except L.LedgerInvalid as error:
        return 1, {"ready": False, "reasons": [L.cap_text(f"ledger invalid: {error}")], "phase": U.UNKNOWN,
                   "usage_tokens": None, "usage_age_seconds": None, "receipt_sha256": None}
    reasons, cp = evaluate(doc, ctx.cfg, doc["project_root"], key)
    view = U.session_view(doc, ctx.cfg, key)
    rec = doc["sessions"].get(key) if key else None
    return (0 if not reasons else 1), {"ready": not reasons, "reasons": reasons, "phase": view["phase"],
                                       "usage_tokens": view["usage_tokens"],
                                       "usage_age_seconds": usage_age(rec) if rec else None,
                                       "receipt_sha256": cp["receipt_sha256"] if cp else None}


def handoff(ctx, opts):
    key = session_arg(opts.session, required=True)
    _, made = checkpoint(ctx, opts)
    code, out = ready(ctx, key)
    return code, dict(out, checkpoint={"revision": made["revision"], "receipt_sha256": made["receipt_sha256"],
                                       "ready_at_checkpoint": made["ready_at_checkpoint"]})
