#!/usr/bin/env python3
"""Artifact-bound review receipts. Stdlib only; no network, no model.

A receipt records WHO reviewed WHICH files (absolute realpath + sha256) with what verdict. It is
fresh only while every named file still has exactly that content, so any later edit makes it stale.
A receipt is evidence for a gate, not authority: it never replaces proof that the reviewer ran.

  receipt.py issue --artifact F [--artifact G ...] --reviewer ROLE --verdict PASS|FAIL --out R
  receipt.py check --receipt R [--root D] [--format text|json]

Exit: 0 ok/fresh, 1 stale, 2 usage or refused input, 3 unavailable (unused here).
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 1 << 20
MAX_ARTIFACTS = 1000
MAX_ARTIFACT_BYTES = 64 << 20
ROLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


class Refused(Exception):
    """Input that must not be processed (exit 2)."""


def current_digest(path):
    """sha256 of the file's bytes, read with a size bound and without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"not a regular file: {path}")
        if st.st_size > MAX_ARTIFACT_BYTES:
            raise Refused(f"artifact larger than {MAX_ARTIFACT_BYTES} bytes: {path}")
        h = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as fh:
            for block in iter(lambda: fh.read(1 << 16), b""):
                h.update(block)
        return h.hexdigest()
    finally:
        os.close(fd)


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Refused(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def load_receipt(path):
    """Parsed receipt dict, or Refused for anything unreadable, oversized, malformed or of another schema."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"receipt is not a regular file: {path}")
        if st.st_size > MAX_RECEIPT_BYTES:
            raise Refused(f"receipt larger than {MAX_RECEIPT_BYTES} bytes: {path}")
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except Refused:
        raise
    except (OSError, ValueError) as exc:
        raise Refused(f"receipt unreadable: {path}: {type(exc).__name__}")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or type(data.get("schema_version")) is not int:
        raise Refused(f"receipt has an unknown schema_version: {path}")
    arts = data.get("artifacts")
    if (
        data.get("verdict") not in ("PASS", "FAIL")
        or not isinstance(data.get("reviewer"), str)
        or not isinstance(arts, list)
        or not 0 < len(arts) <= MAX_ARTIFACTS
        or not all(isinstance(a, dict) and isinstance(a.get("path"), str) and isinstance(a.get("sha256"), str) for a in arts)
    ):
        raise Refused(f"receipt is malformed: {path}")
    return data


def stale_files(receipt, root=None):
    """List of (path, why) for every artifact that is missing, changed, a symlink or unreadable."""
    problems = []
    for art in receipt["artifacts"]:
        p = art["path"]
        full = p if os.path.isabs(p) else os.path.join(root or ".", p)
        try:
            if os.path.islink(full) or os.path.realpath(full) != os.path.abspath(full) and os.path.isabs(p):
                problems.append((p, "symlink"))
            elif current_digest(full) != art["sha256"]:
                problems.append((p, "changed"))
        except FileNotFoundError:
            problems.append((p, "missing"))
        except (OSError, Refused) as exc:
            problems.append((p, f"unreadable: {type(exc).__name__}"))
    return problems


def issue(artifacts, reviewer, verdict, out):
    if verdict not in ("PASS", "FAIL"):
        raise Refused("verdict must be PASS or FAIL")
    if not ROLE_RE.match(reviewer or ""):
        raise Refused("reviewer must be a short role name")
    if not artifacts or len(artifacts) > MAX_ARTIFACTS:
        raise Refused("at least one artifact is required")
    entries, seen = [], set()
    for a in artifacts:
        if os.path.islink(a):
            raise Refused(f"refusing symlinked artifact: {a}")
        real = os.path.realpath(a)
        if real in seen:
            raise Refused(f"duplicate artifact path: {a}")
        seen.add(real)
        try:
            entries.append({"path": real, "sha256": current_digest(real)})
        except OSError as exc:
            raise Refused(f"artifact unreadable: {a}: {type(exc).__name__}")
    doc = {
        "schema_version": SCHEMA_VERSION,
        "reviewer": reviewer,
        "verdict": verdict,
        "artifacts": entries,
        "issued": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out = os.path.abspath(out)
    if os.path.islink(out):
        raise Refused(f"refusing symlinked output: {out}")
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", dir=os.path.dirname(out))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="receipt.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("issue")
    pi.add_argument("--artifact", action="append", required=True)
    pi.add_argument("--reviewer", required=True)
    pi.add_argument("--verdict", required=True, choices=["PASS", "FAIL"])
    pi.add_argument("--out", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("--receipt", required=True)
    pc.add_argument("--root")
    pc.add_argument("--format", choices=["text", "json"], default="text")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.cmd == "issue":
            doc = issue(args.artifact, args.reviewer, args.verdict, args.out)
            print(f"issued {args.verdict} receipt for {len(doc['artifacts'])} artifact(s): {os.path.abspath(args.out)}")
            return 0
        receipt = load_receipt(args.receipt)
        problems = stale_files(receipt, args.root)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps({"status": "stale" if problems else "fresh", "stale": [{"path": p, "why": w} for p, w in problems]}))
    elif problems:
        print("stale")
        for p, why in problems:
            print(f"  {p}: {why}")
    else:
        print("fresh")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
