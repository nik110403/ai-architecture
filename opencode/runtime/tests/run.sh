#!/usr/bin/env bash
# OpenCode continuity runtime tests (bun): fake OpenCode client, the real Python CLI in a temporary project.
# Usage (from the repository root): bash opencode/runtime/tests/run.sh
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || {
	echo "python3 is required"
	exit 1
}
if ! command -v bun >/dev/null 2>&1; then
	echo "NOT RUN: the OpenCode runtime tests need bun (not found)"
	echo "RUNTIME TESTS INCOMPLETE"
	exit 1
fi
TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT
export HOME="$TMP/home" AI_ARCH_FREEZE_DIR="$TMP/freeze" PYTHONDONTWRITEBYTECODE=1
mkdir -p "$HOME"
unset AI_ARCH_JOB_MARKER XDG_STATE_HOME
rc=0
bun "$HERE/runtime.test.ts" || rc=1
if [ "$rc" -eq 0 ]; then echo "RUNTIME TESTS PASSED"; else echo "RUNTIME TESTS FAILED"; fi
exit "$rc"
