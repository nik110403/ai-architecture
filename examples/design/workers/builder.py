"""Stand-in design-builder (role: design-builder). Authored and deterministic; no model judgement is exercised.

Runs in its dispatch worktree (cwd). The prompt arrives on stdin as JSON:
  stage build   copy the seeded first-pass app (fixtures/bad) and commit it
  stage revise  apply one authored patch per finding check id (fixtures/defects.json, reversed) and commit
Flags are for the example's failure cells: --leave-unfixed CHECK, --delete-element, --canary TEXT, --plant-symlink PATH.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

FIXED_DATE = "2026-01-01T00:00:00Z"
HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "gc.auto=0"]


def git(cwd, *argv):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE}
    return subprocess.run(["git", "-C", cwd, *HARDEN, *argv], capture_output=True, env=env, check=True).stdout.decode("utf-8")


def commit_all(cwd, message):
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "--allow-empty", "-m", message)
    return git(cwd, "rev-parse", "HEAD").strip()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--leave-unfixed")
    parser.add_argument("--delete-element", action="store_true")
    parser.add_argument("--canary")
    parser.add_argument("--plant-symlink")
    args = parser.parse_args()
    prompt = json.loads(sys.stdin.read())
    cwd, tail = os.getcwd(), (f" [{args.canary}]" if args.canary else "")
    if args.canary:
        print(args.canary, file=sys.stderr)
    if prompt["stage"] == "build":
        for name in sorted(os.listdir(os.path.join(args.fixtures, "bad"))):
            shutil.copy(os.path.join(args.fixtures, "bad", name), cwd)
        if args.plant_symlink:
            os.symlink(args.plant_symlink, os.path.join(cwd, "linked.txt"))
        out = {"stage": "build", "commit": commit_all(cwd, "build: first pass" + tail)}
    else:
        findings = prompt["findings"]
        if hashlib.sha256(canon(findings).encode()).hexdigest() != prompt["findings_sha"]:
            print("findings sha mismatch", file=sys.stderr)
            return 1
        with open(os.path.join(args.fixtures, "defects.json"), encoding="utf-8") as handle:
            defects = json.load(handle)["defects"]
        fixed = []
        for check in sorted({f["check"] for f in findings}):
            if check == args.leave_unfixed or check not in defects:
                continue
            for edit in defects[check]["edits"]:
                path = os.path.join(cwd, edit["file"])
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text.replace(edit["bad"], edit["good"], 1))
            fixed.append(check)
        if args.delete_element:
            path = os.path.join(cwd, "index.html")
            with open(path, encoding="utf-8") as handle:
                kept = [line for line in handle.read().splitlines(True) if 'id="card"' not in line]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("".join(kept))
        out = {"stage": "revise", "commit": commit_all(cwd, "revise: apply findings" + tail), "fixed": fixed,
               "findings_sha": prompt["findings_sha"]}
    print(json.dumps(dict(out, **({"note": args.canary} if args.canary else {})), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
