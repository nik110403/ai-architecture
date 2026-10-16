#!/usr/bin/env bash
# loop-sync tests. Fixture registries with fictional loops; a fake `systemctl` on PATH stands in
# for real units, so nothing touches the machine's real services.
#   T1      syntax
#   T2-T7   --strict semantics (ported): live row passes, documented-dead row fails,
#           default never fails, output identical with/without --strict, LOOPS_FILE env
#   T8-T14  registry-driven checks against the fake systemctl (timer, service, host scope,
#           hook, file, missing unit under --strict)
#   T15-T18 no systemd available: "not checkable", no crash, exit 0 even with --strict
#   T19-T21 usage errors
# Exit 0 if all pass, 1 otherwise.
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SELF_DIR/../loop-sync"
FIX="$SELF_DIR/fixtures"

TMPD="$(mktemp -d)"
cleanup() { rm -rf "$TMPD"; }
trap cleanup EXIT

pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }
check() { if [ "$1" = 0 ]; then ok "$2"; else bad "$2"; fi; }

# fake systemctl: demo-ok.* active, demo-idle.* installed-but-inactive, anything else absent
FAKEBIN="$TMPD/fake"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/systemctl" <<'EOF'
#!/usr/bin/env bash
[ "$1" = "--user" ] && shift
case "$1" in
  show-environment) exit 0 ;;
  show) case "$2" in
          demo-ok.*)   printf 'loaded\nactive\nenabled\n' ;;
          demo-idle.*) printf 'loaded\ninactive\ndisabled\n' ;;
          *)           printf 'not-found\ninactive\n\n' ;;
        esac ;;
  is-active) unit="${@: -1}"; [[ "$unit" == demo-ok.* ]] ;;
  cat) case "$2" in demo-ok.*|demo-idle.*) exit 0 ;; *) exit 1 ;; esac ;;
esac
EOF
chmod +x "$FAKEBIN/systemctl"

# PATH with the basic tools the script needs but NO systemctl
BARE="$TMPD/bare"; mkdir -p "$BARE"
for t in awk gawk mawk hostname grep basename dirname cat; do
  p="$(command -v "$t" 2>/dev/null)" && ln -sf "$p" "$BARE/$t"
done

run()   { PATH="$FAKEBIN:$PATH" "$SCRIPT" "$@"; }
runno() { PATH="$BARE" /bin/bash "$SCRIPT" "$@"; }

bash -n "$SCRIPT" && bash -n "$SELF_DIR/run.sh"; check $? "T1 bash -n clean"

# ---- strict semantics -----------------------------------------------------------------------
out_a="$(run --strict --registry "$FIX/loops-ok.md" 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; check $? "T2 live registry --strict -> exit 0 (rc=$rc)"
out_b_s="$(run --strict --registry "$FIX/loops-dead.md" 2>&1)"; rc=$?
[ "$rc" -eq 1 ]; check $? "T3 documented-dead row --strict -> exit 1 (rc=$rc)"
out_b_d="$(run --registry "$FIX/loops-dead.md" 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; check $? "T4 documented-dead row, default -> exit 0 (rc=$rc)"
[ "$out_b_s" = "$out_b_d" ]; check $? "T5 --strict output identical to default output"
rc_a=0; rc_b=0
LOOPS_FILE="$FIX/loops-ok.md" run --strict >/dev/null 2>&1 || rc_a=$?
LOOPS_FILE="$FIX/loops-dead.md" run --strict >/dev/null 2>&1 || rc_b=$?
[ "$rc_a" -eq 0 ] && [ "$rc_b" -eq 1 ]; check $? "T6 LOOPS_FILE env: live -> 0, dead -> 1"
rc=0; run --strict --file "$FIX/loops-dead.md" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 1 ]; check $? "T7 --file alias works"

# ---- registry-driven checks -----------------------------------------------------------------
W="$(run --registry "$FIX/loops-wiring.md" --host testhost --settings "$FIX/settings-ok.json" 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; check $? "T8 wiring registry default exit 0"
echo "$W" | grep -q '✅ demo-ok.timer — timer active'; check $? "T9 active timer reported live"
echo "$W" | grep -q '✅ demo-ok.service — service active'; check $? "T10 active service reported live"
echo "$W" | grep -q '⚠️  demo-idle.timer — unit exists but timer not active'; check $? "T11 inactive timer is a warning"
echo "$W" | grep -q 'scoped to host(s) other-host — skipped on testhost'; check $? "T12 host-scoped row skipped, not failed"
echo "$W" | grep -q '✅ demo-hook — referenced in settings file' && echo "$W" | grep -q '✅ loops-ok.md — exists'; check $? "T13 hook and file checks pass"
rc=0; run --strict --registry "$FIX/loops-wiring.md" --host testhost --settings "$FIX/settings-ok.json" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ]; check $? "T13b wiring registry --strict clean (warnings are not violations)"
M="$(run --strict --registry "$FIX/loops-missing.md" 2>&1)"; rc=$?
[ "$rc" -eq 1 ] && echo "$M" | grep -q '⛔ demo-missing.timer — NOT INSTALLED'; check $? "T14 missing unit --strict -> exit 1 (rc=$rc)"
echo "$W" | grep -q 'hook-row' && run --registry "$FIX/loops-wiring.md" --host testhost 2>&1 | grep -q 'demo-hook — not checkable (no --settings'; check $? "T14b hook check without --settings is 'not checkable'"
run --registry "$FIX/loops-wiring.md" --host testhost --settings "$TMPD/nonexistent.json" 2>&1 | grep -q 'settings file unreadable'; check $? "T14c unreadable settings file is 'not checkable'"

# ---- no systemd -----------------------------------------------------------------------------
N="$(runno --strict --registry "$FIX/loops-ok.md" 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; check $? "T15 no systemctl: --strict exit 0, no crash (rc=$rc)"
echo "$N" | grep -q 'not checkable'; check $? "T16 no systemctl: reports 'not checkable'"
N2="$(runno --strict --registry "$FIX/loops-wiring.md" --host testhost 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; check $? "T17 no systemctl: mixed registry exit 0 (rc=$rc)"
rc=0; runno --strict --registry "$FIX/loops-dead.md" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 1 ]; check $? "T18 no systemctl: documented-dead row still fails --strict"

# ---- usage errors ---------------------------------------------------------------------------
rc=0; run >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 2 ]; check $? "T19 missing --registry -> exit 2"
rc=0; run --registry "$TMPD/nope.md" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 2 ]; check $? "T20 unreadable registry -> exit 2"
rc=0; run --bogus >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 2 ]; check $? "T21 unknown flag -> exit 2"

# ---- shipped template parses ----------------------------------------------------------------
T="$(run --registry "$SELF_DIR/../LOOPS.template.md" --host testhost 2>&1)"; rc=$?
echo "$T" | grep -q 'nightly-report' && echo "$T" | grep -q 'build-server-sync' && echo "$T" | grep -q 'skipped on testhost'; check $? "T22 example template parses and host-scopes"
rc=0; run --strict --registry "$SELF_DIR/../LOOPS.template.md" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 1 ]; check $? "T23 template's dead example row trips --strict"

echo "----"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
