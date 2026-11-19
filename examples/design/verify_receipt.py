#!/usr/bin/env python3
"""Verify a design receipt: re-render every round in real Chrome and re-derive the verdict.

Usage: verify_receipt.py [RECEIPT [--run DIR]]. With no argument it runs the offline example into a temporary output
directory first and then verifies that receipt. Exit codes: 0 PASS (prints `rendered: ran`), 1 FAIL or invalid receipt,
2 refused, 3 NOT RUN (prints `rendered: NOT RUN (reason)`; a missing browser is never a pass).
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    spec = importlib.util.spec_from_file_location("aiarch_design_verify", ROOT / "modules" / "design-pipeline" / "design.py")
    design = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(design)
    if not args:
        with tempfile.TemporaryDirectory(prefix="design-verify-out-") as tmp:
            out = Path(tmp) / "out"
            proc = subprocess.run([sys.executable, "-B", str(HERE / "run.py"), "--offline", "--out", str(out)], text=True,
                                  capture_output=True)
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            if proc.returncode in (2, 3):
                return proc.returncode
            run = out / json.loads((out / "latest.json").read_text())["run"]
            return design.main(["verify", str(run / "receipt.json")])
    return design.main(["verify", *args])


if __name__ == "__main__":
    sys.exit(main())
