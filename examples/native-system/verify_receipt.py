#!/usr/bin/env python3
"""Check a native-system receipt against the evidence on disk. Exit 0 all checks hold, 1 otherwise.

Usage: verify_receipt.py [--receipt PATH] [--out DIR] [--expect-mode offline|fake|live]
Default receipt: the run named by `<out>/latest.json`, else `<out>/receipt.json`. A receipt is evidence against
mistakes, not against a same-user forger: that user can rewrite every file this script reads.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTINUITY = ROOT / "modules" / "continuity" / "continuity.py"
sys.path.insert(0, str(HERE))
import run  # noqa: E402  (answer_text and the marker; importing it runs nothing)

MODES = ("offline", "fake", "live")
REQUIRED = ("schema", "mode", "harness", "run_id", "sessions", "kill", "jobs", "resume", "ready",
            "answer", "answer_sha256", "checker_verdict", "accepted_failed", "calls")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def default_receipt(out):
    out = Path(out)
    latest = out / "latest.json"
    if latest.is_file():
        doc = json.loads(latest.read_text())
        sub = run.run_subdir(doc.get("run") if isinstance(doc, dict) else None)
        if sub is None:
            raise ValueError("latest.json names a run that is not 'runs/<id>'")
        return out / sub / "receipt.json"
    return out / "receipt.json"


def check(receipt_path, expect_mode):
    problems = []

    def need(ok, message):
        if not ok:
            problems.append(message)
        return ok

    rd = Path(receipt_path).parent
    try:
        r = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"receipt unreadable: {error.__class__.__name__}"]
    if not need(isinstance(r, dict) and r.get("schema") == run.SCHEMA and all(k in r for k in REQUIRED),
                "schema: wrong or missing fields"):
        return problems
    mode = r["mode"]
    need(mode in MODES, f"mode {mode!r} is not one of {MODES}")
    if expect_mode:
        need(mode == expect_mode, f"mode is {mode}, expected {expect_mode}")
    project = rd / "project"
    sessions = r["sessions"]
    jobs = r["jobs"]
    need(isinstance(jobs, list) and jobs and jobs[0].get("step") == "worker", "jobs: no worker job first")

    # the mode against the evidence in the dispatcher envelopes
    try:  # the fake binary directory is private: plan.json holds it, the receipt does not
        plan = json.loads((rd / "plan.json").read_text(encoding="utf-8"))
        fake_bin = os.path.realpath(plan["fake_bin"]) if plan.get("fake_bin") else None
    except (OSError, ValueError, KeyError, TypeError):
        return problems + ["plan.json unreadable"]
    need(plan.get("mode") == mode, "plan.json mode differs from the receipt")
    envelopes = {}
    for job in jobs if isinstance(jobs, list) else []:
        try:
            block, stdout = run.envelope(rd, job["id"])
        except (OSError, ValueError, KeyError):
            need(False, f"job {job.get('id')}: dispatcher envelope unreadable")
            continue
        envelopes[job["id"]] = (block, stdout)
        harness, exe = block["harness"], block["argv"][0]
        need(sha256(stdout) == block["stdout"]["sha256"] == job["stdout_sha256"], f"job {job['id']}: stdout hash")
        need(usage_total_ok(block, job), f"job {job['id']}: usage_total differs from the envelope")
        if mode == "offline":
            need(harness == "local-command" and not fake_bin, f"job {job['id']}: offline needs local-command")
        elif mode == "fake":
            need(harness in run.EXECUTABLE and harness == r["harness"], f"job {job['id']}: fake needs {r['harness']}")
            need(fake_bin is not None and os.path.dirname(os.path.realpath(exe)) == fake_bin,
                 f"job {job['id']}: binary is not inside fake_bin")
            need(run.has_marker(exe), f"job {job['id']}: fake binary lacks the marker")
        elif mode == "live":
            need(harness in run.EXECUTABLE and harness == r["harness"], f"job {job['id']}: live needs {r['harness']}")
            need(fake_bin is None, "live receipt names a fake_bin")
            need(not run.has_marker(exe), f"job {job['id']}: live binary carries the fake marker")
            need(block.get("harness_version") is not None, f"job {job['id']}: no harness_version")
            need((job.get("usage_total") or 0) > 0 and (run.usage_total(block) or 0) > 0,
                 f"job {job['id']}: no token usage")

    # ready, re-run now
    proc = subprocess.run([sys.executable, "-B", str(CONTINUITY), "ready", "--project", str(project),
                           "--session", sessions["restart"]], capture_output=True, text=True)
    need(proc.returncode == 0, f"ready exits {proc.returncode}: {proc.stdout.strip()[:300]}")
    need(r["ready"] and r["ready"].get("exit") == 0, "receipt records a ready exit other than 0")

    # the answer, the checker and the single action
    answer = project / "answer.md"
    if need(answer.is_file(), "answer.md missing"):
        need(sha256(answer.read_bytes()) == r["answer_sha256"], "answer.md hash differs from the receipt")
        need(answer.read_text(encoding="utf-8").strip() == str(r["answer"]), "answer.md differs from the answer")
    checkers = [j for j in jobs if j.get("step") == "checker"]
    verdict = None
    if need(bool(checkers) and checkers[-1]["id"] in envelopes, "no checker job"):
        block, stdout = envelopes[checkers[-1]["id"]]
        verdict = run.normalize_verdict(run.answer_text(block["harness"], stdout))
    independent(r, rd, jobs, checkers, envelopes, need)
    need(verdict == "CONFIRMED", f"the checker said {verdict!r}, not CONFIRMED")
    need(r["checker_verdict"] == verdict, "receipt checker verdict differs from the envelope")
    try:  # independent of the checker: the published answer must be the sum of the run's own input
        total = sum(int(x) for x in (project / "input.txt").read_text(encoding="utf-8").split())
    except (OSError, ValueError):
        total = None
    need(total is not None and str(r["answer"]) == str(total), "answer is not the sum of input.txt")
    if jobs and jobs[0]["id"] in envelopes:
        block, stdout = envelopes[jobs[0]["id"]]
        need(run.answer_text(block["harness"], stdout) == str(r["answer"]), "worker answer differs from the receipt")
    actions = rd / "actions.jsonl"
    need(actions.is_file() and len(actions.read_text().splitlines()) == 1, "actions.jsonl must hold exactly one entry")

    # the Next was consumed by the restart session
    try:
        doc = json.loads((project / ".ai-continuity" / "ledger" / "ledger.json").read_text())
        cp = next((c for c in doc["checkpoints"] if c["revision"] == r["resume"]["checkpoint_revision"]), None)
    except (OSError, ValueError, KeyError, TypeError):
        cp = None
    need(cp is not None and cp["resume"]["consumed_by"] == sessions["restart"],
         "the Next was not consumed by the restart session")
    need(sessions["restart"] != sessions["coordinator"], "the restart reuses the first session")
    need(isinstance(r["kill"], dict) and r["kill"].get("signal") == 9 and r["kill"].get("pid_gone"),
         "no SIGKILL record with a vanished pid")

    # the calls
    try:
        data = (rd / "calls.jsonl").read_bytes()
        count = r["calls"]["lines"]
        prefix = b"".join(data.splitlines(keepends=True)[:count])
        need(sha256(prefix) == r["calls"]["sha256"], "calls.jsonl differs from the receipt")
        for line in data.splitlines():
            argv = json.loads(line)["argv"]
            need("--project" in argv, f"a call without --project: {argv[:1]}")
    except (OSError, ValueError, KeyError):
        need(False, "calls.jsonl unreadable")
    return problems


def independent(r, rd, jobs, checkers, envelopes, need):
    """Checker independence: refuter role (receipt and ledger), another job than the worker's, the claimed sum in
    the prompt it was given (envelope argv, else the ledger spool)."""
    worker = jobs[0].get("id") if jobs and isinstance(jobs[0], dict) else None
    try:
        ledger_jobs = json.loads((rd / "project" / ".ai-continuity" / "ledger" / "ledger.json").read_text())["jobs"]
    except (OSError, ValueError, KeyError):
        ledger_jobs = {}
    for job in checkers:
        jid = job["id"]
        need(job.get("role") == "refuter" and (ledger_jobs.get(jid) or {}).get("role") == "refuter",
             f"checker independence: job {jid} is not a refuter")
        need(jid != worker, f"checker independence: job {jid} is the worker's job")
        prompt = ""
        if jid in envelopes:
            prompt = " ".join(str(a) for a in envelopes[jid][0].get("argv", []))
        try:
            prompt += (rd / "project" / ".ai-continuity" / "ledger" / "spool" / f"{jid}.prompt").read_text(
                encoding="utf-8")
        except OSError:
            pass
        need(f"Claimed sum: {r['answer']}\n" in prompt + "\n", f"checker independence: the prompt of {jid} lacks the claimed sum")


def usage_total_ok(block, job):
    return run.usage_total(block) == job.get("usage_total")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt")
    parser.add_argument("--out")
    parser.add_argument("--expect-mode", choices=MODES)
    args = parser.parse_args(argv)
    try:
        path = Path(args.receipt) if args.receipt else default_receipt(args.out or run.default_out())
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    problems = check(path, args.expect_mode)
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print("receipt verified")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
