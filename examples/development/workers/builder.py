"""Stand-in builder (role: builder). Authored and deterministic; no model judgement is exercised.

Runs in its dispatch worktree (cwd). Usage: builder.py --journey debug|secure|incident --fixtures DIR [--contract FILE]
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402


def debug(cwd, fixtures):
    base = c.git(cwd, "rev-parse", "HEAD").strip()
    symptom = c.py(os.path.join(cwd, "check_repro.py"), cwd=cwd)[1].strip()
    shutil.copy(os.path.join(fixtures, "red", "check_slug.py"), cwd)
    red = c.commit_all(cwd, "red: add the failing test")
    red_exit = c.py(os.path.join(cwd, "check_slug.py"), cwd=cwd)[0]
    shutil.copy(os.path.join(fixtures, "green", "slug.py"), cwd)
    green = c.commit_all(cwd, "green: reuse textutil.collapse_ws")
    green_exit = c.py(os.path.join(cwd, "check_slug.py"), cwd=cwd)[0]
    c.emit({
        "base_commit": base, "red_commit": red, "green_commit": green, "red_exit": red_exit, "green_exit": green_exit,
        "test_id": "TestSlug.test_collapses_whitespace", "reproduce": {"script": "check_repro.py", "symptom": symptom},
        "scope": ["check_slug.py", "slug.py"], "reuse": ["textutil.collapse_ws"], "new_dependencies": [],
        "dependency_decisions": [{
            "candidate": "python-slugify", "decision": "rejected",
            "reason": "str methods plus the existing textutil.collapse_ws are enough"}],
    })


def secure(cwd, fixtures):
    base = c.git(cwd, "rev-parse", "HEAD").strip()
    shutil.copy(os.path.join(fixtures, "guarded", "fileserver.py"), cwd)
    c.emit({"base_commit": base, "build_commit": c.commit_all(cwd, "add the guards")})


def incident(cwd, fixtures, contract):
    change_id = c.prompt()["change_id"]
    spec = contract["changes"][change_id]
    base = c.git(cwd, "rev-parse", "HEAD").strip()
    state_path = os.path.join(cwd, "state.json")
    with open(state_path, "rb") as handle:
        before_bytes = handle.read()
    state = json.loads(before_bytes)
    recheck = {"state_sha256": c.sha(before_bytes), "satisfied": all(state.get(k) == v for k, v in spec["requires"].items())}
    if not recheck["satisfied"]:
        c.emit({"change_id": change_id, "prerequisite_recheck": recheck, "apply_commit": None, "rollback_commit": None})
        return
    script = os.path.join(fixtures, contract["script"])
    before = c.py_json(script, state_path, cwd=cwd)[contract["metric"]["name"]]
    state.update(spec["set"])
    with open(state_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
    apply_commit = c.commit_all(cwd, f"apply {change_id}")
    after = c.py_json(script, state_path, cwd=cwd)[contract["metric"]["name"]]
    outcome = "succeeded" if after <= contract["metric"]["threshold"] else "failed"
    rollback = None
    if outcome == "failed":
        c.git(cwd, "checkout", base, "--", "state.json")
        rollback = c.commit_all(cwd, f"roll back {change_id}")
    c.emit({"change_id": change_id, "prerequisite_recheck": recheck, "apply_commit": apply_commit,
            "rollback_commit": rollback, "before": before, "after": after, "outcome": outcome})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journey", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--contract")
    args = parser.parse_args()
    cwd = os.getcwd()
    if args.journey == "debug":
        debug(cwd, args.fixtures)
    elif args.journey == "secure":
        secure(cwd, args.fixtures)
    else:
        incident(cwd, args.fixtures, c.load_contract(args.contract)["journeys"]["incident"])


if __name__ == "__main__":
    main()
