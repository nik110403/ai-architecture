#!/usr/bin/env bash
# Runs every gates test with an empty temporary HOME, so nothing depends on the caller's
# home directory, user config, or ~/.claude. Usage: bash plugins/gates/tests/run.sh
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || {
	echo "python3 is required"
	exit 1
}
command -v git >/dev/null 2>&1 || {
	echo "git is required"
	exit 1
}
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME" "$HERE/.pwseam-active"' EXIT # set BEFORE the flag exists
export HOME="$TMP_HOME"
# gates_config.home() reads the passwd database, not $HOME. Hooks run under `python3 -I`, so the seam is
# a flag file (removed on exit) naming TMP_HOME plus GATES_TEST_PW_HOME; both must match.
printf '%s %s' "$$" "$TMP_HOME" >"$HERE/.pwseam-active"
export GATES_TEST_PW_HOME="$TMP_HOME"
unset AIARCH_CONFIG CLAUDE_HOOK_STATE_DIR CLAUDE_PLUGIN_DATA
export PYTHONDONTWRITEBYTECODE=1
rc=0
echo "== destructive-command guard =="
python3 "$HERE/run_destructive.py" || rc=1
echo "== edit budget / no-code / review / state / resume =="
python3 "$HERE/run_hooks.py" || rc=1
echo "== config scope =="
python3 "$HERE/run_config_scope.py" || rc=1
echo "== edit budget transactions =="
python3 "$HERE/test_edit_budget.py" || rc=1
[ "$rc" -eq 0 ] && echo "ALL GATES TESTS PASSED" || echo "GATES TESTS FAILED"
exit "$rc"
