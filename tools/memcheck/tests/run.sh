#!/usr/bin/env bash
# memcheck tests. Copies the fixtures to a temp dir, runs tools/memcheck/memcheck against them.
# Covers the scan checks, --fix-index, --fix-index --shorten, and the realpath / trust-root
# refusals. Exit 0 if all pass, 1 otherwise. Needs bash, perl, flock, realpath.
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SELF_DIR/../memcheck"
FIX="$SELF_DIR/fixtures"

pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }
check() { if [ "$1" = 0 ]; then ok "$2"; else bad "$2"; fi; }

TMPD="$(mktemp -d)"
cleanup() { rm -rf "$TMPD"; }
trap cleanup EXIT

bash -n "$BIN"; check $? "T1 bash -n memcheck clean"
bash -n "$SELF_DIR/run.sh"; check $? "T2 bash -n self clean"

# ---- shorten fixture ------------------------------------------------------------------------
ROOT="$TMPD/sh"; mkdir -p "$ROOT"; cp -r "$FIX/shorten/demo" "$ROOT/"
MEMDIR="$ROOT/demo/memory"
SHORT_LINE="- [Short Entry](short-entry.md) — a short hook under budget, well inside limits."
ORIG_TARGETS="$(grep -oE '\]\([^)]*\.md\)' "$MEMDIR/MEMORY.md" | sort)"
ORIG_HASH="$(md5sum "$MEMDIR/MEMORY.md" | cut -d' ' -f1)"

[ "${#SHORT_LINE}" -le 100 ]; check $? "T3 fixture short line is <=100 chars"

SCAN_OUT="$("$BIN" --root "$ROOT")"
n="$(printf '%s\n' "$SCAN_OUT" | grep -c 'ℹ️ demo/MEMORY.md:[0-9]')"
[ "$n" -eq 5 ]; check $? "T4 scan flags exactly 5 long index lines (got $n)"
printf '%s\n' "$SCAN_OUT" | grep -q '⛔'; [ $? -ne 0 ]; check $? "T4b scan has no drift mark on this fixture"

DRY_OUT="$("$BIN" --root "$ROOT" --dir "$MEMDIR" --fix-index --shorten)"
n="$(printf '%s\n' "$DRY_OUT" | grep -c 'MEMORY.md ::')"
[ "$n" -eq 5 ]; check $? "T5 dry-run lists exactly 5 lines (got $n)"
printf '%s\n' "$DRY_OUT" | grep -qF -e "$SHORT_LINE"; [ $? -ne 0 ]; check $? "T5b dry-run does not touch the short line"
[ "$ORIG_HASH" = "$(md5sum "$MEMDIR/MEMORY.md" | cut -d' ' -f1)" ]; check $? "T5c dry-run wrote nothing"

APPLY1="$("$BIN" --root "$ROOT" --dir "$MEMDIR" --fix-index --shorten --apply)"
n="$(printf '%s\n' "$APPLY1" | grep -c '✅ shortened:')"
[ "$n" -eq 5 ]; check $? "T6 first --apply shortens exactly 5 lines (got $n)"
grep -qFx -e "$SHORT_LINE" "$MEMDIR/MEMORY.md"; check $? "T6b short line untouched after --apply"
allshort=1
while IFS= read -r line; do hook="${line#*— }"; [ "${#hook}" -le 100 ] || allshort=0; done < <(grep -E '^- \[' "$MEMDIR/MEMORY.md")
[ "$allshort" -eq 1 ]; check $? "T6c every hook <=100 chars after --apply"

declare -A tokens=(
  [quoted-string]="Cannot find module 'left-pad/v3'"
  [tilde-path]="~/.config/example-tool/sync-hook.sh"
  [bin-path]="bin/example-sync"
  [negation]="NOT enforced"
  [date]="2026-09-07"
)
for label in "${!tokens[@]}"; do
  grep -qF "${tokens[$label]}" "$MEMDIR/MEMORY.md"; check $? "T7 token survives: $label"
done
[ "$ORIG_TARGETS" = "$(grep -oE '\]\([^)]*\.md\)' "$MEMDIR/MEMORY.md" | sort)" ]; check $? "T8 link targets unchanged"

B="$(md5sum "$MEMDIR/MEMORY.md" | cut -d' ' -f1)"
APPLY2="$("$BIN" --root "$ROOT" --dir "$MEMDIR" --fix-index --shorten --apply)"
[ -z "$APPLY2" ] && [ "$B" = "$(md5sum "$MEMDIR/MEMORY.md" | cut -d' ' -f1)" ]; check $? "T9 second --apply is a no-op"
[ -z "$("$BIN" --root "$ROOT" --dir "$MEMDIR" --fix-index --shorten)" ]; check $? "T10 dry-run after --apply proposes nothing"
"$BIN" --root "$ROOT" >/dev/null; check $? "T11 scan exits 0 (report-only)"
grep -q 'shorten-index' "$ROOT/.memcheck-audit.log"; check $? "T12 --apply wrote an audit-log line under the root"

