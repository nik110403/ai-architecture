#!/usr/bin/env python3
"""Byte-identity goldens for review-stop-gate.py with NO review_receipts_dir configured.

fixtures/review-golden.json was captured from the BASE hook (before the receipt feature was added);
this test replays every transcript fixture and compares the hook's raw stdout byte for byte.
`--capture` rewrites the golden (only ever run against an unmodified gate)."""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "review-stop-gate.py")
FIX = os.path.join(HERE, "fixtures")
GOLDEN = os.path.join(FIX, "review-golden.json")
TMP = tempfile.mkdtemp()
CFG = os.path.join(TMP, "gates.json")
with open(os.path.join(FIX, "test-config.json")) as fh, open(CFG, "w") as out:
    out.write(fh.read())
STATE = os.path.join(TMP, "state")
os.makedirs(STATE)


def run(transcript, active):
    payload = {"cwd": TMP, "session_id": "golden", "transcript_path": transcript}
    if active:
        payload["stop_hook_active"] = True
    env = dict(os.environ, CLAUDE_HOOK_STATE_DIR=STATE, AIARCH_CONFIG=CFG)
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True, text=True, env=env, check=True)
    return r.stdout


def collect():
    got = {}
    for name in sorted(os.listdir(os.path.join(FIX, "transcripts"))):
        if not name.startswith("review-"):
            continue
        text = open(os.path.join(FIX, "transcripts", name)).read().replace("@CWD@", TMP)
        path = os.path.join(TMP, name)
        open(path, "w").write(text)
        got[name] = {"stop": run(path, False), "stop_hook_active": run(path, True)}
    got["(missing transcript)"] = {"stop": run(os.path.join(TMP, "none.jsonl"), False)}
    return got


got = collect()
if "--capture" in sys.argv:
    json.dump(got, open(GOLDEN, "w"), indent=1, sort_keys=True)
    print(f"captured {len(got)} goldens")
    sys.exit(0)
want = json.load(open(GOLDEN))
bad = [k for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]
for k in bad:
    print(f"FAIL - review golden differs: {k}: want {want.get(k)!r} got {got.get(k)!r}")
print(f"review byte-identity goldens: {len(got) - len(bad)} identical, {len(bad)} differ")
sys.exit(1 if bad else 0)
