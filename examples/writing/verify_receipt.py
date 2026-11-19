#!/usr/bin/env python3
"""Verify the writing example: re-run it in a temp dir (default) or check an existing --receipt.

Exit 0 only if every expected step is present and passed and each recorded draft hash matches
the draft file next to the receipt; exit 1 otherwise.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED = ["render correspondence", "render essay", "render personal", "route fiction newspaper",
            "profile round trip"]


def verify(receipt_path):
    try:
        data = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        steps = {s["step"]: s for s in data["steps"]}
        if [s["step"] for s in data["steps"]] != EXPECTED or not data["ok"]:
            return "steps missing or receipt not ok"
        if not all(s["ok"] for s in steps.values()):
            return "a step failed"
        for step in EXPECTED[:3]:
            draft = Path(receipt_path).parent / steps[step]["register"] / "draft.md"
            if hashlib.sha256(draft.read_bytes()).hexdigest() != steps[step]["draft_sha256"]:
                return f"draft hash mismatch: {step}"
    except (OSError, ValueError, KeyError, TypeError) as error:
        return f"unreadable receipt: {type(error).__name__}"
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--receipt")
    a = p.parse_args()
    if a.receipt:
        problem = verify(a.receipt)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable, "-B", str(HERE / "run.py"), "--offline", "--out", tmp],
                                 capture_output=True, text=True)
            problem = f"run.py exited {run.returncode}" if run.returncode else verify(Path(tmp) / "receipt.json")
    print("verify_receipt: OK" if problem is None else f"verify_receipt: FAILED: {problem}")
    return 0 if problem is None else 1


if __name__ == "__main__":
    sys.exit(main())
