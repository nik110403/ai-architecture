#!/usr/bin/env python3
"""review-stop-gate with `review_receipts_dir` set: receipts only NARROW the gate.

Stop passes a security-sensitive edit only when the transcript proves the review role ran AND a fresh PASS
receipt from that exact role covers the current sha256 of every edited security file. Receipts are issued
with the real modules/review/receipt.py CLI. Without the key, byte-identity is covered by run_review_golden.py."""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "review-stop-gate.py")
RECEIPT = os.path.join(HERE, "..", "..", "..", "modules", "review", "receipt.py")
TMP = tempfile.mkdtemp()
STATE = os.path.join(TMP, "state")
os.makedirs(STATE)
passed = failed = 0


def check(name, got, want, why=""):
    global passed, failed
    if want == "block":
        ok = got[0] == "block" and (not why or why in got[1])
    else:
        ok = got[0] == want
    if ok:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}: want {want} {why!r}, got {got}")


def case_dir(n):
    d = os.path.join(TMP, n)
    os.makedirs(os.path.join(d, "receipts"))
    return d


def transcript(d, files, reviewer=True, subagent="ai-architecture:security-reviewer"):
    lines = []
    for i, f in enumerate(files):
        lines.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"w{i}", "name": "Write", "input": {"file_path": f, "content": "def login(password): pass"}}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"w{i}", "content": "ok", "is_error": False}]}})
    if reviewer:
        lines.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Task", "input": {"subagent_type": subagent, "prompt": "review"}}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}]}})
    p = os.path.join(d, "t.jsonl")
    open(p, "w").write("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


def issue(d, name, files, verdict="PASS", reviewer="security-reviewer"):
    cmd = [sys.executable, "-B", RECEIPT, "issue", "--reviewer", reviewer, "--verdict", verdict, "--out", os.path.join(d, "receipts", name)]
    for f in files:
        cmd += ["--artifact", f]
    subprocess.run(cmd, check=True, capture_output=True)


def stop(d, tpath, rdir="DEFAULT", extra=None):
    cfg = {} if rdir is None else {"review_receipts_dir": os.path.join(d, "receipts") if rdir == "DEFAULT" else rdir}
    cfg.update(extra or {})
    cpath = os.path.join(d, "gates.json")
    json.dump(cfg, open(cpath, "w"))
    env = dict(os.environ, CLAUDE_HOOK_STATE_DIR=STATE, AIARCH_CONFIG=cpath)
    r = subprocess.run([sys.executable, HOOK], input=json.dumps({"cwd": d, "session_id": "s", "transcript_path": tpath}), capture_output=True, text=True, env=env, check=True)
    out = r.stdout.strip()
    if not out:
        return ("allow", "")
    j = json.loads(out)
    return (j.get("decision", "?"), j.get("reason", "") + " " + j.get("systemMessage", ""))


def sec_file(d, name="login.py", text="def login(password): pass\n"):
    f = os.path.join(d, name)
    open(f, "w").write(text)
    return os.path.realpath(f)


d = case_dir("base")
f = sec_file(d)
t = transcript(d, [f])
check("no receipt at all -> block", stop(d, t), "block", f)
issue(d, "ok.json", [f])
check("fresh PASS receipt from the configured role + reviewer ran -> allow", stop(d, t), "allow")
link = os.path.join(d, "alias.py")
os.symlink(f, link)
check("file_path through a symlink compares by realpath -> allow", stop(d, transcript(d, [link])), "allow")
t = transcript(d, [f])
open(f, "a").write("# edited again\n")
check("stale receipt (file edited after review) -> block naming the file", stop(d, t), "block", f)
check("stale receipt reason says stale", stop(d, t), "block", "stale")

d = case_dir("fail")
f = sec_file(d)
t = transcript(d, [f])
issue(d, "f.json", [f], verdict="FAIL")
check("FAIL receipt alone -> block", stop(d, t), "block", f)
issue(d, "p.json", [f])
check("(c) FAIL receipt naming the current hash beside a PASS -> block", stop(d, t), "block", "FAIL")

d = case_dir("norole")
f = sec_file(d)
issue(d, "p.json", [f])
check("(a) fresh PASS receipt but no reviewer Task in the transcript -> block", stop(d, transcript(d, [f], reviewer=False)), "block", "never ran")

d = case_dir("wrong")
f = sec_file(d)
issue(d, "p.json", [f], reviewer="design-critic")
check("(b) receipt from the wrong role -> block", stop(d, transcript(d, [f])), "block", f)

d = case_dir("partial")
f, g = sec_file(d), sec_file(d, "token.py", "token = 1\n")
issue(d, "p.json", [f])
check("two security files, receipt covers only one -> block naming the other", stop(d, transcript(d, [f, g])), "block", g)
issue(d, "q.json", [g])
check("each covered by a fresh PASS (separate receipts) -> allow", stop(d, transcript(d, [f, g])), "allow")

d = case_dir("badcfg")
f = sec_file(d)
t = transcript(d, [f])
issue(d, "p.json", [f])
missing = os.path.join(d, "no-such-dir")
check("(d) key set, directory missing -> block naming it", stop(d, t, rdir=missing), "block", missing)
check("(d) key set, relative path -> block", stop(d, t, rdir="receipts"), "block", "absolute")
notdir = os.path.join(d, "afile")
open(notdir, "w").write("x")
check("(d) key set, not a directory -> block naming it", stop(d, t, rdir=notdir), "block", notdir)
linkdir = os.path.join(d, "linkdir")
os.symlink(os.path.join(d, "receipts"), linkdir)
check("(d) key set, symlinked directory -> block", stop(d, t, rdir=linkdir), "block", linkdir)

d = case_dir("junk")
f = sec_file(d)
t = transcript(d, [f])
issue(d, "p.json", [f])
open(os.path.join(d, "receipts", "junk.json"), "w").write("{not json")
check("unreadable receipt in the directory -> block naming it", stop(d, t), "block", "junk.json")
os.unlink(os.path.join(d, "receipts", "junk.json"))
open(os.path.join(d, "receipts", "dup.json"), "w").write(open(os.path.join(d, "receipts", "p.json")).read().replace('"verdict": "PASS"', '"verdict": "PASS", "verdict": "FAIL"'))
check("duplicate-key receipt -> block", stop(d, t), "block", "dup.json")
os.unlink(os.path.join(d, "receipts", "dup.json"))
check("clean directory again -> allow", stop(d, t), "allow")

d = case_dir("bounds")
f = sec_file(d)
t = transcript(d, [f])
issue(d, "p.json", [f])
src = os.path.join(d, "receipts", "p.json")
for i in range(210):
    shutil.copy(src, os.path.join(d, "receipts", f"c{i}.json"))
check("more receipts than the bound -> block", stop(d, t), "block", "too many")
d = case_dir("big")
f = sec_file(d, "big.py", "password = 1\n" + "#" * (17 << 20))
t = transcript(d, [f])
issue(d, "p.json", [f])
check("artifact over the byte bound -> block", stop(d, t), "block", "too large")

d = case_dir("nonsec")
f = sec_file(d, "page.md", "docs")
open(os.path.join(d, "x"), "w")
t = transcript(d, [f], reviewer=False)
check("non-security session with the key set -> allow (no new requirement)", stop(d, t), "allow")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nreview receipts gate: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
