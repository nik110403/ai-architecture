#!/usr/bin/env python3
"""An adapter whose state is not where the contract says: it runs the native adapter with a --state-home the harness never sees.

The harness cannot read, kill or corrupt what it cannot find, so every cell that needs the state file must be not-run, never pass."""
import os
import subprocess
import sys

base = os.environ["AIARCH_BROKEN_BASE"]
args = sys.argv[1:]
if "--state-home" in args:
    i = args.index("--state-home") + 1
    args[i] = args[i] + "-elsewhere"
r = subprocess.run([sys.executable, "-B", base, *args], capture_output=True, text=True)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
