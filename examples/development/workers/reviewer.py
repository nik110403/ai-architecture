"""Stand-in security-reviewer. Replays the contract's threats and the benign request against the named commit."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()
    commit, contract = c.prompt()["commit"], c.load_contract(args.contract)["journeys"]["secure-build"]
    script = os.path.join(args.fixtures, contract["script"])
    with c.extract(os.getcwd(), commit) as tree:
        threats = [t["id"] for t in contract["threats"]
                   if c.py_json(script, tree, t["id"], t["input"], cwd=tree)["outcome"] == "refused"]
        benign = c.py_json(script, tree, contract["benign"]["id"], contract["benign"]["input"], cwd=tree)["outcome"]
    approved = len(threats) == len(contract["threats"]) and benign == "served"
    c.emit({"commit": commit, "verdict": "APPROVE" if approved else "CHANGES-REQUESTED", "threats_refused": threats,
            "benign": benign})


if __name__ == "__main__":
    main()
