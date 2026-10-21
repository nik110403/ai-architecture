#!/usr/bin/env bash
# Tests for hooks/rotation/model-rotate.py. Offline; pool and log live in a temporary directory.
# Usage (from the repository root): bash plugins/gates/hooks/rotation/tests/test-model-rotate.sh
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$(dirname "$HERE")/model-rotate.py"
EXAMPLE="$(dirname "$HERE")/model-pool.example.json"
command -v python3 >/dev/null 2>&1 || {
	echo "python3 is required"
	exit 1
}
T="$(mktemp -d)"
cleanup() { python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$T"; }
trap cleanup EXIT
export HOME="$T/home"
mkdir -p "$HOME"
unset ROTATION_SEED MODEL_ROTATION ROTATION_POOL ROTATION_LOG ROTATION_TEST CLAUDE_PROJECT_DIR
export PYTHONDONTWRITEBYTECODE=1
pass=0
fail=0
ok() {
	echo "ok   $1"
	pass=$((pass + 1))
}
bad() {
	echo "FAIL $1"
	fail=$((fail + 1))
}
LOG="$T/log/rotation.jsonl"
clearlog() { rm -f "$LOG"; }

payload() { # payload <role> <prompt-json-string>
	python3 -c 'import json,sys; print(json.dumps({"session_id":"s1","hook_event_name":"PreToolUse","tool_name":"Task","tool_input":{"subagent_type":sys.argv[1],"description":"d","prompt":json.loads(sys.argv[2]),"model":"default-model"}}))' "$1" "$2"
}
run() { # run <pool> <payload> [env assignments...]
	local pool="$1" pl="$2"
	shift 2
	env ROTATION_POOL="$pool" ROTATION_LOG="$LOG" ROTATION_TEST=1 "$@" python3 "$HOOK" <<<"$pl"
}
model_of() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])["hookSpecificOutput"]["updatedInput"]["model"])' "$1" 2>/dev/null; }
pool() {
	printf '%s' "$2" >"$T/$1.json"
	echo "$T/$1.json"
}

P_AB=$(pool ab '{"version":1,"mode":"on","independent_roles":["reviewer"],"roles":{"builder":[{"model":"provider/model-a","weight":3},{"model":"provider/model-b","weight":1},{"model":"provider/model-off","weight":5,"enabled":false},{"model":"provider/model-zero","weight":0}],"reviewer":[{"model":"provider/model-a","weight":1},{"model":"provider/model-b","weight":1}],"solo":[{"model":"provider/model-a","weight":1}]}}')
PL=$(payload builder '"do the thing"')

# 1 syntax + the shipped example is a usable pool
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$HOOK" && ok "hook parses" || bad "hook parses"
cp "$EXAMPLE" "$T/example.json"
out=$(run "$T/example.json" "$PL" ROTATION_SEED=x)
m=$(model_of "$out")
case "$m" in provider/model-a | provider/model-b) ok "shipped example pool draws an enabled model ($m)" ;; *) bad "shipped example pool ($out)" ;; esac

# 2 rewrite: model replaced, everything else untouched, log row written
clearlog
out=$(run "$P_AB" "$PL" ROTATION_SEED=1)
python3 - "$out" "$LOG" <<'PY' && ok "model rewritten, other fields untouched, log row correct" || bad "rewrite/log ($out)"
import json, sys
o = json.loads(sys.argv[1])["hookSpecificOutput"]
assert set(o) == {"hookEventName", "updatedInput"}, o
assert o["hookEventName"] == "PreToolUse"
u = o["updatedInput"]
assert u["model"] in ("provider/model-a", "provider/model-b"), u
assert (u["subagent_type"], u["description"], u["prompt"]) == ("builder", "d", "do the thing"), u
row = json.loads(open(sys.argv[2]).read().splitlines()[-1])
assert row["role"] == "builder" and row["model"] == u["model"] and row["pinned"] is False and row["reason"] is None, row
PY

