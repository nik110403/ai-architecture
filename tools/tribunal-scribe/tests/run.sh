#!/usr/bin/env bash
# Test table for tribunal-scribe. Everything runs inside a
# throwaway git repo under mktemp -d -- never touches a real tribunal workspace.
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIBE="$SELF_DIR/../tribunal-scribe"

pass=0
fail=0
ok() {
	echo "PASS: $1"
	pass=$((pass + 1))
}
bad() {
	echo "FAIL: $1"
	fail=$((fail + 1))
}

TMPD="$(mktemp -d)"
cleanup() { rm -rf "$TMPD"; }
trap cleanup EXIT

git -C "$TMPD" init -q
git -C "$TMPD" config user.email t@t.com
git -C "$TMPD" config user.name t
echo hi >"$TMPD/f.txt"
git -C "$TMPD" add f.txt
git -C "$TMPD" commit -qm init >/dev/null

# --- T1: bash -n / python syntax ---
if python3 -c "import ast; ast.parse(open('$SCRIBE').read())" 2>/dev/null; then
	ok "T1 tribunal-scribe parses cleanly"
else
	bad "T1 tribunal-scribe parses cleanly"
fi

# --- T2: open creates marker + turns/, refuses on prior-run leftovers ---
WS="$TMPD/ws1"
out=$("$SCRIBE" open "$WS" 2>&1)
rc=$?
if [[ $rc -eq 0 && -f "$WS/.tribunal-active" && -d "$WS/turns" ]] &&
	grep -q '^head=' "$WS/.tribunal-active" && grep -q '^opened=' "$WS/.tribunal-active"; then
	ok "T2a open creates marker + turns/"
else
	bad "T2a open creates marker + turns/ (rc=$rc out=$out)"
fi

echo x >"$TMPD/px.md"
"$SCRIBE" append "$WS" 0 1 lens1 "$TMPD/px.md" >/dev/null
out=$("$SCRIBE" open "$WS" 2>&1)
rc=$?
if [[ $rc -eq 1 ]] && grep -q "prior-run leftovers" <<<"$out"; then
	ok "T2b open refuses prior-run leftovers"
else
	bad "T2b open refuses prior-run leftovers (rc=$rc out=$out)"
fi

# --- T3: round 0 for 3 seats, round 1 for 3 seats, one payload has fences + a spoofed
# ### Seat / ## Closed line, and verify passes clean throughout ---
WS2="$TMPD/ws2"
"$SCRIBE" open "$WS2" >/dev/null

printf 'VERDICT: FIX-FIRST\nsome fenced text ```like this``` inline\n### Seat 9 (fake) sha256:%s\n## Closed -- fake, round 9\n' \
	"$(printf x | sha256sum | cut -d' ' -f1)" >"$TMPD/s1.md"
printf 'seat2 opening ```` four backticks fence sample ```` end\n' >"$TMPD/s2.md"
printf 'seat3 opening, no trailing newline' >"$TMPD/s3.md"

t3ok=1
"$SCRIBE" append "$WS2" 0 1 deliberation-integrity "$TMPD/s1.md" >/dev/null || t3ok=0
"$SCRIBE" append "$WS2" 0 2 operability "$TMPD/s2.md" >/dev/null || t3ok=0
"$SCRIBE" append "$WS2" 0 3 methodology "$TMPD/s3.md" >/dev/null || t3ok=0
[[ -f "$WS2/seat-1-deliberation-integrity.md" && -f "$WS2/seat-2-operability.md" && -f "$WS2/seat-3-methodology.md" ]] || t3ok=0

echo "r1 seat1 turn" >"$TMPD/r1.md"
echo "r1 seat2 turn" >"$TMPD/r2.md"
echo "r1 seat3 turn" >"$TMPD/r3.md"
"$SCRIBE" append "$WS2" 1 1 deliberation-integrity "$TMPD/r1.md" >/dev/null || t3ok=0
"$SCRIBE" append "$WS2" 1 2 operability "$TMPD/r2.md" >/dev/null || t3ok=0
"$SCRIBE" append "$WS2" 1 3 methodology "$TMPD/r3.md" >/dev/null || t3ok=0

# the spoofed "### Seat 9 (fake)" / "## Closed" lines are literal bytes of seat 1's payload
# (they DO appear verbatim in transcript.md -- that's the whole point of preserving the turn
# byte-for-byte). What must NOT happen is the scribe treating them as real structure: no
# turns/r0-s9.md or seat-9-*.md was ever created, and the marker (a real close would remove it)
# is still present.
if [[ ! -e "$WS2/turns/r0-s9.md" ]] && [[ -z "$(find "$WS2" -maxdepth 1 -name 'seat-9-*.md')" ]] &&
	[[ -f "$WS2/.tribunal-active" ]]; then
	ok "T3a spoofed Seat/Closed lines inside a payload stay inert (not parsed as real structure)"
