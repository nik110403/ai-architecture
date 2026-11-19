"""Verification half of artifact-bound review receipts (the issuing CLI is modules/review/receipt.py).

A receipt names files by absolute realpath + sha256 and carries a verdict and the reviewer role. The Stop gate
uses receipts only to NARROW what it accepts: proof that the review role ran stays mandatory. Reading is bounded
(receipt count, receipt size, artifact size); anything over a bound, unreadable or malformed is reported, never
skipped, so the caller blocks. Stdlib only. Keep the schema in step with modules/review/receipt.py (schema_version 1)."""
import hashlib
import json
import os
import stat

MAX_RECEIPTS = 200
MAX_RECEIPT_BYTES = 1 << 20
MAX_ARTIFACT_BYTES = 16 << 20


def _no_dups(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def file_sha256(path):
    """(digest, None) or (None, problem). Never follows a final symlink; bounded size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if st.st_size > MAX_ARTIFACT_BYTES:
            return None, f"too large to verify (> {MAX_ARTIFACT_BYTES} bytes)"
        h = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as fh:
            for block in iter(lambda: fh.read(1 << 16), b""):
                h.update(block)
        return h.hexdigest(), None
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    finally:
        os.close(fd)


def load_dir(rdir):
    """([receipt dicts], [problem strings]). A configured directory that is unusable is a problem, not 'no receipts'."""
    if not isinstance(rdir, str) or not os.path.isabs(rdir):
        return [], [f"review_receipts_dir {rdir!r} is not an absolute path"]
    try:
        if os.path.islink(rdir) or not os.path.isdir(rdir):
            return [], [f"review_receipts_dir {rdir} is not a plain directory (missing, not a directory, or a symlink)"]
        names = sorted(n for n in os.listdir(rdir) if n.endswith(".json"))
    except OSError as exc:
        return [], [f"review_receipts_dir {rdir} is unreadable ({type(exc).__name__})"]
    if len(names) > MAX_RECEIPTS:
        return [], [f"too many receipts in {rdir} ({len(names)} > {MAX_RECEIPTS}); archive old ones"]
    receipts, problems = [], []
    for n in names:
        p = os.path.join(rdir, n)
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECEIPT_BYTES:
                raise ValueError("not a regular file or too large")
            with open(p, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8"), object_pairs_hook=_no_dups)
            arts = doc["artifacts"]
            if (type(doc.get("schema_version")) is not int or doc["schema_version"] != 1 or doc["verdict"] not in ("PASS", "FAIL")
                    or not isinstance(doc["reviewer"], str) or not isinstance(arts, list)
                    or not all(isinstance(a["path"], str) and isinstance(a["sha256"], str) for a in arts)):
                raise ValueError("malformed")
        except Exception:
            problems.append(f"unreadable or malformed receipt {n}")
            continue
        receipts.append(doc)
    return receipts, problems


def assess(files, rdir, role):
    """Block reasons (empty list = every file is covered by a fresh PASS from `role` and no FAIL names its current hash)."""
    receipts, problems = load_dir(rdir)
    reasons = list(problems)
    if problems and not receipts and any("review_receipts_dir" in p or "too many" in p for p in problems):
        return reasons
    for f in sorted(files):
        real = os.path.realpath(f)
        digest, why = file_sha256(real)
        if why == "missing":
            continue  # deleted since: nothing left to certify
        if why:
            reasons.append(f"cannot verify {f}: {why}")
            continue
        named = [(r, a) for r in receipts for a in r["artifacts"] if a["path"] == real]
        if any(r["verdict"] == "FAIL" and a["sha256"] == digest for r, a in named):
            reasons.append(f"a FAIL review receipt names the current content of {f}")
        elif any(r["verdict"] == "PASS" and r["reviewer"] == role and a["sha256"] == digest for r, a in named):
            continue
        elif named:
            reasons.append(f"review receipt for {f} is stale or not from {role} (the file changed after review, or another role issued it)")
        else:
            reasons.append(f"no review receipt covers {f}")
    return reasons