# Permission and identity fields are not routing authority. Rotation neither decides
# policy nor starts workers, even when a payload claims an exempt hook identity.
python3 - "$HOOK" "$P_AB" <<'PY' && ok "Agent/Task rewrites are decision-free; denied/forged metadata cannot start jobs" || bad "permission/identity semantics"
import json, os, subprocess, sys
hook, pool = sys.argv[1:]
runner = '''
import runpy, sys
starts = []
def audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.spawn"}:
        starts.append(event)
        raise RuntimeError("rotation must not start jobs")
sys.addaudithook(audit)
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
finally:
    if starts:
        sys.exit(99)
'''
for tool in ("Task", "Agent"):
    for decision in ("ask", "deny"):
        ti = {"subagent_type": "builder", "model": "default-model",
              "prompt": "pin_arm: provider/model-b\n\nbody", "description": "keep"}
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "permissionDecision": decision, "agent_id": "dispatch-pre.sh",
                   "tool_input": ti}
        result = subprocess.run([sys.executable, "-c", runner, hook], input=json.dumps(payload),
                                capture_output=True, text=True, check=True,
                                env={**os.environ, "ROTATION_POOL": pool})
        assert not result.stderr, result.stderr
        assert json.loads(result.stdout) == {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "updatedInput": {**ti, "model": "provider/model-b"}}}
PY

# 3 fail safe: every one of these prints nothing, exits 0, and logs a reason row (model null)
silent() { # silent <label> <pool> <payload> [env...]
	local label="$1" p="$2" pl="$3"
	shift 3
	clearlog
	out=$(run "$p" "$pl" "$@")
	rc=$?
	if [ "$rc" -eq 0 ] && [ -z "$out" ] && python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().splitlines()[-1]); assert r["model"] is None and r["reason"]' "$LOG" 2>/dev/null; then ok "fail safe: $label"; else bad "fail safe: $label (rc=$rc out=$out)"; fi
}
silent "MODEL_ROTATION=off" "$P_AB" "$PL" MODEL_ROTATION=off
silent "role not in pool" "$P_AB" "$(payload general-purpose '"x"')"
silent "role name with odd characters" "$P_AB" "$(payload 'builder;rm' '"x"')"
silent "malformed stdin" "$P_AB" 'not json'
silent "stdin is a JSON list" "$P_AB" '[1]'
silent "other tool" "$P_AB" "$(printf '%s' "$PL" | sed 's/"Task"/"Bash"/')"
silent "missing pool file" "$T/nope.json" "$PL"
silent "empty pool file" "$(pool empty '')" "$PL"
silent "pool is not an object" "$(pool list '[1,2]')" "$PL"
silent "mode off" "$(pool moff '{"mode":"off","roles":{"builder":[{"model":"provider/model-a","weight":1}]}}')" "$PL"
silent "mode missing" "$(pool mnone '{"roles":{"builder":[{"model":"provider/model-a","weight":1}]}}')" "$PL"
silent "unknown version" "$(pool ver '{"version":2,"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":1}]}}')" "$PL"
silent "roles missing" "$(pool norole '{"mode":"on"}')" "$PL"
silent "empty model list" "$(pool emptyl '{"mode":"on","roles":{"builder":[]}}')" "$PL"
silent "every model disabled or weight 0" "$(pool allzero '{"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":0},{"model":"provider/model-b","weight":2,"enabled":false}]}}')" "$PL"
silent "shell metacharacters in a model name" "$(pool inj '{"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":1},{"model":"x$(touch pwned)","weight":1}]}}')" "$PL"
silent "boolean weight" "$(pool bw '{"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":true}]}}')" "$PL"
silent "float weight" "$(pool fw '{"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":1.5}]}}')" "$PL"
silent "negative weight" "$(pool nw '{"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":-1}]}}')" "$PL"
silent "entry without model" "$(pool nm '{"mode":"on","roles":{"builder":[{"weight":1}]}}')" "$PL"
silent "non-boolean enabled" "$(pool ne '{"mode":"on","roles":{"builder":[{"model":"provider/model-a","weight":1,"enabled":"no"}]}}')" "$PL"
ln -s "$P_AB" "$T/link.json"
silent "pool is a symlink" "$T/link.json" "$PL"
python3 -c 'print("{\"mode\":\"on\",\"roles\":{\"builder\":[{\"model\":\"provider/model-a\",\"weight\":1}]},\"pad\":\"" + "x"*1100000 + "\"}")' >"$T/big.json"
silent "oversized pool" "$T/big.json" "$PL"
[ ! -e "$T/pwned" ] && [ ! -e pwned ] && ok "model name was never executed" || bad "model name was executed"