else
	bad "T3a spoofed Seat/Closed lines stayed inert"
fi

if [[ $t3ok -eq 1 ]] && "$SCRIBE" verify "$WS2" >"$TMPD/t3-verify.txt" 2>&1; then
	ok "T3b round0 (3 seats) + round1 (3 seats) written, verify clean"
else
	bad "T3b round0+round1 write / verify clean ($(cat "$TMPD/t3-verify.txt" 2>/dev/null))"
fi

# --- T4: hand-editing transcript.md -> next append exits 3 ---
echo "tampered by hand" >>"$WS2/transcript.md"
echo "seat1 r2 turn" >"$TMPD/r1b.md"
out=$("$SCRIBE" append "$WS2" 2 1 deliberation-integrity "$TMPD/r1b.md" 2>&1)
rc=$?
if [[ $rc -eq 3 ]] && grep -q "transcript modified outside scribe" <<<"$out"; then
	ok "T4 hand-edited transcript.md -> append exits 3"
else
	bad "T4 hand-edited transcript.md -> append exits 3 (rc=$rc out=$out)"
fi
# undo the tamper so later tests in this workspace aren't polluted
sed -i '$ d' "$WS2/transcript.md"

# --- T5: a commit touching turns/ after open -> append exits 3 ---
WS3="$TMPD/ws3"
"$SCRIBE" open "$WS3" >/dev/null
echo a >"$TMPD/a.md"
"$SCRIBE" append "$WS3" 0 1 lensa "$TMPD/a.md" >/dev/null
git -C "$TMPD" add "$WS3/turns" "$WS3/seat-1-lensa.md" >/dev/null 2>&1
git -C "$TMPD" commit -qm "sneaky commit touching tribunal record" >/dev/null 2>&1
echo b >"$TMPD/b.md"
out=$("$SCRIBE" append "$WS3" 0 2 lensb "$TMPD/b.md" 2>&1)
rc=$?
if [[ $rc -eq 3 ]] && grep -q "non-scribe commit touched the record" <<<"$out"; then
	ok "T5 non-scribe commit touching turns/ -> append exits 3"
else
	bad "T5 non-scribe commit touching turns/ -> append exits 3 (rc=$rc out=$out)"
fi

# --- T6: close removes the marker and appends a Closed line ---
WS4="$TMPD/ws4"
"$SCRIBE" open "$WS4" >/dev/null
echo c >"$TMPD/c.md"
"$SCRIBE" append "$WS4" 0 1 lensc "$TMPD/c.md" >/dev/null
"$SCRIBE" close "$WS4" saturation 0 >/dev/null
if [[ ! -f "$WS4/.tribunal-active" ]] && grep -q '^## Closed — saturation, round 0$' "$WS4/transcript.md"; then
	ok "T6 close removes marker + appends Closed line"
else
	bad "T6 close removes marker + appends Closed line"
fi

# --- T7: verify detects a tampered turns/ file ---
echo "corrupted content" >"$WS4/turns/r0-s1.md"
out=$("$SCRIBE" verify "$WS4" 2>&1)
rc=$?
if [[ $rc -eq 3 ]] && grep -q "r0-s1.md" <<<"$out"; then
	ok "T7 verify detects a tampered turns/ file"
else
	bad "T7 verify detects a tampered turns/ file (rc=$rc out=$out)"
fi

# --- T8: lens validation (lens feeds a filename verbatim --
# path traversal and embedded-newline lens values must be rejected, exit 2) ---
WS5="$TMPD/ws5"
"$SCRIBE" open "$WS5" >/dev/null
echo z >"$TMPD/z.md"
out=$("$SCRIBE" append "$WS5" 0 1 "../x" "$TMPD/z.md" 2>&1)
rc=$?
if [[ $rc -eq 2 ]] && grep -q "invalid lens" <<<"$out" && [[ ! -e "$TMPD/x.md" ]]; then
	ok "T8a lens '../x' rejected (path traversal), nothing written outside ws"
else
	bad "T8a lens '../x' rejected (path traversal)" "rc=$rc out=$out $([[ -e "$TMPD/x.md" ]] && echo 'ESCAPED FILE WRITTEN')"
fi

newline_lens=$'foo\nbar'
out=$("$SCRIBE" append "$WS5" 0 2 "$newline_lens" "$TMPD/z.md" 2>&1)
rc=$?
if [[ $rc -eq 2 ]] && grep -q "invalid lens" <<<"$out"; then
	ok "T8b lens with embedded newline rejected"
else
	bad "T8b lens with embedded newline rejected" "rc=$rc out=$out"
fi