# ---- scan fixture ---------------------------------------------------------------------------
R2="$TMPD/sc"; mkdir -p "$R2"; cp -r "$FIX/scan/demo" "$R2/"
# a dangling index line is added at test time (a checked-in dangling link would fail the collection link check)
printf '%s\n' '- [Gone](gone.md) — index line whose target file does not exist' >> "$R2/demo/memory/MEMORY.md"
OUT="$("$BIN" --root "$R2")"
echo "$OUT" | grep -q '⛔ demo/orphan-note.md — not referenced'; check $? "T13 orphan file flagged as index drift"
echo "$OUT" | grep -q '⛔ demo/MEMORY.md → missing target: gone.md'; check $? "T14 index link to missing file flagged"
echo "$OUT" | grep -q 'ℹ️ demo/alpha.md: \[\[missing-note\]\]'; check $? "T15 dangling wikilink flagged"
echo "$OUT" | grep -q 'ℹ️ demo/loner.md'; check $? "T16 isolated memory flagged"
echo "$OUT" | grep -q 'ℹ️ demo/alpha.md: 2020-01-15'; check $? "T17 stale date flagged"
echo "$OUT" | grep -q 'Summary: 5 files scanned'; check $? "T18 summary counts files"

FIXOUT="$("$BIN" --root "$R2" --fix-index)"
echo "$FIXOUT" | grep -qF -e '- [Orphan Note](orphan-note.md) — Present on disk but absent from the index.'; check $? "T19 fix-index proposes the orphan line"
echo "$FIXOUT" | grep -q 'nofm.md — no frontmatter'; check $? "T20 file without frontmatter reported, not fixed"
"$BIN" --root "$R2" --fix-index --apply >/dev/null
grep -qF '(orphan-note.md)' "$R2/demo/memory/MEMORY.md"; check $? "T21 --apply appended the orphan line"
c1="$(grep -c 'orphan-note.md' "$R2/demo/memory/MEMORY.md")"
"$BIN" --root "$R2" --fix-index --apply >/dev/null
[ "$c1" = "$(grep -c 'orphan-note.md' "$R2/demo/memory/MEMORY.md")" ]; check $? "T22 second --apply adds no duplicate"
grep -qF 'gone.md' "$R2/demo/memory/MEMORY.md"; check $? "T23 dangling index line never auto-deleted"

# ---- hostile-environment / trust-root refusals ----------------------------------------------
R3="$TMPD/host"; mkdir -p "$R3" "$TMPD/outside/memory" "$TMPD/outside2"
cp -r "$FIX/scan/demo" "$R3/"
cp "$FIX/scan/demo/memory/"*.md "$TMPD/outside/memory/"
mkdir -p "$R3/evil-slug"; ln -s "$TMPD/outside/memory" "$R3/evil-slug/memory"
mkdir -p "$R3/demo/memory/deep/memory"; cp "$FIX/scan/demo/memory/alpha.md" "$R3/demo/memory/deep/memory/"

REF="$("$BIN" --root "$R3" --dir "$R3/evil-slug/memory" --fix-index --apply)"
echo "$REF" | grep -q 'refused'; check $? "T24 --dir symlink escape refused"
[ ! -e "$TMPD/outside/memory/.memcheck-audit.log" ] && ! grep -q 'orphan-note.md)' "$TMPD/outside/memory/MEMORY.md"; check $? "T24b nothing written outside the root"
REF="$("$BIN" --root "$R3" --dir "$R3/demo/memory/deep/memory" --fix-index)"
echo "$REF" | grep -q 'refused'; check $? "T25 nested memory dir refused"
REF="$("$BIN" --root "$R3" --dir "$TMPD/outside/memory" --fix-index)"
echo "$REF" | grep -q 'refused'; check $? "T26 --dir outside the root refused"
SCAN="$("$BIN" --root "$R3" 2>&1)"
echo "$SCAN" | grep -q 'skipping .*evil-slug/memory'; check $? "T27 scan skips symlinked memory dir that leaves the root"
echo "$SCAN" | grep -v 'skipping' | grep -q 'evil-slug/'; [ $? -ne 0 ]; check $? "T27b escaped dir contents not scanned"

