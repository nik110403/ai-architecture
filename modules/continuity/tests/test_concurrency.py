import fcntl
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ROOT, ContCase, L, jobs  # noqa: E402

WORKER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from modules.continuity import jobs, ledger as L
state, mode, tag = sys.argv[2], sys.argv[3], sys.argv[4]
ctx = jobs.Context(state)

def record(job_id):
    return {"job_id": job_id, "kind": "dispatch", "role": "researcher", "access": "read",
            "owner": {"harness": "cli", "session_id": None}, "state": "launching", "created_at": L.now(),
            "updated_at": L.now(), "updated_revision": 0, "submitter": {"pid": 1, "start_ticks": None},
            "dispatcher": None, "workdir": None, "worktree_plan": None, "prompt_sha256": None,
            "budget": {"tokens": None}, "cancel": None, "result": None, "post_bank": [],
            "acknowledged_revision": None}

def obs(sha):
    return {"outcome": "succeeded", "cause": "completed", "source": "file", "result_sha256": sha, "record": None,
            "exit_code": None, "usage": None, "worktree": None, "evidence": [], "note": None, "trust": "dispatcher"}

if mode == "create":
    flags = []
    for i in range(25):
        job_id = f"{tag}-{i}"
        with ctx.ledger.transaction() as doc:
            record_ = record(job_id)
            record_["updated_revision"] = doc["revision"] + 1
            doc["jobs"][job_id] = record_
        flags.append(jobs.bank(ctx.ledger, job_id, obs("%064x" % (int(tag[1:]) * 1000 + i)))["banked_now"])
    print(json.dumps(flags))
else:
    print(json.dumps(jobs.bank(ctx.ledger, "shared", obs("%064x" % (int(tag[1:]) + 1)))["banked_now"]))
'''


class Concurrency(ContCase):
    def swarm(self, mode, count=8):
        script = self.dir / "worker_proc.py"
        script.write_text(WORKER)
        self.set_lock_wait(60)
        procs = [subprocess.Popen([sys.executable, "-B", str(script), str(ROOT), str(self.state), mode, f"p{n}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(count)]
        out = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, stderr)
            out.append(json.loads(stdout))
        return out

    def set_lock_wait(self, seconds):
        path = self.state / "config.json"
        config = json.loads(path.read_text())
        config["lock_wait_seconds"] = seconds
        path.write_text(json.dumps(config))

    def test_eight_processes_create_and_bank_two_hundred_jobs(self):
        results = self.swarm("create")
        self.assertTrue(all(all(flags) for flags in results), "each job is banked exactly once by its own process")
        doc = self.doc()
        self.assertEqual(len(doc["jobs"]), 200)
        self.assertTrue(all(j["state"] == "banked" and j["post_bank"] == [] for j in doc["jobs"].values()))
        self.assertEqual(doc["revision"], 400, "init is revision 0; one revision per committed transaction")
        self.assertEqual(sorted(j["result"]["bank_revision"] for j in doc["jobs"].values()),
                         sorted(set(j["result"]["bank_revision"] for j in doc["jobs"].values())))

    def test_eight_processes_bank_the_same_job_with_different_observations(self):
        ctx = self.ctx()
        with ctx.ledger.transaction() as doc:
            doc["jobs"]["shared"] = {
                "job_id": "shared", "kind": "dispatch", "role": "researcher", "access": "read",
                "owner": {"harness": "cli", "session_id": None}, "state": "launching", "created_at": L.now(),
                "updated_at": L.now(), "updated_revision": 1, "submitter": {"pid": 1, "start_ticks": None},
                "dispatcher": None, "workdir": None, "worktree_plan": None, "prompt_sha256": None,
                "budget": {"tokens": None}, "cancel": None, "result": None, "post_bank": [],
                "acknowledged_revision": None}
        results = self.swarm("same")
        self.assertEqual(sorted(results), [False] * 7 + [True], "exactly one banked_now")
        job = self.job("shared")
        self.assertEqual(job["state"], "banked")
        self.assertEqual(len(job["post_bank"]), 7)
        winner = job["result"]["result_sha256"]
        self.assertNotIn(winner, [e["sha256"] for e in job["post_bank"]])
        self.assertEqual(len({e["sha256"] for e in job["post_bank"]}), 7)

    def test_lock_timeout_exits_3_and_leaves_the_ledger_byte_identical(self):
        self.set_lock_wait(0.3)
        before = self.ledger_bytes()
        fd = os.open(self.state / "ledger.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.start()
            code_drain, _ = self.cli("drain", "start", "--reason", "x")
        finally:
            os.close(fd)
        self.assertEqual((code, code_drain), (3, 3), out)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(list((self.state / "spool").iterdir()), [], "no prompt file without a job record")
        self.assertEqual(list((self.state / "runs").iterdir()), [], "no dispatcher without a job record")

    def test_a_dead_lock_holder_never_blocks_the_next_writer(self):
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, os, sys, time\n"
            f"fd = os.open({str(self.state / 'ledger.lock')!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)")],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.reap_proc, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        holder.kill()
        holder.wait()
        code, out = self.cli("drain", "start", "--reason", "after the crash")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
