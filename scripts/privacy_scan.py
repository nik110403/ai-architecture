#!/usr/bin/env python3
"""Scan a tree for private-infrastructure terms. Stdlib only.

Exit 0 clean, 1 findings (printed as `path:line: term`). Usage: privacy_scan.py [--root DIR]
No file is skipped: author metadata (LICENSE*, CITATION.cff, `author` in
.claude-plugin/*.json, banner footer) holds only the author's name/handle/site,
which never matches a deny-list term, so nothing needs allowlisting. Deny-list
terms in those files would still be flagged. The scanner skips only itself.
"""

import argparse
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__"}
SELF = "scripts/privacy_scan.py"
# (label, compiled pattern); built from fragments so this file never matches itself.
TERMS = [(t, re.compile(re.escape(t), re.I)) for t in (
    "/home/", "ai-01", "home.arpa", "10.0.0.", "Nextcloud", "Second Brain",
    "research-git", "herdr", "Forgejo", "Tailscale", "DeepInfra",
    "prose-engine", "writing-style", "coralline", "paperclip", "git-autopilot")]
TERMS += [
    ("ses_", re.compile(r"\bses_[A-Za-z0-9]", re.I)),
    ("chg-", re.compile(r"\bchg-[0-9a-f]", re.I)),
    ("laya", re.compile(r"\blaya\b", re.I)),
    ("AIER", re.compile(r"\bAIER\b")),  # case-sensitive
]


def scan(root):
    root = Path(root).resolve()
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
    hits = scan(parser.parse_args(argv).root)
    print("\n".join(hits))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
