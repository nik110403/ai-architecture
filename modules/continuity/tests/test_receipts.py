"""R9-U04b: usage records, checkpoint receipts, evidence hashing, schema growth."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ContCase, L, jobs  # noqa: E402
from modules.continuity import receipts  # noqa: E402


class CpMixin:
    """CLI shortcuts for the checkpoint family. Not a TestCase: importing it never re-runs tests."""

    def write(self, name, text="v1"):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def owner_pid(self):
        """A live process that is not the test runner (the helpers' reap signals every recorded dispatcher pid)."""
        if getattr(self, "_owner", None) is None:
            self._owner = subprocess.Popen(["sleep", "300"])
            self.addCleanup(self.reap_proc, self._owner)
        return self._owner.pid

    def ext(self, job_id, pid=None):
        code, out = self.cli("external", "register", extra=[
            "--job-id", job_id, "--role", "researcher", "--access", "read", "--session", "cli:a",
            "--owner-pid", str(pid or self.owner_pid())])
        self.assertEqual(code, 0, out)

    def ext_bank(self, job_id, outcome="succeeded"):
        cause = "completed" if outcome == "succeeded" else "orphaned_external"
        code, out = self.cli("external", "bank", extra=["--job-id", job_id, "--outcome", outcome, "--cause", cause])
        self.assertEqual(code, 0, out)

    def usage(self, tokens="500", session="cli:a", *extra):
        code, out = self.cli("usage", "record", extra=["--session", session, "--tokens", str(tokens), *extra])
        self.assertEqual(code, 0, out)
        return out

    def cp(self, *extra, state="the state", nxt="the next step", evidence=("ev.txt",), session="cli:a", expect=0):
        args = ["--state", state, "--next", nxt]
        for ref in evidence:
            args += ["--evidence", ref]
        if session:
            args += ["--session", session]
        code, out = self.cli("checkpoint", extra=[*args, *extra])
        self.assertEqual(code, expect, out)
        return out

    def ready(self, session="cli:a", expect=None):
        code, out = self.cli("ready", extra=["--session", session])
        self.assertEqual(code, 0 if out["ready"] else 1, out)
        if expect is not None:
            self.assertEqual(out["ready"], expect, out)
        return out

    def reasons(self, out):
        return " | ".join(out["reasons"])


class Receipts(CpMixin, ContCase):
    def test_checkpoint_acknowledges_banked_jobs_and_records_pending(self):
        self.write("ev.txt")
        self.ext("e1")
        self.ext_bank("e1")
        self.ext("e2")
        out = self.cp()
        self.assertEqual((out["covered_jobs"], out["pending_jobs"]), (["e1"], ["e2"]))
        doc = self.doc()
        cp = doc["checkpoints"][-1]
        self.assertEqual(doc["jobs"]["e1"]["acknowledged_revision"], cp["revision"])
        self.assertIsNone(doc["jobs"]["e2"]["acknowledged_revision"])
        self.assertEqual(cp["receipt_sha256"], receipts.receipt_hash(cp))
        self.assertEqual(cp["jobs_digest"], receipts.jobs_digest(doc))
        self.assertEqual(cp["resume"], {"consumed_by": None, "consumed_at": None})

    def test_a_late_result_after_acknowledgement_is_covered_by_the_next_checkpoint(self):
        self.write("ev.txt")
        self.ext("e1")
        self.ext_bank("e1")
        self.cp()

        def late(doc):
            job = doc["jobs"]["e1"]
            job["post_bank"].append({"observed_at": L.now(), "outcome": "failed", "sha256": "a" * 64, "source": "file"})
            jobs.touch(doc, job)
        self.edit(late)
        self.assertEqual(self.cp()["covered_jobs"], ["e1"])

    def test_accept_failed_only_names_banked_failures(self):
        self.write("ev.txt")
        self.ext("good")
        self.ext_bank("good")
        self.ext("bad")
        self.ext_bank("bad", "failed")
        self.ext("open")
        for name in ("good", "open", "nope"):
            code, out = self.cli("checkpoint", extra=["--state", "s", "--next", "n", "--evidence", "ev.txt",
                                                       "--accept-failed", name])
            self.assertEqual(code, 2, (name, out))
        self.assertEqual(self.doc()["checkpoints"], [])  # a refused checkpoint writes nothing
        self.assertEqual(self.cp("--accept-failed", "bad")["accepted_failed"], ["bad"])

    def test_evidence_hash_literal_and_refused_refs(self):
        self.write("ev.txt", "hello")
        (self.dir / "outside.txt").write_text("x")
        os.symlink(self.dir / "outside.txt", self.project / "link.txt")
        for ref in ("../outside.txt", "link.txt", "missing.txt"):  # path-shaped but not a regular file in the project
            self.cp(evidence=("ev.txt", ref), expect=2)
        self.cp(evidence=("ev.txt", "commit abc123"))
        items = {i["ref"]: i for i in self.doc()["checkpoints"][-1]["evidence"]}
        self.assertEqual(items["ev.txt"]["sha256"], L.sha256_hex(b"hello"))
        self.assertEqual(items["ev.txt"]["bytes"], 5)
        for ref in ("commit abc123",):
            self.assertEqual((items[ref]["sha256"], items[ref]["bytes"]), (None, None), ref)

    def test_tracker_must_be_a_regular_file_inside_the_project(self):
        self.write("ev.txt")
        self.write("TRACKER.md", "t")
        (self.dir / "outside.md").write_text("x")
        for bad in ("../outside.md", str(self.dir / "outside.md"), "missing.md"):
            code, out = self.cli("checkpoint", extra=["--state", "s", "--next", "n", "--evidence", "ev.txt",
                                                       "--tracker", bad])
            self.assertEqual(code, 2, (bad, out))
        self.cp("--tracker", str(self.project / "TRACKER.md"))
        self.assertEqual(self.doc()["checkpoints"][-1]["tracker"],
                         {"path": "TRACKER.md", "sha256": L.sha256_hex(b"t")})

    def test_text_limits_and_evidence_required(self):
        self.write("ev.txt")
        for extra in (["--state", "s" * 2001, "--next", "n"], ["--state", "s", "--next", "n" * 1001],
                      ["--state", " ", "--next", "n"]):
            code, _ = self.cli("checkpoint", extra=[*extra, "--evidence", "ev.txt"])
            self.assertEqual(code, 2, extra[:2])
        proc = subprocess.run([sys.executable, "-B", str(L.ROOT / "modules/continuity/continuity.py"), "checkpoint",
                               "--project", str(self.project), "--state", "s", "--next", "n"], capture_output=True)
        self.assertEqual(proc.returncode, 2)  # no --evidence: a usage error

    def test_at_most_twenty_checkpoints_newest_kept(self):
        self.write("ev.txt")
        for n in range(22):
            self.cp(nxt=f"step {n}")
        cps = self.doc()["checkpoints"]
        self.assertEqual((len(cps), cps[-1]["next"], cps[0]["next"]), (20, "step 21", "step 2"))

    def test_usage_records_unknown_and_evicts_the_oldest_of_64(self):
        self.usage("unknown", "claude-code:s1", "--model", "m1", "--limit", "1000")
        self.assertIsNone(self.doc()["sessions"]["claude-code:s1"]["usage_tokens"])
        self.usage("7", "claude-code:s1")  # a later record keeps the earlier model and limit
        rec = self.doc()["sessions"]["claude-code:s1"]
        self.assertEqual((rec["usage_tokens"], rec["limit_tokens"], rec["model"]), (7, 1000, "m1"))
        for n in range(66):
            self.edit(lambda doc, n=n: doc["sessions"].update({}))
            self.usage("1", f"cli:s{n}")
        sessions = self.doc()["sessions"]
        self.assertEqual(len(sessions), 64)
        self.assertIn("cli:s65", sessions)
        self.assertNotIn("claude-code:s1", sessions)
        for bad in (["--session", "nocolon", "--tokens", "1"], ["--session", "cli:a b", "--tokens", "1"],
                    ["--session", "other:a", "--tokens", "1"], ["--session", "cli:a", "--tokens", "-1"],
                    ["--session", "cli:a", "--tokens", "lots"]):
            self.assertEqual(self.cli("usage", "record", extra=bad)[0], 2, bad)

    def test_concurrent_checkpoints_all_land(self):
        self.write("ev.txt")
        argv = [sys.executable, "-B", str(L.ROOT / "modules/continuity/continuity.py"), "checkpoint", "--project",
                str(self.project), "--state", "s", "--next", "n", "--evidence", "ev.txt"]
        procs = [subprocess.Popen(argv, stdout=subprocess.DEVNULL) for _ in range(6)]
        self.assertEqual([p.wait(60) for p in procs], [0] * 6)
        revisions = [c["revision"] for c in self.doc()["checkpoints"]]
        self.assertEqual(len(revisions), 6)
        self.assertEqual(revisions, sorted(set(revisions)))


class SchemaGrowth(CpMixin, ContCase):
    def test_a_ledger_written_by_u04a_validates_and_round_trips(self):
        self.ext("e1")
        self.ext_bank("e1")

        def downgrade(doc):  # what U04a wrote: no usage_total or budget_state in a result
            for job in doc["jobs"].values():
                job["result"].pop("usage_total", None)
                job["result"].pop("budget_state", None)
        self.edit(downgrade)
        old = self.doc()
        self.assertNotIn("budget_state", old["jobs"]["e1"]["result"])
        L.validate(old)
        self.cli("drain", "start", extra=["--reason", "t"])  # a write after the upgrade keeps the old record valid
        self.cli("drain", "end")
        self.assertEqual(self.job("e1")["result"], old["jobs"]["e1"]["result"])
        self.ext("e2")
        self.ext_bank("e2")  # a new bank adds both keys
        self.assertEqual(self.job("e2")["result"]["budget_state"], "unbounded")
        self.assertIsNone(self.job("e2")["result"]["usage_total"])

    def test_unknown_keys_and_bad_receipts_are_still_refused(self):
        self.write("ev.txt")
        self.cp()
        for change in (lambda d: d["checkpoints"][-1].update(extra=1),
                       lambda d: d["checkpoints"][-1].update(state=""),
                       lambda d: d["sessions"].update({"cli:x": {"harness": "cli"}}),
                       lambda d: d["jobs"].update({})):
            doc = self.doc()
            change(doc)
            if doc == self.doc():
                continue
            with self.assertRaises(L.LedgerInvalid):
                L.validate(doc)


if __name__ == "__main__":
    unittest.main()