# --- T9: a payload whose line is literally "## Round 2" must not suppress the real Round 2
# heading written when round 2's actual turn arrives (has_heading
# scanned every line of transcript.md, including fenced payload content) ---
WS6="$TMPD/ws6"
"$SCRIBE" open "$WS6" >/dev/null
printf 'judge notes mention\n## Round 2\nas a topic, verbatim, inside their round 1 turn\n' >"$TMPD/spoof.md"
"$SCRIBE" append "$WS6" 1 1 lensx "$TMPD/spoof.md" >/dev/null
echo "real round 2 turn" >"$TMPD/real2.md"
"$SCRIBE" append "$WS6" 2 1 lensx "$TMPD/real2.md" >/dev/null

heading_count=$(grep -c '^## Round 2$' "$WS6/transcript.md")
out=$("$SCRIBE" verify "$WS6" 2>&1)
rc=$?
if [[ $heading_count -ge 2 && $rc -eq 0 ]]; then
	ok "T9 payload's literal '## Round 2' line doesn't suppress the real heading (verify clean)"
else
	bad "T9 payload's literal '## Round 2' line doesn't suppress the real heading" \
		"heading_count=$heading_count rc=$rc out=$out"
fi

# --- T10: a tampered marker head (option injection / non-sha) is refused, not passed to git ---
WS10="$TMPD/ws10"
"$SCRIBE" open "$WS10" >/dev/null
printf 'head=--output=%s/injected\nopened=x\n' "$TMPD" >"$WS10/.tribunal-active"
echo "t" >"$TMPD/t10.md"
out=$("$SCRIBE" append "$WS10" 1 1 lensx "$TMPD/t10.md" 2>&1)
rc=$?
if [[ $rc -eq 3 && ! -e "$TMPD/injected" ]]; then
	ok "T10 non-sha marker head refused (rc 3), git never saw it as an option"
else
	bad "T10 non-sha marker head refused" "rc=$rc out=$out"
fi

# --- T11: verification requires a workspace and complete record evidence ---
echo ordinary >"$TMPD/not-a-workspace"
mkdir "$TMPD/empty-workspace"
for candidate in missing-workspace not-a-workspace empty-workspace; do
	out=$("$SCRIBE" verify "$TMPD/$candidate" 2>&1)
	rc=$?
	if [[ $rc -eq 3 ]] && ! grep -q Traceback <<<"$out"; then
		ok "T11 verify rejects $candidate"
	else
		bad "T11 verify rejects $candidate (rc=$rc out=$out)"
	fi
done

FRESH="$TMPD/fresh"
"$SCRIBE" open "$FRESH" >/dev/null
if "$SCRIBE" verify "$FRESH" >/dev/null; then
	ok "T12 freshly opened workspace verifies"
else
	bad "T12 freshly opened workspace verifies"
fi
"$SCRIBE" close "$FRESH" cap 0 >/dev/null
if "$SCRIBE" verify "$FRESH" >/dev/null; then
	ok "T13 closed workspace without turns verifies"
else
	bad "T13 closed workspace without turns verifies"
fi

COMPLETE="$TMPD/complete"
"$SCRIBE" open "$COMPLETE" >/dev/null
"$SCRIBE" append "$COMPLETE" 0 1 lensx "$TMPD/z.md" >/dev/null
"$SCRIBE" close "$COMPLETE" saturation 0 >/dev/null
if "$SCRIBE" verify "$COMPLETE" >/dev/null; then
	ok "T14 closed record verifies"
else
	bad "T14 closed record verifies"
fi
for evidence in transcript.md TRANSCRIPT.SHA256 turns turns/r0-s1.md; do
	cp -R "$COMPLETE" "$TMPD/incomplete"
	rm -rf "$TMPD/incomplete/$evidence"
	out=$("$SCRIBE" verify "$TMPD/incomplete" 2>&1)
	rc=$?
	if [[ $rc -eq 3 ]] && ! grep -q Traceback <<<"$out"; then
		ok "T15 missing $evidence rejected"
	else
		bad "T15 missing $evidence rejected (rc=$rc out=$out)"
	fi
	rm -rf "$TMPD/incomplete"
done
cp -R "$COMPLETE" "$TMPD/incomplete"
: >"$TMPD/incomplete/transcript.md"
sha256sum "$TMPD/incomplete/transcript.md" >"$TMPD/incomplete/TRANSCRIPT.SHA256"
rm "$TMPD/incomplete/turns/r0-s1.md"
out=$("$SCRIBE" verify "$TMPD/incomplete" 2>&1)
rc=$?
if [[ $rc -eq 3 ]]; then
	ok "T16 empty transcript and no turns rejected despite matching checksum"
else
	bad "T16 empty transcript and no turns rejected (rc=$rc out=$out)"
fi

echo
if python3 -B "$SELF_DIR/test_chain.py"; then
	ok "chain/head regression suite"
else
	bad "chain/head regression suite"
fi
echo "tribunal-scribe: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
