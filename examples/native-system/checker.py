#!/usr/bin/env python3
"""Offline checker seat: reads the prompt on stdin and prints exactly CONFIRMED or REJECTED.

The prompt carries a `Numbers:` line and a `Claimed sum:` line; this seat never sees the worker's process.
--once-marker FILE: fail (exit 1) the first time, creating FILE; later runs behave normally.
"""

import argparse
import os
import re
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once-marker")
    args = parser.parse_args()
    prompt = sys.stdin.read()
    if args.once_marker and not os.path.exists(args.once_marker):
        with open(args.once_marker, "w", encoding="utf-8") as handle:
            handle.write("failed once\n")
        print("checker told to fail once", file=sys.stderr)
        return 1
    numbers = re.search(r"^Numbers: (.*)$", prompt, re.M)
    claim = re.search(r"^Claimed sum: (-?\d+)$", prompt, re.M)
    if not numbers or not claim:
        print("REJECTED")
        return 0
    print("CONFIRMED" if sum(int(x) for x in numbers.group(1).split()) == int(claim.group(1)) else "REJECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
