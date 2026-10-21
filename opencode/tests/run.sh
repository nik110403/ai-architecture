#!/usr/bin/env bash
# OpenCode bridge + generator tests. Usage (from the repository root): bash opencode/tests/run.sh
# Needs python3 and bun. Missing bun is incomplete, never a passing suite.
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OC="$(dirname "$HERE")"
command -v python3 >/dev/null 2>&1 || {
	echo "python3 is required"
	exit 1
}
TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT
export HOME="$TMP/home"
mkdir -p "$HOME"
unset AIARCH_CONFIG AIARCH_GATES_ROOT
export PYTHONDONTWRITEBYTECODE=1
rc=0
echo "== agents generator =="
SYNC="$OC/agents/opencode-agents-sync"
python3 "$SYNC" --agents-dir "$HERE/fixtures/agents" --out "$TMP/fixture.json" || rc=1
if cmp -s "$TMP/fixture.json" "$HERE/fixtures/agents.golden.json"; then echo "ok   fixture matches golden JSON"; else
	echo "FAIL fixture differs from golden JSON"
	rc=1
fi
if python3 "$SYNC" --check "$HERE/fixtures/agents.golden.json" --agents-dir "$HERE/fixtures/agents" >/dev/null; then echo "ok   --check accepts the golden file"; else
	echo "FAIL --check"
	rc=1
fi
python3 - "$HERE/fixtures/agents.golden.json" <<'PY' || rc=1
import json, sys
a = json.load(open(sys.argv[1]))["agent"]
assert "edit" not in a["writer"]["tools"], "Write alone must keep edit un-switched-off"
assert "edit" in a["scout"]["tools"] and "edit" not in a["fixer"].get("tools", {}), "Edit|Write mapping"
assert not any(v for r in a.values() for v in r.get("tools", {}).values()), "generator must never emit a grant"
print("ok   generator emits switch-offs only; Edit|Write share one value")
PY
echo '{}' >"$TMP/stale.json"
if python3 "$SYNC" --check "$TMP/stale.json" --agents-dir "$HERE/fixtures/agents" >/dev/null 2>&1; then
	echo "FAIL --check accepted a stale file"
	rc=1
else echo "ok   --check rejects a stale file"; fi
python3 "$SYNC" --out "$TMP/real.json" || rc=1
python3 - "$TMP/real.json" "$OC/../agents" <<'PY' || rc=1
import json, sys
from pathlib import Path
data = json.load(open(sys.argv[1]))["agent"]
root = Path(sys.argv[2])
want = {p.stem for p in root.glob("*.md")} | {p.name for p in root.iterdir() if p.is_dir() and (p / f"{p.name}.md").is_file()}
assert set(data) == want, (sorted(data), sorted(want))
for name, a in data.items():
    assert a["mode"] == "subagent" and a["model"] == "provider/model-name" and a["prompt"].strip() and a["description"], name
    assert set(a.get("tools", {}).values()) <= {False}, name  # switch-offs only, never a grant
print(f"ok   {len(data)} roles generated from agents/ with placeholder models")
PY
echo "== bridge unit tests =="
if command -v bun >/dev/null 2>&1; then
	bun "$HERE/bridge.test.ts" || rc=1
	AIARCH_BRIDGE_EVENTS_DIR="$TMP/bridge-events" bun "$HERE/review-integration.test.ts" || rc=1
	python3 -B "$OC/../plugins/gates/tests/run_hooks.py" --bridge-events "$TMP/bridge-events" || rc=1
else
	echo "NOT RUN: bridge tests need bun (not found); the extensionless TypeScript imports do not run under plain node"
	echo "OPENCODE TESTS INCOMPLETE"
	exit 1
fi
if [ "$rc" -eq 0 ]; then echo "OPENCODE TESTS PASSED"; else echo "OPENCODE TESTS FAILED"; fi
exit "$rc"
