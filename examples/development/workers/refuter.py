"""Stand-in refuter (role: refuter). Tries to reproduce the finding it is handed; echoes the finding sha it checked.

Usage: refuter.py --journey domain|audit --fixtures DIR
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journey", required=True)
    parser.add_argument("--fixtures", required=True)
    args = parser.parse_args()
    ask, cwd = c.prompt(), os.getcwd()
    finding = ask["finding"]
    if args.journey == "domain":
        replay = c.py_json(os.path.join(args.fixtures, "check_scenario.py"), cwd, finding["scenario_id"], cwd=cwd)
        stands = replay["contradiction"] and (replay["expected"], replay["observed"]) == (finding["expected"], finding["observed"])
        c.emit({"finding_sha256": ask["finding_sha256"], "verdict": "STANDS" if stands else "REFUTED"})
        return
    seen = c.py_json(os.path.join(args.fixtures, "check_exploit.py"), cwd, ask["replay_case"], cwd=cwd)["canary_observed"]
    stands = seen if finding["status"] == "confirmed" else not seen
    c.emit({"finding_id": finding["id"], "finding_sha256": ask["finding_sha256"], "verdict": "STANDS" if stands else "REFUTED"})


if __name__ == "__main__":
    main()
