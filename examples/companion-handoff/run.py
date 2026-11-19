#!/usr/bin/env python3
"""Offline demo: one disposable candidate through handoff, inbox and a recorded disposition, against a fixture companion.

    python3 run.py --offline --out DIR

--offline is required. The companion here is an authored stand-in kept with the module's tests, so the receipt says
backend: fixture and never counts as a real-companion receipt. Everything runs in a temp workspace; DIR gets only the
receipt file. Prints the roundtrip result as one JSON line.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parents[1] / "modules" / "discovery-inbox"
FAKE = MODULE / "tests" / "fixtures" / "fake_companion"


def git(root, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run([shutil.which("git") or "git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=demo", "-c",
                    "user.email=demo@example.invalid", *args], cwd=root, env=env, check=True, capture_output=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", required=True, help="no network, fixture companion only")
    ap.add_argument("--out", required=True, metavar="DIR", help="receipt directory (created 0700)")
    args = ap.parse_args(argv)
    if not shutil.which("git"):
        print("run.py: git was not found", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        root = work / "companion"
        shutil.copytree(FAKE, root)
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "fixture")
        project = work / "targets" / "demo-project"
        project.mkdir(parents=True)
        (project / "AGENTS.md").write_text("# Demo project\n")
        manifest = work / "manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "interface": "handoff-brief/1", "name": "fixture-companion", "root": str(root), "rev": "HEAD",
            "commands": {"handoff": "bin/fake-handoff", "set": "bin/fake-set", "list": "bin/fake-list"},
            "support": ["bin/fake_lib.py"],
            "dispositions": {"adopted": "integrated", "harvested": "harvested", "deferred": "deferred", "rejected": "skip"},
            "eligible_status": "next"}))
        done = subprocess.run([sys.executable, "-B", str(MODULE / "discovery.py"), "roundtrip", "--companion", str(manifest),
                               "--registry", str(work / "registry.json"), "--project", str(project), "--topic", "main", "--mode", "pack",
                               "--disposition", "deferred", "--backend", "fixture", "--out", args.out], capture_output=True, text=True,
                              timeout=300)
        if done.returncode != 0:
            print(done.stderr.strip(), file=sys.stderr)
            return done.returncode
        result = json.loads(done.stdout)
        print(done.stdout.strip())
        print(f"candidate {result['candidate_id']} placed in the pack, disposition {result['disposition']}, "
              f"companion ack {result['companion_ack']}, backend {result['backend']} (a fixture does not make 70-discovery EXERCISED)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
