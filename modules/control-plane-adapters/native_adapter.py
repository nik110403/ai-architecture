#!/usr/bin/env python3
"""The native autopilot behind the adapter protocol of contract.json (the reference adapter for conformance.py).

  native_adapter.py kickoff --repo R --plan P --fake-bin D --model M --state-home H [--budget-tokens N] [--max-rounds N] [--max-workers K]
  native_adapter.py resume|status|pause|stop --state-home H [--max-rounds N] [--max-workers K] [--budget-tokens N]

It only calls modules/autopilot/autopilot.py (kickoff provider `fake`: it never spends anything) with XDG_STATE_HOME=H.
The harness (conformance.py), not this adapter, kills the controller, corrupts the state file and edits the frozen plan.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTOPILOT = ROOT / "modules" / "autopilot" / "autopilot.py"


def out(code, reason, **kw):
    print(json.dumps(dict(kw, reason=reason), sort_keys=True))
    return code


def opt(args, name):
    return args[args.index(name) + 1] if name in args else None


def plan_dir(home):
    base = Path(home) / "ai-architecture" / "autopilot"
    ids = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    return ids[0] if ids else None


def run(argv, home):
    env = dict(os.environ, XDG_STATE_HOME=home)
    return subprocess.run([sys.executable, "-B", str(AUTOPILOT), *argv], capture_output=True, text=True, env=env)


def relay(r):
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def main(argv):
    if not argv:
        return out(2, "usage")
    verb, rest = argv[0], argv[1:]
    home = opt(rest, "--state-home")
    if not home:
        return out(2, "usage", message="--state-home is required")
    passthrough = []
    for name in ("--budget-tokens", "--max-rounds", "--max-workers"):
        if opt(rest, name) is not None:
            passthrough += [name, opt(rest, name)]
    if verb == "kickoff":
        return relay(run(["kickoff", "--repo", opt(rest, "--repo"), "--plan", opt(rest, "--plan"), "--provider", "fake", "--fake-bin", opt(rest, "--fake-bin"),
                          "--model", opt(rest, "--model") or "fake-m", *passthrough], home))
    pid = plan_dir(home)
    if pid is None:
        return out(2, "no-state")
    if verb == "resume":
        return relay(run(["resume", "--plan-id", pid, *passthrough], home))
    if verb in ("pause", "stop"):
        return relay(run([verb, "--plan-id", pid], home))
    if verb == "status":
        r = run(["status", "--json", "--plan-id", pid], home)
        try:
            doc = json.loads(r.stdout)
            doc["rounds_accepted"] = len(doc.get("items", {}).get("done", []))
            print(json.dumps(doc, sort_keys=True))
        except ValueError:
            sys.stdout.write(r.stdout)
        return r.returncode
    return out(2, "usage", message="unknown verb")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
