#!/usr/bin/env python3
"""Real process/FS regressions for the edit-budget transaction. Stdlib only."""

import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


HOOK = Path(__file__).resolve().parents[1] / "hooks" / "edit-budget-gate.py"


class EditBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.counter = self.state / "edit-budget-session"
        self.lock = self.state / "edit-budget-session.lock"
        self.config = self.root / "gates.json"
        self.config.write_text(json.dumps({"edit_budget_models": {"budgeted": 2}}))
        transcript = self.root / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"model": "budgeted"}}) + "\n"
        )
        self.payload = {
            "session_id": "session",
            "transcript_path": str(transcript),
            "tool_name": "Write",
            "tool_input": {"file_path": "file.py", "content": "x = 1\n" * 200},
        }
        self.env = dict(
            os.environ,
            AIARCH_CONFIG=str(self.config),
            CLAUDE_HOOK_STATE_DIR=str(self.state),
        )

    def invoke(self, payload=None, fault=None, env=None, wrapper=False):
        if fault:
            # Exercise an actual child hook; inject only an otherwise hard-to-provoke OS error.
            code = (
                "import runpy,sys; from unittest.mock import patch; "
                f"sys.path.insert(0, {str(HOOK.parent / 'lib')!r}); "
                f"p=patch({fault!r}, side_effect=OSError('injected failure')); p.start(); "
                f"runpy.run_path({str(HOOK)!r}, run_name='__main__')"
            )
            cmd = [sys.executable, "-I", "-c", code]
        elif wrapper:
            cmd = ["bash", str(HOOK.parent / "run-hook.sh"), "pretool", HOOK.name]
        else:
            cmd = [sys.executable, "-I", str(HOOK)]
        result = subprocess.run(
            cmd,
            input=json.dumps(payload or self.payload),
            text=True,
            capture_output=True,
            env=env or self.env,
            cwd=self.work,
            check=True,
            timeout=8,
        )
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def assert_state_error(self, result):
        self.assertIn("hookSpecificOutput", result, "state error silently allowed")
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("edit budget state", out["permissionDecisionReason"].lower())
        self.assertIn("failing closed", out["permissionDecisionReason"].lower())

    def test_first_use_and_limit_and_session_identity(self):
        self.assertEqual(self.invoke(), {})
        self.assertEqual(self.counter.read_text(), "1")
        first_inode = self.counter.stat().st_ino
        self.assertEqual(self.invoke(), {})
        self.assertNotEqual(self.counter.stat().st_ino, first_inode)
        self.assertEqual(
            self.invoke()["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(self.counter.read_text(), "3")
        other = dict(self.payload, session_id="other")
        self.assertEqual(self.invoke(other), {})
        self.assertEqual((self.state / "edit-budget-other").read_text(), "1")
        for sid in ("../bad", ""):
            self.invoke(dict(self.payload, session_id=sid))
        self.assertEqual((self.state / "edit-budget-unknown").read_text(), "2")
        self.assertEqual(self.counter.stat().st_mode & 0o777, 0o600)

    def test_concurrent_processes_no_lost_counts_or_extra_allows(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(lambda _: self.invoke(), range(192)))
        # Lock waiters may time out under host load and fail closed without
        # counting; every other invocation must be counted exactly once.
        denies = [
            result
            for result in results
            if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        ]
        allows = sum(result == {} for result in results)
        self.assertEqual(allows, 2)
        self.assertEqual(allows + len(denies), 192, "non-allow result was not a deny")
        errors = sum(
            "edit budget state" in r["hookSpecificOutput"]["permissionDecisionReason"].lower()
            and "failing closed" in r["hookSpecificOutput"]["permissionDecisionReason"].lower()
            for r in denies
        )
        self.assertEqual(int(self.counter.read_text()), 192 - errors)
        self.assertEqual(len(denies) - errors, 190 - errors)

    def test_corrupt_state_is_not_reset(self):
        for raw in (
            b"",
            b"garbage",
            b"-1",
            b"1.0",
            b"True",
            b"1\x002",
            b"\xff",
            b"9" * 257,
            b"1" + b" " * 255 + b"garbage",
        ):
            with self.subTest(raw=raw[:20]):
                self.counter.write_bytes(raw)
                self.assert_state_error(self.invoke())
                self.assertEqual(self.counter.read_bytes(), raw)

    def test_nonregular_state_and_lock_are_bounded_denials(self):
        for path in (self.counter, self.lock):
            # Counter failure cases legitimately leave the stable lock sidecar.
            path.unlink(missing_ok=True)
            for kind in ("fifo", "directory", "symlink"):
                with self.subTest(path=path.name, kind=kind):
                    if kind == "fifo":
                        os.mkfifo(path)
                    elif kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(self.root / "missing")
                    try:
                        start = time.monotonic()
                        self.assert_state_error(self.invoke())
                        self.assertLess(time.monotonic() - start, 3)
                    finally:
                        path.rmdir() if kind == "directory" else path.unlink()

    def test_external_lock_blocks_entire_transaction_then_recovers(self):
        self.counter.write_text("1")
        with self.lock.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            self.assert_state_error(self.invoke(wrapper=True))
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(self.counter.read_text(), "1")
        self.assertEqual(self.invoke(wrapper=True), {})
        self.assertEqual(self.counter.read_text(), "2")

    def test_persistence_and_lock_syscall_errors_do_not_allow_or_truncate(self):
        for fault in ("os.replace", "os.fsync", "tempfile.mkstemp", "fcntl.flock"):
            with self.subTest(fault=fault):
                self.counter.write_text("1")
                self.assert_state_error(self.invoke(fault=fault))
                self.assertEqual(self.counter.read_text(), "1")
                self.assertEqual(
                    sorted(p.name for p in self.state.iterdir()),
                    [self.counter.name, self.lock.name],
                )

    @unittest.skipIf(os.getuid() == 0, "root bypasses discretionary permissions")
    def test_unwritable_directory_and_unreadable_state(self):
        self.counter.write_text("1")
        self.lock.touch(mode=0o600)  # lock opens normally; persistence itself must fail
        self.state.chmod(0o500)
        try:
            self.assert_state_error(self.invoke())
        finally:
            self.state.chmod(0o700)
        self.counter.chmod(0)
        try:
            self.assert_state_error(self.invoke())
        finally:
            self.counter.chmod(0o600)
        self.assertEqual(self.counter.read_text(), "1")

    def test_state_path_precedence_and_plugin_data_fallback(self):
        fallback = self.root / "plugin-data"
        env = dict(self.env, CLAUDE_PLUGIN_DATA=str(fallback))
        self.assertEqual(self.invoke(env=env), {})
        self.assertFalse(fallback.exists())
        del env["CLAUDE_HOOK_STATE_DIR"]
        self.assertEqual(self.invoke(env=env), {})
        self.assertEqual((fallback / self.counter.name).read_text(), "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
