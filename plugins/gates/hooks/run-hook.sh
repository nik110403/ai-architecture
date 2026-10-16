#!/usr/bin/env bash
# Usage: run-hook.sh <pretool|stop|session> <script.py>
# Runs a gate hook under a WALL-CLOCK cap (coreutils `timeout`, inside the 10 s hook timeout).
# Claude Code lets a call through when a hook times out, so the cap lives here, where CPU starvation
# cannot defeat it. A timeout, crash, missing python3/timeout, or non-JSON output fails CLOSED:
# pretool -> deny, stop -> block (silent if stop_hook_active, so the turn can end), session -> systemMessage.
HERE="${BASH_SOURCE[0]%/*}"; [ "$HERE" = "${BASH_SOURCE[0]}" ] && HERE=.   # builtins only: must work even with an empty PATH
kind="$1"; script="$2"
# Wall budget (hook limit 10 s): stdin copy <= 2+1 s, guard <= 3+1 s, stop_hook_active check <= 1.2 s => < 9 s worst case.
# stdin is NOT read with bash `read` (1 byte/syscall, ~2 s/MB before any timeout starts): `cat` copies it to a
# temp file under its own timeout, and the guard then reads that file.
tmp=""; fd3=""
trap '[ -n "$tmp" ] && rm -f -- "$tmp"' EXIT
fail() {
  local m="ai-architecture-gates: $script $1 -- failing closed."
  case "$kind" in
    pretool) printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"'"$m"'"}}' ;;
    stop)
      # silent only when the payload JSON says stop_hook_active is exactly true (so the turn can end)
      src="$tmp"; [ -n "$fd3" ] && src=/dev/fd/5   # fd 5: own read fd at offset 0, never read before
      if [ -n "$src" ] && timeout -k 0.2 1 python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("stop_hook_active") is True else 1)' "$src" >/dev/null 2>&1; then
        printf '{}\n'
      else
        printf '%s\n' '{"decision":"block","reason":"'"$m"'"}'
      fi ;;
    *) printf '%s\n' '{"systemMessage":"'"$m"'"}' ;;
  esac
  exit 0
}
command -v python3 >/dev/null 2>&1 || fail "needs python3 (not found)"
command -v timeout >/dev/null 2>&1 || fail "needs coreutils timeout (not found)"
command -v cat >/dev/null 2>&1 || fail "needs cat (not found)"
command -v mktemp >/dev/null 2>&1 || fail "needs mktemp (not found)"
tmp="$(mktemp)" || { tmp=""; fail "mktemp failed"; }
# open fd 3 read-write on the payload file and unlink it BEFORE any data is written: there is no window in
# which the path can be swapped, and a SIGKILL leaves no leftover. The payload is then written through fd 3.
exec 3>"$tmp" 4<"$tmp" 5<"$tmp" || fail "could not open stdin copy"
rm -f -- "$tmp"; tmp=""; fd3=1
timeout -k 1 2 cat >&3 || fail "could not read stdin in time"
# separate read fds (4: guard, 5: stop probe) start at offset 0 on any OS, no reopen needed
out="$(timeout -k 1 3 python3 -I "$HERE/$script" <&4)"; rc=$?
[ "$rc" -eq 0 ] || fail "failed or timed out (rc=$rc)"
case "$out" in ""|"{"*) ;; *) fail "produced no valid decision" ;; esac
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
