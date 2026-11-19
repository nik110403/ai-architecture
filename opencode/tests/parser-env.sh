#!/usr/bin/env bash
# Checks the structural-parser location the acceptance test (u11-structural.test.ts) will use.
# AIARCH_PARSER_DIR names a directory whose node_modules holds web-tree-sitter 0.25.10 and tree-sitter-bash 0.25.0,
# kept OUTSIDE the source tree (check_collection rejects node_modules in it). One-line offline-capable install:
#   bun add --cwd "$AIARCH_PARSER_DIR" web-tree-sitter@0.25.10 tree-sitter-bash@0.25.0
# Set but unusable (missing or wrong version) => FAIL (exit 1). Unset => exit 0 and say loudly that the pins are not enforced.
if [ -z "${AIARCH_PARSER_DIR:-}" ]; then
	echo "NOTE parser-env: AIARCH_PARSER_DIR is not set; the structural parser test uses whatever resolves from the tree and SKIPS if nothing does"
	exit 0
fi
rc=0
for spec in web-tree-sitter=0.25.10 tree-sitter-bash=0.25.0; do
	pkg="${spec%%=*}"
	want="${spec#*=}"
	f="$AIARCH_PARSER_DIR/node_modules/$pkg/package.json"
	got="$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
	if [ "$got" = "$want" ]; then echo "ok   parser-env: $pkg $got"; else
		echo "FAIL parser-env: $pkg ${want} not found under AIARCH_PARSER_DIR=$AIARCH_PARSER_DIR (got '${got:-missing}'); run: bun add --cwd \"\$AIARCH_PARSER_DIR\" web-tree-sitter@0.25.10 tree-sitter-bash@0.25.0"
		rc=1
	fi
done
exit "$rc"
