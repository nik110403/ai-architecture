#!/usr/bin/env python3
"""A third negative control for the conformance harness: an adapter that publishes state the harness must not trust.

It wraps the native adapter (AIARCH_BROKEN_BASE, absolute path), but
  - keeps a decoy state.json (owner.pid = a sleeper process, a stop_reason) under --state-home, beside the real one;
  - runs the real controller in a session of its own, out of the process tree the harness launched
    (AIARCH_DECOY_SLEEPER=inside puts the sleeper in the launched tree, forge also rewrites the real owner.pid to it;
    the default puts it outside);
  - answers `stop` with SIGKILL on the real controller and its workers: no drain, nothing banked, then reports stopped;
  - answers a resume with state-corrupt when its decoy file fails to parse, whatever the real state looks like.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

base = os.environ["AIARCH_BROKEN_BASE"]
args = sys.argv[1:]
verb = args[:1]
home = Path(args[args.index("--state-home") + 1])
decoy_dir = home / "ai-architecture" / "0-decoy"
decoy = decoy_dir / "state.json"


def real_state():
    for f in sorted((home / "ai-architecture" / "autopilot").glob("*/state.json")):
        try:
            return json.loads(f.read_text())
        except ValueError:
            return {}
    return {}


def native(*a, **kw):
    return subprocess.run([sys.executable, "-B", base, *a], capture_output=True, text=True, **kw)


if verb == ["kickoff"]:
    decoy_dir.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.environ.get("AIARCH_DECOY_SLEEPER") not in ("inside", "forge"))
    decoy.write_text(json.dumps({"owner": {"pid": sleeper.pid}, "stop_reason": None}))
    if os.environ.get("AIARCH_DECOY_SLEEPER") == "forge":  # also keep rewriting the real state's owner.pid to the in-tree sleeper
        import threading

        def forge():
            while True:
                for f in (home / "ai-architecture" / "autopilot").glob("*/state.json"):
                    try:
                        d = json.loads(f.read_text())
                        if d["owner"]["pid"] != sleeper.pid:
                            (decoy_dir / "real-owner").write_text(str(d["owner"]["pid"]))
                        d["owner"]["pid"] = sleeper.pid
                        tmp = f.with_name("state.forge")
                        tmp.write_text(json.dumps(d))
                        os.replace(tmp, f)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                time.sleep(0.05)

        threading.Thread(target=forge, daemon=True).start()
    real = subprocess.Popen([sys.executable, "-B", base, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    out, err = real.communicate()
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(0 if real.returncode in (0, -signal.SIGKILL) else real.returncode)
if verb == ["stop"]:
    pid = (real_state().get("owner") or {}).get("pid")
    if (decoy_dir / "real-owner").exists():
        pid = int((decoy_dir / "real-owner").read_text())
    if isinstance(pid, int):
        os.kill(pid, signal.SIGKILL)
    for f in list((Path(os.environ["PATH"].split(":")[0]) / "pids").glob("*")):
        try:
            os.kill(int(f.read_text().split()[0]), signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            pass
    try:
        decoy.write_text(json.dumps({"owner": {"pid": 1}, "stop_reason": "stopped"}))
    except OSError:
        pass
    time.sleep(0.5)
    print(json.dumps({"reason": "stopped"}))
    sys.exit(0)
if verb == ["resume"]:
    try:
        json.loads(decoy.read_text())
    except (OSError, ValueError):
        print(json.dumps({"reason": "state-corrupt"}))
        sys.exit(1)
r = native(*args)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
