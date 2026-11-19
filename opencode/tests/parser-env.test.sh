#!/usr/bin/env bash
# Tests for the AIARCH_PARSER_DIR contract: set-but-unusable FAILS, unset skips loudly, a good dir passes.
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"
trap 'rm -rf -- "$T"' EXIT
rc=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; rc=1; }
# a copy of the bridge + tests + corpus with no node_modules anywhere above it, so nothing resolves by accident
R="$T/repo"
mkdir -p "$R/plugins/gates/tests" "$T/empty/node_modules" "$T/fake/node_modules/web-tree-sitter" "$T/fake/node_modules/tree-sitter-bash" "$T/old/node_modules/web-tree-sitter" "$T/old/node_modules/tree-sitter-bash"
cp -r "$HERE/.." "$R/opencode"
cp -r "$HERE/../../plugins/gates/tests/fixtures" "$R/plugins/gates/tests/"
printf '{"version": "0.25.10"}\n' >"$T/fake/node_modules/web-tree-sitter/package.json"
printf '{"version": "0.25.0"}\n' >"$T/fake/node_modules/tree-sitter-bash/package.json"
printf '{"version": "0.27.1"}\n' >"$T/old/node_modules/web-tree-sitter/package.json"
printf '{"version": "0.25.0"}\n' >"$T/old/node_modules/tree-sitter-bash/package.json"

if AIARCH_PARSER_DIR="$T/empty" bash "$HERE/parser-env.sh" >/dev/null; then bad "empty AIARCH_PARSER_DIR must fail"; else ok "empty AIARCH_PARSER_DIR fails"; fi
if AIARCH_PARSER_DIR="$T/old" bash "$HERE/parser-env.sh" >/dev/null; then bad "wrong web-tree-sitter version must fail"; else ok "wrong parser version fails"; fi
if AIARCH_PARSER_DIR="$T/fake" bash "$HERE/parser-env.sh" >/dev/null; then ok "pinned versions pass"; else bad "pinned versions must pass"; fi
if env -u AIARCH_PARSER_DIR bash "$HERE/parser-env.sh" | grep -q "AIARCH_PARSER_DIR is not set"; then ok "unset says so"; else bad "unset must say so"; fi
if command -v bun >/dev/null 2>&1; then
	# set but the modules do not load: the acceptance test must FAIL, never skip
	out="$(env -u AIARCH_REQUIRE_PARSER AIARCH_PARSER_DIR="$T/fake" bun --no-install "$R/opencode/tests/u11-structural.test.ts" 2>&1)"
	st=$?
	if [ "$st" -ne 0 ] && ! grep -q SKIPPED <<<"$out" && grep -q "^FAIL" <<<"$out"; then ok "unloadable AIARCH_PARSER_DIR fails the structural test"; else bad "unloadable AIARCH_PARSER_DIR must fail, not skip (rc=$st)"; fi
	# unset and nothing resolves: loud skip, rc 0
	out="$(env -u AIARCH_PARSER_DIR -u AIARCH_REQUIRE_PARSER bun --no-install "$R/opencode/tests/u11-structural.test.ts" 2>&1)"
	st=$?
	if [ "$st" -eq 0 ] && grep -q "SKIPPED structural corpus" <<<"$out"; then ok "unset and unresolvable skips with a reason"; else bad "unset must skip loudly (rc=$st)"; fi
else echo "NOTE bun not found: parser-env bun cases not run"; fi
exit "$rc"
