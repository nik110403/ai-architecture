#!/usr/bin/env bash
# plancheck tests — fixture tests for plancheck checks 12 (open-Qn soft cap +
# Assumption-lines aggregate), 13 (item field coverage), 14 (unjustified gate) and 15 (Resume
# Next waits on the owner without a gate), plus the exit-status contract and the shipped
# example packs. Builds isolated fixture packs under mktemp -d and scopes plancheck to them
# with `--dir`. PLANCHECK_DEFAULT_ROOT is also exported and exercised once (T10), per the
# tool's own documented override.
# Exits 0 if all checks pass, 1 otherwise.
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_DIR="$(cd "$SELF_DIR/.." && pwd)"
BIN="$TOOL_DIR/plancheck"

pass=0
fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

TMPD="$(mktemp -d)"
cleanup() { rm -rf "$TMPD"; }
trap cleanup EXIT

# --- T1/T2: shell sanity ---------------------------------------------------------------------
if bash -n "$BIN"; then ok "T1 bash -n plancheck clean"; else bad "T1 bash -n plancheck clean"; fi
if bash -n "$SELF_DIR/run.sh"; then ok "T2 bash -n self clean"; else bad "T2 bash -n self clean"; fi

export PLANCHECK_DEFAULT_ROOT="$TMPD/root"
ROOT="$PLANCHECK_DEFAULT_ROOT"

minimal_tracker() {  # dir label
  cat > "$1/TRACKER.md" <<EOF
# $2 — Tracker

**Status: active**

**Goal:** fixture pack, exit 0 is the goal.

## Resume here
**Next:** nothing, this is a fixture.

Last verified: 2026-09-07
EOF
}

# --- Fixture: 6 open Qn (over the soft cap) ---------------------------------------------------
mkdir -p "$ROOT/proj/plans/pack-open6"
minimal_tracker "$ROOT/proj/plans/pack-open6" "pack-open6"
cat > "$ROOT/proj/plans/pack-open6/SPEC.md" <<'EOF'
### S1 — fixture item
- What: fixture
- Acceptance: fixture
- Verify: `true`
EOF
cat > "$ROOT/proj/plans/pack-open6/QUESTIONS.md" <<'EOF'
## Q1 · a
Context.
- [ ] A

## Q2 · b
Context.
- [ ] A

## Q3 · c
Context.
- [ ] A

## Q4 · d
Context.
- [ ] A

## Q5 · e
Context.
- [ ] A

## Q6 · f
Context.
- [ ] A

## Parked
- an idea
EOF

# --- Fixture: 5 open Qn (at the soft cap, no flag) --------------------------------------------
mkdir -p "$ROOT/proj/plans/pack-open5"
minimal_tracker "$ROOT/proj/plans/pack-open5" "pack-open5"
cat > "$ROOT/proj/plans/pack-open5/SPEC.md" <<'EOF'
### S1 — fixture item
- What: fixture
- Acceptance: fixture
- Verify: `true`
EOF
cat > "$ROOT/proj/plans/pack-open5/QUESTIONS.md" <<'EOF'
## Q1 · a
Context.
- [ ] A

## Q2 · b
Context.
- [ ] A

## Q3 · c
Context.
- [ ] A

## Q4 · d
Context.
- [ ] A

## Q5 · e
Context.
- [ ] A
EOF

# --- Fixture: 2 Assumption lines ---------------------------------------------------------------
mkdir -p "$ROOT/proj/plans/pack-assump"
minimal_tracker "$ROOT/proj/plans/pack-assump" "pack-assump"
cat > "$ROOT/proj/plans/pack-assump/SPEC.md" <<'EOF'
### S1 — first
- What: fixture
- Acceptance: fixture
- Verify: `true`
- Assumption: default A — basis: measured; reversible by flipping a flag; wrong if X happens

### S2 — second
- What: fixture
- Acceptance: fixture
- Verify: `true`
- Assumption: default B — basis: inferred; reversible by a follow-up item; wrong if Y happens
EOF

# --- Fixture: item field coverage (one full item, one missing Verify+Touches) ------------------
mkdir -p "$ROOT/proj/plans/pack-fields"
minimal_tracker "$ROOT/proj/plans/pack-fields" "pack-fields"
cat > "$ROOT/proj/plans/pack-fields/SPEC.md" <<'EOF'
### S1 — full item
- What: does the thing
- Acceptance: it works
- Verify: `echo ok`
- Needs: none
- Touches: some/path

### S2 — missing item
- What: does another thing
- Acceptance: it works too
EOF

OUT="$("$BIN" --dir "$ROOT/proj/plans")"

# --- T3/T4: open-Qn soft cap (check 12) ---------------------------------------------------------
if grep -q 'pack-open6 — 6 open questions (soft cap 5)' <<<"$OUT"; then
  ok "T3 6 open Qn -> ℹ️ present"
else
  bad "T3 6 open Qn -> ℹ️ present"
fi
if grep -q 'pack-open5 —.*open questions' <<<"$OUT"; then
  bad "T4 5 open Qn -> no ℹ️ (soft cap not exceeded)"
else
  ok "T4 5 open Qn -> no ℹ️ (soft cap not exceeded)"
fi

# --- T5: Assumption-lines aggregate (check 12) --------------------------------------------------
if grep -qE '^Assumption lines: 2 across [0-9]+ packs$' <<<"$OUT"; then
  ok "T5 2 Assumption lines -> aggregate says 2"
else
  bad "T5 2 Assumption lines -> aggregate says 2"
fi

# --- T6/T7/T8: item field coverage (check 13) ---------------------------------------------------
if grep -q 'pack-fields: S2 missing: Verify, Touches' <<<"$OUT"; then
  ok "T6 item missing Verify+Touches -> ℹ️ names both"
