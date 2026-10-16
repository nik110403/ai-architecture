#!/usr/bin/env bash
# Model-rotation tests: the Claude Code hook (python3) and the OpenCode module (bun).
# Usage (from the repository root): bash opencode/rotation/tests/run.sh
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../../.." && pwd)"
rc=0
echo "== Claude Code hook =="
bash "$ROOT/plugins/gates/hooks/rotation/tests/test-model-rotate.sh" || rc=1
echo "== OpenCode module =="
if command -v bun >/dev/null 2>&1; then
  bun "$HERE/rotation.test.ts" || rc=1
else
  echo "NOT RUN: the OpenCode module tests need bun (not found)"
fi
if [ "$rc" -eq 0 ]; then echo "ROTATION TESTS PASSED"; else echo "ROTATION TESTS FAILED"; fi
exit "$rc"