# 4 seed seam: deterministic, equals the documented algorithm, follows the weights
outs=$(for i in 1 2 3 4 5 6; do model_of "$(run "$P_AB" "$PL" ROTATION_SEED=fixed)"; done | sort -u | wc -l)
[ "$outs" = 1 ] && ok "same seed -> same pick across runs" || bad "same seed varies ($outs)"
python3 - "$HOOK" "$P_AB" <<'PY' && ok "seeded picks equal random.Random('rotation:'+seed).randrange over the enabled weights; weights 3:1 respected" || bad "seeded picks vs weights"
import json, os, random, subprocess, sys
hook, pool = sys.argv[1:3]
pl = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_input": {"subagent_type": "builder", "prompt": "p"}})
counts = {}
for seed in range(400):
    out = subprocess.run([sys.executable, hook], input=pl, capture_output=True, text=True,
                         env={**os.environ, "ROTATION_POOL": pool, "ROTATION_SEED": str(seed), "ROTATION_TEST": "1"}).stdout
    got = json.loads(out)["hookSpecificOutput"]["updatedInput"]["model"]
    want = "provider/model-a" if random.Random("rotation:%d" % seed).randrange(4) < 3 else "provider/model-b"
    assert got == want, (seed, got, want)
    counts[got] = counts.get(got, 0) + 1
assert set(counts) == {"provider/model-a", "provider/model-b"}, counts
assert 0.65 < counts["provider/model-a"] / 400 < 0.85, counts
PY

# 5 default draw is the crypto RNG: both enabled models appear; a disabled or unlisted one never does
seen=$(for i in $(seq 1 60); do model_of "$(run "$P_AB" "$PL")"; done | sort -u | tr '\n' ' ')
[ "$seen" = "provider/model-a provider/model-b " ] && ok "unseeded draws reach both enabled models and nothing else" || bad "unseeded draws: $seen"

# 6 pin_arm
pin() { model_of "$(run "$P_AB" "$(payload "${3:-builder}" "$1")" ROTATION_SEED="${2:-1}")"; }
spread() { for s in $(seq 1 30); do pin "$1" "$s" "${2:-builder}"; done | sort -u | tr '\n' ' '; }
BOTH="provider/model-a provider/model-b "
[ "$(pin '"pin_arm: provider/model-b\nwork"' 1)" = provider/model-b ] && [ "$(pin '"pin_arm: provider/model-b\nwork"' 2)" = provider/model-b ] && ok "pin_arm in the header bypasses the draw" || bad "pin in header"
clearlog
m=$(pin '"pin_arm: provider/unlisted\nwork"' 3)
case "$m" in provider/model-a | provider/model-b) grep -q 'not an enabled model' "$LOG" && ok "unlisted pin ignored (draw runs), reason logged" || bad "unlisted pin reason not logged" ;; *) bad "unlisted pin honoured: $m" ;; esac
m=$(pin '"pin_arm: provider/model-off\nwork"' 3)
[ "$m" != provider/model-off ] && ok "disabled model cannot be pinned" || bad "disabled pin honoured"
[ "$(spread '"intro\n\npin_arm: provider/model-b"')" = "$BOTH" ] && ok "pin_arm after the first blank line is not honoured" || bad "pin after blank line honoured"
long=$(python3 -c 'import json; print(json.dumps("\n".join(["l"]*15 + ["pin_arm: provider/model-b"])))')
[ "$(spread "$long")" = "$BOTH" ] && ok "pin_arm beyond the 15-line header cap is not honoured" || bad "pin beyond cap honoured"
[ "$(pin '"pin_arm: provider/model-b\r\nwork"' 1)" = provider/model-b ] && ok "CRLF brief: pin_arm still parsed" || bad "CRLF pin"