# env vars cannot relocate the trust root or the audit log
R4="$TMPD/env"; mkdir -p "$R4"; cp -r "$FIX/scan/demo" "$R4/"
touch "$TMPD/outside2/.memcheck-test-sentinel"
MEMCHECK_HOME_ROOT="$TMPD/outside2" MEMCHECK_GLOB="$TMPD/outside/memory" LOOP_AUDIT_LOG="$TMPD/outside2/audit.log" \
  "$BIN" --root "$R4" --dir "$R4/demo/memory" --fix-index --apply >/dev/null
[ ! -e "$TMPD/outside2/audit.log" ] && [ -s "$R4/.memcheck-audit.log" ]; check $? "T28 env vars cannot redirect root or audit log"

# explicit --audit-log honored
R5="$TMPD/al"; mkdir -p "$R5"; cp -r "$FIX/scan/demo" "$R5/"
"$BIN" --root "$R5" --fix-index --apply --audit-log "$TMPD/custom.log" >/dev/null
grep -q 'fix-index' "$TMPD/custom.log"; check $? "T29 --audit-log path honored"

# bracket filename refused
R6="$TMPD/br"; mkdir -p "$R6/demo/memory"; cp "$FIX/scan/demo/memory/alpha.md" "$R6/demo/memory/a[1].md"
printf '%s\n' '- [X](x.md) — y' > "$R6/demo/memory/MEMORY.md"
"$BIN" --root "$R6" --fix-index --apply | grep -q 'refusing to index'; check $? "T30 bracket filename refused"

# sanitization: injected description cannot break out of the index line
R7="$TMPD/san"; mkdir -p "$R7/demo/memory"
printf -- '---\nname: bad\ndescription: x](http://evil) [y `z`\n---\nbody\n' > "$R7/demo/memory/bad.md"
: > "$R7/demo/memory/MEMORY.md"
"$BIN" --root "$R7" --fix-index --apply >/dev/null
grep -qF '(bad.md) — xhttp://evil y z' "$R7/demo/memory/MEMORY.md"; check $? "T31 description bracket/paren/backtick chars stripped"

# usage errors
"$BIN" >/dev/null 2>&1; [ $? -eq 2 ]; check $? "T32 missing --root is a usage error (exit 2)"
"$BIN" --root "$TMPD/nope" >/dev/null 2>&1; [ $? -eq 2 ]; check $? "T33 nonexistent --root is a usage error"

# symlink refusals (security review): lock / index / audit log must never be followed
SENT="$TMPD/symlink-target"
mk_sym() {  # name -> prints root; a fresh copy of the scan fixture
  local r="$TMPD/$1"; mkdir -p "$r"; cp -r "$FIX/scan/demo" "$r/"; echo "$r"
}
printf 'KEEP\n' > "$SENT"
R8="$(mk_sym symlock)"; ln -s "$SENT" "$R8/demo/memory/MEMORY.md.lock"
"$BIN" --root "$R8" --fix-index >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && [ "$(cat "$SENT")" = "KEEP" ]; check $? "T34 symlinked lock refused (rc=$rc) and target not truncated, even in dry-run"
R9="$(mk_sym symidx)"; mv "$R9/demo/memory/MEMORY.md" "$TMPD/real-index.md"; ln -s "$TMPD/real-index.md" "$R9/demo/memory/MEMORY.md"
H="$(md5sum "$TMPD/real-index.md" | cut -d' ' -f1)"
"$BIN" --root "$R9" --fix-index --apply >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && [ "$H" = "$(md5sum "$TMPD/real-index.md" | cut -d' ' -f1)" ]; check $? "T35 symlinked index refused (rc=$rc), target unchanged"
R10="$(mk_sym symaudit)"; ln -s "$SENT" "$R10/.memcheck-audit.log"
"$BIN" --root "$R10" --fix-index --apply >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && [ "$(cat "$SENT")" = "KEEP" ]; check $? "T36 symlinked audit log refused (rc=$rc), target not appended"

# python runs isolated (-I): a ./re.py in the cwd must not execute
R11="$TMPD/pyiso"; mkdir -p "$R11"; cp -r "$FIX/shorten/demo" "$R11/"
CWDD="$TMPD/hostile-cwd"; mkdir -p "$CWDD"
printf 'open(%s,"w").write("pwned")\n' "\"$TMPD/pwned-marker\"" > "$CWDD/re.py"
( cd "$CWDD" && "$BIN" --root "$R11" --dir "$R11/demo/memory" --fix-index --shorten --apply >/dev/null 2>&1 )
[ ! -e "$TMPD/pwned-marker" ] && grep -q '' "$R11/demo/memory/MEMORY.md"; check $? "T37 cwd ./re.py is not imported by the shorten helper"

echo "----"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
