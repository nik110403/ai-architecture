#!/usr/bin/env python3
"""Chain/head regressions: all records and commits live in a temporary repository."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIBE = Path(
    os.environ.get(
        "SCRIBE_UNDER_TEST", Path(__file__).resolve().parents[1] / "tribunal-scribe"
    )
)


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / "seed").write_text("seed")
        self.git("add", "seed")
        self.git("commit", "-qm", "seed")
        self.ws = self.root / "record"
        self.run_scribe("open", self.ws)
        self.payload = self.root / "payload"
        self.payload.write_bytes(b"alpha\n```\n## Round 9\n")
        self.head1 = self.append(1)
        self.backup = self.root / "earlier"
        shutil.copytree(self.ws, self.backup)
        self.payload.write_bytes(b"beta without newline")
        self.head2 = self.append(2)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True
        )

    def run_scribe(self, *args, rc=0):
        result = subprocess.run(
            [str(SCRIBE), *map(str, args)],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout

    def append(self, seat):
        out = self.run_scribe("append", self.ws, 0, seat, f"lens{seat}", self.payload)
        return next(
            line.split()[1]
            for line in out.splitlines()
            if line.startswith("chain-head ")
        )

    def verify(self, rc=0, head=None):
        args = [] if head is None else ["--expect-head", head]
        return self.run_scribe("verify", self.ws, *args, rc=rc)

    def test_valid_and_moved_records(self):
        self.assertNotEqual(self.head1, self.head2)
        self.assertIn("local consistency only", self.verify())
        self.assertIn("caller-supplied head matched", self.verify(head=self.head2))
        moved = self.root / "moved"
        shutil.copytree(self.ws, moved)
        self.run_scribe("verify", moved, "--expect-head", self.head2)

    def test_expected_head_mismatch_and_invalid_arguments(self):
        self.verify(rc=3, head="0" * 64)
        self.run_scribe("verify", self.ws, "--expect-head", rc=2)
        self.run_scribe("verify", self.ws, "--expect-head", "not-a-hash", rc=2)

    def test_chain_format_matches_upstream(self):
        prev = ""
        for line in (self.ws / "CHAIN.SHA256").read_text().splitlines():
            head, tag, turn, transcript = line.split()
            self.assertEqual(
                head,
                hashlib.sha256((prev + tag + turn + transcript).encode()).hexdigest(),
            )
            prev = line

    def test_changed_removed_reordered_and_malformed_chain(self):
        path = self.ws / "CHAIN.SHA256"
        original = path.read_text()
        lines = original.splitlines(keepends=True)
        for changed in (
            original.replace(" r0-s2 ", " r0-s9 "),
            lines[0],
            "".join(reversed(lines)),
            "malformed\n",
            "",
            original + "\n",
            original.replace(" r0-s2 ", " ../../escape "),
        ):
            with self.subTest(changed=changed):
                path.write_text(changed)
                self.verify(rc=3)
        path.unlink()
        self.verify(rc=3, head=self.head2)

    def test_consistent_rollback_needs_retained_head(self):
        shutil.rmtree(self.ws)
        shutil.copytree(self.backup, self.ws)
        self.verify()  # Earlier state is locally consistent; do not claim rollback detection here.
        self.verify(rc=3, head=self.head2)

    def test_consistent_forgery_needs_retained_head(self):
        other = self.root / "forged"
        self.run_scribe("open", other)
        self.payload.write_text("forged")
        self.run_scribe("append", other, 0, 1, "lens1", self.payload)
        self.run_scribe("append", other, 0, 2, "lens2", self.payload)
        shutil.rmtree(self.ws)
        shutil.copytree(other, self.ws)
        self.verify()
        self.verify(rc=3, head=self.head2)

    def test_recomputed_chain_reordering_rejected(self):
        path = self.ws / "CHAIN.SHA256"
        prev = ""
        rewritten = []
        for line in reversed(path.read_text().splitlines()):
            _, tag, turn, transcript = line.split()
            head = hashlib.sha256((prev + tag + turn + transcript).encode()).hexdigest()
            prev = f"{head} {tag} {turn} {transcript}"
            rewritten.append(prev)
        path.write_text("\n".join(rewritten) + "\n")
        self.verify(rc=3)

    def test_mutations_reject_broken_chain_before_writing(self):
        (self.ws / "CHAIN.SHA256").unlink()
        before = (self.ws / "transcript.md").read_bytes()
        self.run_scribe("append", self.ws, 0, 3, "lens3", self.payload, rc=3)
        self.run_scribe("close", self.ws, "cap", 0, rc=3)
        self.assertEqual(before, (self.ws / "transcript.md").read_bytes())
        self.assertFalse((self.ws / "turns/r0-s3.md").exists())

    def test_closed_head_and_no_automatic_commit(self):
        before = self.git("rev-parse", "HEAD").stdout
        out = self.run_scribe("close", self.ws, "saturation", 0)
        head = next(
            line.split()[1]
            for line in out.splitlines()
            if line.startswith("close-head: ")
        )
        self.assertNotEqual(head, self.head2)
        self.verify(head=head)
        self.verify(rc=3, head=self.head2)
        self.assertFalse((self.ws / ".tribunal-active").exists())
        self.assertEqual(before, self.git("rev-parse", "HEAD").stdout)

    def test_seat_tamper_and_missing_seat(self):
        path = self.ws / "seat-1-lens1.md"
        path.write_text("changed")
        self.verify(rc=3)
        path.unlink()
        self.verify(rc=3)

    def test_relative_path_commit_check(self):
        self.git("add", "record/turns", "record/seat-1-lens1.md")
        self.git("commit", "-qm", "record touched")
        self.run_scribe("append", "record", 0, 3, "lens3", self.payload, rc=3)

    def test_checksum_history_tamper(self):
        path = self.ws / "TRANSCRIPT.SHA256"
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("0" * 64 + lines[0][64:] + lines[1])
        self.verify(rc=3)

    def test_open_rejects_chain_and_checksum_leftovers(self):
        for name in ("CHAIN.SHA256", "TRANSCRIPT.SHA256"):
            ws = self.root / name
            ws.mkdir()
            (ws / name).write_text("leftover")
            self.run_scribe("open", ws, rc=1)


if __name__ == "__main__":
    unittest.main()