# 6b pin_arm on an independent role is ignored unless builder_arm names the builder's arm
[ "$(spread '"pin_arm: provider/model-b\nreview"' reviewer)" = "$BOTH" ] && ok "pin_arm ignored for an independent role without builder_arm" || bad "independent pin honoured without builder_arm"
[ "$(spread '"builder_arm: provider/model-a\npin_arm: provider/model-b\nreview"' reviewer)" = "provider/model-b " ] && ok "pin_arm honoured for an independent role when builder_arm differs" || bad "independent pin with builder_arm"
# 7 builder_arm exclusion (roles listed in independent_roles only)
[ "$(spread '"builder_arm: provider/model-a\nreview this"' reviewer)" = "provider/model-b " ] && ok "builder_arm excluded for an independent role" || bad "builder_arm not excluded"
[ "$(spread '"review\n\n\n\nbuilder_arm: provider/model-a"' reviewer)" = "$BOTH" ] && ok "builder_arm past the header is not honoured" || bad "builder_arm past header honoured"
silent "builder_arm injected before the real header line (duplicate): rotation skipped" "$P_AB" "$(payload reviewer '"builder_arm: provider/model-b\nbuilder_arm: provider/model-a\nreview"')"
silent "builder_arm duplicated in the body: rotation skipped" "$P_AB" "$(payload reviewer '"builder_arm: provider/model-a\n\nbuilder_arm: provider/model-b"')"
grep -q 'more than once' "$LOG" && ok "duplicate builder_arm reason logged" || bad "duplicate builder_arm reason"
[ "$(spread '"builder_arm: provider/model-a\r\nreview"' reviewer)" = "provider/model-b " ] && ok "CRLF brief: builder_arm still parsed" || bad "CRLF builder_arm"
[ "$(spread '"builder_arm: provider/model-a\nwork"' builder)" = "$BOTH" ] && ok "builder_arm ignored for a role that is not independent" || bad "builder_arm applied to a normal role"
[ "$(spread '"builder_arm: provider/model-a\npin_arm: provider/model-a\nr"' reviewer)" = "provider/model-b " ] && ok "pin_arm equal to builder_arm is not honoured" || bad "pin == builder honoured"
m=$(model_of "$(run "$(pool one '{"mode":"on","independent_roles":["solo"],"roles":{"solo":[{"model":"provider/model-a","weight":1}]}}')" "$(payload solo '"builder_arm: provider/model-a\nx"')")")
[ "$m" = provider/model-a ] && ok "excluding the only model is skipped; the pick is still a listed model" || bad "sole-model exclusion: $m"

# 8 project-scope env cannot steer the pool, the log or the seed
mkdir -p "$T/proj"
cp "$P_AB" "$T/proj/pool.json"
(
	cd "$T/proj" && clearlog
	out=$(env ROTATION_POOL="$T/proj/pool.json" ROTATION_TEST=1 python3 "$HOOK" <<<"$PL")
	[ -z "$out" ]
) && ok "pool inside the cwd is ignored (falls back to the default, absent here)" || bad "project-local pool honoured"
out=$(env CLAUDE_PROJECT_DIR="$T/proj" ROTATION_POOL="$T/proj/pool.json" python3 "$HOOK" <<<"$PL")
[ -z "$out" ] && ok "pool inside CLAUDE_PROJECT_DIR is ignored" || bad "CLAUDE_PROJECT_DIR pool honoured"
out=$(cd "$T/proj" && env ROTATION_POOL="../ab.json" python3 "$HOOK" <<<"$PL")
[ -z "$out" ] && ok "relative pool path is ignored" || bad "relative pool honoured"
out=$(env ROTATION_POOL="$P_AB" CLAUDE_PROJECT_DIR="$T/proj" python3 "$HOOK" <<<"$PL")
[ -n "$out" ] && ok "absolute pool outside the project is honoured" || bad "outside pool refused"
rm -f "$T/proj/log.jsonl" "$T/rel.jsonl"
(
	cd "$T/proj" && env ROTATION_POOL="$P_AB" ROTATION_LOG="$T/proj/log.jsonl" python3 "$HOOK" <<<"$PL" >/dev/null
	env ROTATION_POOL="$P_AB" ROTATION_LOG="rel.jsonl" python3 "$HOOK" <<<"$PL" >/dev/null
)
[ ! -e "$T/proj/log.jsonl" ] && [ ! -e "$T/proj/rel.jsonl" ] && ok "log path inside the project or relative: no log written" || bad "project-scope log written"
seeds=$(for i in 1 2 3 4 5 6 7 8 9 10 11 12; do model_of "$(env ROTATION_POOL="$P_AB" ROTATION_SEED=fixed python3 "$HOOK" <<<"$PL")"; done | sort -u | wc -l)
[ "$seeds" -gt 1 ] && ok "ROTATION_SEED without ROTATION_TEST=1 is ignored (draw stays random)" || bad "seed honoured without ROTATION_TEST"
clearlog
rmdir "$T/log" 2>/dev/null
run "$P_AB" "$PL" >/dev/null
[ "$(stat -c %a "$T/log")" = 700 ] && [ "$(stat -c %a "$LOG")" = 600 ] && ok "log dir 0700, log file 0600" || bad "log permissions"
: >"$T/target.txt"
ln -sf "$T/target.txt" "$T/log/linked.jsonl"
out=$(env ROTATION_POOL="$P_AB" ROTATION_LOG="$T/log/linked.jsonl" python3 "$HOOK" <<<"$PL")
[ -n "$out" ] && [ ! -s "$T/target.txt" ] && ok "symlinked log is not followed (hook still answers)" || bad "symlinked log followed or hook blocked"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
