#!/usr/bin/env python3
"""Verify the documents example: re-run it in a temp dir (default) or check an existing --receipt.

Exit 0 only if every expected step is present, in order, and is `pass` or `unavailable` with a reason, and
every recorded artifact hash matches the file next to the receipt. `unavailable` is counted apart from
passes and never as one. Exit 1 otherwise, including for a tampered receipt.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED = ["tools status", "create docx", "create xlsx", "create pptx", "create pdf", "check-formulas",
            "check-formulas names wrong cell", "package", "render pdf", "cloud read", "cloud write", "cloud conflict",
            "cloud missing connector", "cloud stub read unverified", "calendar add authorized", "calendar add refused",
            "morning brief", "import preview", "import apply", "import rollback"]
UNAVAILABLE_OK = {"render pdf"}


def verify(receipt_path):
    """Return (problem, passed, unavailable)."""
    try:
        data = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        steps = data["steps"]
        if [s["step"] for s in steps] != EXPECTED:
            return "steps missing, extra or out of order", 0, 0
        passed = unavailable = 0
        for s in steps:
            if s["status"] == "pass":
                passed += 1
            elif s["status"] == "unavailable" and s["step"] in UNAVAILABLE_OK and str(s.get("reason", "")).strip():
                unavailable += 1
            else:
                return f"step not passed: {s['step']} ({s['status']})", 0, 0
        if data["ok"] is not True or not data["artifacts"]:
            return "receipt not ok or lists no artifacts", 0, 0
        for rel, digest in data["artifacts"].items():
            target = (Path(receipt_path).parent / rel).resolve()
            if Path(receipt_path).parent.resolve() not in target.parents:
                return f"artifact path escapes the receipt directory: {rel}", 0, 0
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return f"artifact hash mismatch: {rel}", 0, 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return f"unreadable receipt: {type(error).__name__}", 0, 0
    return None, passed, unavailable


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--receipt")
    a = p.parse_args()
    if a.receipt:
        problem, passed, unavailable = verify(a.receipt)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable, "-B", str(HERE / "run.py"), "--offline", "--out", tmp],
                                 capture_output=True, text=True)
            if run.returncode:
                problem, passed, unavailable = f"run.py exited {run.returncode}", 0, 0
            else:
                problem, passed, unavailable = verify(Path(tmp) / "receipt.json")
    if problem is None:
        print(f"verify_receipts: OK ({passed} passed, {unavailable} unavailable, never counted as passed)")
        return 0
    print(f"verify_receipts: FAILED: {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
