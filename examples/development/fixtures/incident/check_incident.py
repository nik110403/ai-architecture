"""Deterministic service simulator: prints the latency metric for a state file. Usage: STATE_FILE."""
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
print(json.dumps({"latency_ms": 200 if state.get("cache") == "on" else 900}))
