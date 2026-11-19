#!/usr/bin/env python3
"""Offline worker seat: prints the sum of the integers in ./input.txt once the gate file exists.

Options: --gate FILE (wait until it exists), --offset N (add N: the --wrong-worker hook), --fail (exit 1).
The prompt on stdin is read and ignored.
"""

import argparse
import os
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()
    sys.stdin.read()
    while args.gate and not os.path.exists(args.gate):
        time.sleep(0.05)
    if args.fail:
        print("worker told to fail", file=sys.stderr)
        return 1
    with open("input.txt", encoding="utf-8") as handle:
        print(sum(int(x) for x in handle.read().split()) + args.offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
