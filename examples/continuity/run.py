#!/usr/bin/env python3
"""DETERMINISTIC FIXTURE: recover one fixed persisted Next in fresh processes."""

import json
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "modules" / "continuity" / "journal.py"
NEXT = "write checked-summary.json from verified numbers.json"


def journal(target, command, *args):
    run = subprocess.run(
        [sys.executable, str(TOOL), command, "--target", str(target), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(run.stdout)


def check_numbers(target):
    values = json.loads((target / "numbers.json").read_text(encoding="utf-8"))
    if values != [2, 3, 5]:
        raise ValueError("unexpected fixture artifact")
    return sum(values)


def fresh(mode, target):
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--mode",
            mode,
            "--target",
            str(target),
        ],
        check=True,
    )


def dispatch_fixed_next(target):
    resumed = journal(target, "resume", "--json")
    if not resumed["ready"] or resumed["journal"]["checkpoint"]["next"] != NEXT:
        raise ValueError("checkpoint is not ready for this fixed action")
    (target / "checked-summary.json").write_text(
        json.dumps({"total": check_numbers(target), "executed_next": NEXT}),
        encoding="utf-8",
    )


def verify(target):
    expected = {"total": 10, "executed_next": NEXT}
    if (
        json.loads((target / "checked-summary.json").read_text(encoding="utf-8"))
        != expected
    ):
        raise ValueError("independent summary verification failed")


def fixture():
    print("DETERMINISTIC FIXTURE")
    with tempfile.TemporaryDirectory(prefix="continuity-preview-") as raw:
        target = Path(raw)
        journal(target, "start", "--job", "fixture-worker")
        (target / "numbers.json").write_text("[2, 3, 5]", encoding="utf-8")
        fresh("check", target)
        journal(target, "finish", "--job", "fixture-worker", "--result", "numbers.json")
        journal(
            target,
            "checkpoint",
            "--state",
            "numbers independently checked",
            "--evidence",
            "numbers.json",
            "--next",
            NEXT,
        )
        fresh("next", target)
        fresh("verify", target)
    print("persisted Next executed: PASS (summary total=10)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("check", "next", "verify"))
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    if args.mode is None:
        fixture()
    else:
        if args.target is None:
            parser.error("--target is required with --mode")
        if args.mode == "check":
            check_numbers(args.target)
        elif args.mode == "next":
            dispatch_fixed_next(args.target)
        else:
            verify(args.target)


if __name__ == "__main__":
    main()