else
  bad "T6 item missing Verify+Touches -> ℹ️ names both"
fi
if grep -q 'pack-fields: S1 missing' <<<"$OUT"; then
  bad "T7 full item -> no ℹ️"
else
  ok "T7 full item -> no ℹ️"
fi
if grep -q 'pack-fields — fields: Acc 2/2 · Ver 1/2 · Needs 1/2 · Touches 1/2' <<<"$OUT"; then
  ok "T8 coverage line matches expected counts"
else
  bad "T8 coverage line matches expected counts"
fi

# --- T9: exit status: 0 with no blocking finding, 1 with one, --report-only always 0 -------------
"$BIN" --dir "$ROOT" >/dev/null
rc=$?
if [ "$rc" -eq 0 ]; then ok "T9a plancheck exits 0 (no blocking finding)"; else bad "T9a plancheck exits 0 (no blocking finding); got rc=$rc"; fi
mkdir -p "$TMPD/blocked/pack-blocked"
minimal_tracker "$TMPD/blocked/pack-blocked" "pack-blocked"
printf '### S1 — x\n- Needs: S9\n' > "$TMPD/blocked/pack-blocked/SPEC.md"
"$BIN" --dir "$TMPD/blocked" >/dev/null; rc=$?
if [ "$rc" -eq 1 ]; then ok "T9b dangling Needs -> exit 1"; else bad "T9b dangling Needs -> exit 1; got rc=$rc"; fi
"$BIN" --dir "$TMPD/blocked" --report-only >/dev/null; rc=$?
if [ "$rc" -eq 0 ]; then ok "T9c --report-only -> exit 0 despite blocking finding"; else bad "T9c --report-only -> exit 0; got rc=$rc"; fi

# --- T10: PLANCHECK_DEFAULT_ROOT override reaches the default sweep too --------------------------
DEFAULT_OUT="$("$BIN")"
if grep -q 'pack-open6 — 6 open questions (soft cap 5)' <<<"$DEFAULT_OUT"; then
  ok "T10 PLANCHECK_DEFAULT_ROOT override reaches default (no --dir) sweep"
else
  bad "T10 PLANCHECK_DEFAULT_ROOT override reaches default (no --dir) sweep"
fi

# --- Fixtures: gates (check 14) + Resume Next waiting on the owner (check 15) ---------------------------
gate_tracker() {  # dir label next-line
  cat > "$1/TRACKER.md" <<EOF
# $2 — Tracker

**Status: active**

**Goal:** fixture pack, exit 0 is the goal.

## Resume here
$3

Last verified: 2026-09-16
EOF
}
GROOT="$TMPD/gates"
mkdir -p "$GROOT/pack-gated" "$GROOT/pack-ungated"
gate_tracker "$GROOT/pack-gated" "pack-gated" "**Next:** S2 on the owner's go."
gate_tracker "$GROOT/pack-ungated" "pack-ungated" '**Next action:** awaiting the owner before S1.'
cat > "$GROOT/pack-gated/SPEC.md" <<'EOF'
### S1 — auto item
- Acceptance: x
- Verify: `true`
- Touches: a/
- Gate: auto

### S2 — gated with valid reason
- Acceptance: x
- Verify: `true`
- Touches: b/
- Gate: human — Live Install into the shared config

### S3 — gated with bogus reason
- Acceptance: x
- Verify: `true`
- Touches: c/
- Gate: human — feels risky

### S4 — bad value
- Acceptance: x
- Verify: `true`
- Touches: d/
- Gate: maybe
EOF
cat > "$GROOT/pack-ungated/SPEC.md" <<'EOF'
### S1 — no gate field
- Acceptance: x
- Verify: `true`
- Touches: a/
EOF
GOUT="$("$BIN" --dir "$GROOT")"

if grep -qE 'pack-gated: S(1|2) Gate' <<<"$GOUT"; then
  bad "T11 Gate: auto / human with allowed reason -> no ℹ️"
else
  ok "T11 Gate: auto / human with allowed reason -> no ℹ️"
fi
if grep -q 'pack-gated: S3 Gate: human reason not in allowed list' <<<"$GOUT"; then
  ok "T12 Gate: human with bogus reason -> ℹ️"
else
  bad "T12 Gate: human with bogus reason -> ℹ️"
fi
if grep -q 'pack-gated: S4 Gate: invalid value' <<<"$GOUT"; then
  ok "T13 Gate: invalid value -> ℹ️"
else
  bad "T13 Gate: invalid value -> ℹ️"
fi
if grep -q 'pack-gated: Resume Next waits on the human owner' <<<"$GOUT"; then
  bad "T14 Next on the owner's go naming a Gate: human item -> no ℹ️"
else
  ok "T14 Next on the owner's go naming a Gate: human item -> no ℹ️"
fi
if grep -q 'pack-ungated: Resume Next waits on the human owner but no named Gate: human item' <<<"$GOUT"; then
  ok "T15 Next awaiting the owner without a Gate: human item -> ℹ️"
else
  bad "T15 Next awaiting the owner without a Gate: human item -> ℹ️"
fi

# --- T16/T17: the shipped example packs: clean pack exits 0, broken pack exits non-zero -----------
"$BIN" --dir "$TOOL_DIR/example" >/dev/null; rc=$?
if [ "$rc" -eq 0 ]; then ok "T16 example pack -> exit 0"; else bad "T16 example pack -> exit 0; got rc=$rc"; fi
BOUT="$("$BIN" --dir "$TOOL_DIR/example-broken")"; rc=$?
if [ "$rc" -ne 0 ] && grep -q '⛔' <<<"$BOUT"; then ok "T17 example-broken pack -> non-zero exit with ⛔ findings"; else bad "T17 example-broken pack -> non-zero; got rc=$rc"; fi

echo "----"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
