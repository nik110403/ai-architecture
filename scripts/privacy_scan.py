#!/usr/bin/env python3
"""Scan a tree for generic privacy markers. Stdlib only.

Exit 0 clean, 1 findings (printed as `path:line: term`). Usage: privacy_scan.py [--root DIR]
Author metadata has no special exemption: matching markers in LICENSE*,
CITATION.cff, or plugin metadata are still flagged. The scanner skips itself,
symlinks, binary files, and excluded directories.
"""

import argparse
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__"}
SELF = "scripts/privacy_scan.py"
# (label, compiled pattern); generic defaults, not an operator-specific policy.
# Built from fragments so this file never matches itself.
TERMS = [
    (t, re.compile(re.escape(t), re.I))
    for t in (
        "/" + "home/",
        "10.0" + ".0.",
    )
]
TERMS += [
    ("ses_", re.compile(r"\bses_[A-Za-z0-9]", re.I)),
    ("chg-", re.compile(r"\bchg-[0-9a-f]", re.I)),
]


def scan(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root is not a directory: {root}")
    hits = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if rel.as_posix() == SELF or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            for label, pattern in TERMS:
                if pattern.search(line):
                    hits.append(f"{rel.as_posix()}:{number}: {label}")
    return hits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        hits = scan(args.root)
    except ValueError as exc:
        parser.error(str(exc))
    print("\n".join(hits))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
