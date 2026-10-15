#!/usr/bin/env python3
"""Read-only continuity snapshot validator; Python 3.11+, stdlib only.

This checks supplied snapshot evidence, not native harness readiness. Path checks
reject symlinks and non-regular inputs but do not claim race-proof handling.
"""

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path


DECLARATION = (
    "Caller-supplied edit_observed, usage_known, jobs, and arrivals are not "
    "authenticated facts. This is not actual readiness certification and gives no "
    "monotonic transaction, lock, freshness, or semantic-truth guarantee."
)
FIELDS = {
    "schema_version",
    "sha256",
    "mtime_ns",
    "edit_observed",
    "active_jobs",
    "arrivals",
    "banked_arrivals",
    "usage_known",
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
NEXT = re.compile(
    r"^[ \t]*-[ \t]+\*\*Next:\*\*[ \t]*(\S(?:.*\S)?)[ \t]*$", re.MULTILINE
)


class InputError(Exception):
    pass


def unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InputError("duplicate JSON key")
        value[key] = item
    return value


def regular(path, label):
    try:
        info = path.lstat()
    except OSError as error:
        raise InputError(f"cannot access {label}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InputError(f"{label} must be a regular non-symlink file")
    return info


def read_bytes(path, label):
    regular(path, label)
    try:
        return path.read_bytes()
    except OSError as error:
        raise InputError(f"cannot read {label}") from error


def nonempty_ids(value, label):
    if not isinstance(value, list):
        raise InputError(f"{label} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise InputError(f"{label} contains an invalid ID")
    if len(value) != len(set(value)):
        raise InputError(f"{label} contains duplicate IDs")
    return set(value)


def receipt_value(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
    except (UnicodeDecodeError, ValueError, RecursionError, InputError) as error:
        raise InputError("receipt is malformed or too deeply nested") from error
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise InputError("receipt has an unsupported schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise InputError("receipt schema_version must be integer 1")
    if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
        raise InputError("receipt sha256 must be lowercase hexadecimal")
    if type(value["mtime_ns"]) is not int or value["mtime_ns"] < 0:
        raise InputError("receipt mtime_ns must be a nonnegative integer")
    for name in ("edit_observed", "usage_known"):
        if type(value[name]) is not bool:
            raise InputError(f"receipt {name} must be boolean")
    value["active_jobs"] = nonempty_ids(value["active_jobs"], "active_jobs")
    value["arrivals"] = nonempty_ids(value["arrivals"], "arrivals")
    value["banked_arrivals"] = nonempty_ids(value["banked_arrivals"], "banked_arrivals")
    return value


def resume_reasons(tracker):
    try:
        text = tracker.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("tracker is not valid UTF-8 text") from error
    # Normalize only the parsing view: receipts still hash the original bytes.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fence = None
    block = None
    for line in text.split("\n"):
        marker = FENCE.fullmatch(line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip(" \t")
            ):
                fence = None
            continue
        if marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = marker[1]
            continue
        heading = HEADING.fullmatch(line)
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip(" \t")
            level = len(heading[1])
            if block is not None and level <= 2:
                break
            if block is None and level == 2 and title == "Resume here":
                block = []
                continue
        if block is not None:
            block.append(line)
    if block is None:
        return ["missing ## Resume here heading"]
    block = "\n".join(block)
    reasons = []
    if not block.strip():
        reasons.append("Resume here block is empty")
    if not NEXT.search(block):
        reasons.append("Resume here block has no nonempty Next")
    return reasons


def check(tracker_path, receipt_path):
    tracker = read_bytes(tracker_path, "tracker")
    receipt = receipt_value(read_bytes(receipt_path, "receipt"))
    try:
        current = tracker_path.stat()
    except OSError as error:
        raise InputError("cannot stat tracker") from error
    reasons = resume_reasons(tracker)
    if hashlib.sha256(tracker).hexdigest() != receipt["sha256"]:
        reasons.append("tracker SHA-256 does not match receipt")
    if current.st_mtime_ns != receipt["mtime_ns"]:
        reasons.append("tracker mtime_ns does not match receipt")
    if not receipt["edit_observed"]:
        reasons.append("tracker edit was not observed")
    if not receipt["usage_known"]:
        reasons.append("usage is unknown")
    if receipt["active_jobs"]:
        reasons.append(
            "active jobs remain: " + ", ".join(sorted(receipt["active_jobs"]))
        )
    unbanked = receipt["arrivals"] - receipt["banked_arrivals"]
    if unbanked:
        reasons.append("unbanked arrivals remain: " + ", ".join(sorted(unbanked)))
    return sorted(reasons)


def emit(reasons, output):
    status = "CONSISTENT" if not reasons else "UNREADY"
    if output == "json":
        print(
            json.dumps(
                {"declaration": DECLARATION, "reasons": reasons, "status": status},
                sort_keys=True,
            )
        )
    else:
        print(f"continuity-check: {status}")
        for reason in reasons:
            print(f"- {reason}")
        print(f"declaration: {DECLARATION}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a read-only continuity snapshot."
    )
    parser.add_argument("--tracker", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        reasons = check(args.tracker, args.receipt)
    except InputError as error:
        print(f"invalid input: {error}", file=sys.stderr)
        return 2
    emit(reasons, args.format)
    return 0 if not reasons else 1


if __name__ == "__main__":
    sys.exit(main())
