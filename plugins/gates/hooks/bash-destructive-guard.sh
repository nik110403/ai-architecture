#!/usr/bin/env bash
# PreToolUse (Bash) wrapper for bash-destructive-guard.py: wall-clock cap (7 s) and fail-closed
# handling live in run-hook.sh. A missing python3 or timeout, a crash, or a timeout denies the command.
# Uses only bash builtins before the exec so even an empty PATH reaches the deny below.
shopt -s execfail
H="${BASH_SOURCE[0]%/*}"; [ "$H" = "${BASH_SOURCE[0]}" ] && H=.
# run-hook.sh unreadable or not a regular file: exec of a fresh bash would exit 127 with no output (= allow).
[ -f "$H/run-hook.sh" ] && [ -r "$H/run-hook.sh" ] || { printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"guard error: run-hook.sh missing or unreadable -- failing closed."}}'; exit 0; }
exec "$BASH" "$H/run-hook.sh" pretool bash-destructive-guard.py
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"guard error: run-hook.sh not runnable -- failing closed."}}'
