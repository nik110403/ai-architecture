#!/usr/bin/env bash
# gate-check tests: parser, exit codes (0 met, 1 unmet, 2 usage), EXPECT substring/regex,
# manual-gate evidence rule, ABANDON handling, --dry-run. Fixtures live under mktemp -d.
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SELF_DIR/../gate-check"
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }
TMPD="$(mktemp -d)"
cleanup() { rm -rf "$TMPD"; }
trap cleanup EXIT

if python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$BIN"; then ok "T1 gate-check parses"; else bad "T1 gate-check parses"; fi

mkdir -p "$TMPD/met" "$TMPD/unmet" "$TMPD/empty"
cat > "$TMPD/met/GATES.md" <<'G'
# Gates: sample

- [ ] G1: exit status only
  CHECK: true
  EVIDENCE: pending

- [ ] G2: substring match
  CHECK: echo "8/8 passed"
  EXPECT: 8/8 passed

- [ ] G3: regex match
  CHECK: printf 'alpha\nbeta 12\n'
  EXPECT: /^beta \d+$/

- [x] G4: manual with evidence
  EVIDENCE: reviewed by a second person on 2026-01-02

- [ ] G5: surrendered gate
  CHECK: false

ABANDON: G5 not reachable in this environment
G
out="$("$BIN" "$TMPD/met")"; rc=$?
if [ "$rc" -eq 0 ] && grep -q 'ALL MET (4 met, 1 abandoned)' <<<"$out"; then
  ok "T2 all gates met -> exit 0 (abandoned reported separately)"
else
  bad "T2 all met (rc=$rc) $out"
fi

cat > "$TMPD/unmet/GATES.md" <<'G'
# Gates: sample

- [ ] G1: command fails
  CHECK: false

- [ ] G2: wrong output
  CHECK: echo nope
  EXPECT: yes

- [x] G3: checked but evidence still pending
  EVIDENCE: pending

- [ ] G4: manual and unchecked
G
out="$("$BIN" "$TMPD/unmet/GATES.md")"; rc=$?
if [ "$rc" -eq 1 ] && grep -q 'UNMET: 4' <<<"$out"; then
  ok "T3 unmet gates -> exit 1, all four counted"
else
  bad "T3 unmet (rc=$rc) $out"
fi
if grep -q 'checked but EVIDENCE pending' <<<"$out"; then ok "T4 checked-without-evidence is a claim, not proof"; else bad "T4 evidence rule"; fi

out="$("$BIN" "$TMPD/met" --dry-run)"; rc=$?
if [ "$rc" -eq 0 ] && grep -q 'dry-run, nothing executed' <<<"$out"; then ok "T5 --dry-run lists without running"; else bad "T5 dry-run (rc=$rc)"; fi

"$BIN" "$TMPD/empty" >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 2 ]; then ok "T6 missing GATES.md -> exit 2"; else bad "T6 missing file (rc=$rc)"; fi
printf '# nothing here\n' > "$TMPD/empty/GATES.md"
"$BIN" "$TMPD/empty" >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 2 ]; then ok "T7 no gates found -> exit 2"; else bad "T7 no gates (rc=$rc)"; fi

mkdir -p "$TMPD/slow"
printf -- '- [ ] G1: too slow\n  CHECK: sleep 5\n' > "$TMPD/slow/GATES.md"
"$BIN" "$TMPD/slow" --timeout 1 >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 1 ]; then ok "T8 timed-out check counts as unmet"; else bad "T8 timeout (rc=$rc)"; fi

echo "----"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
