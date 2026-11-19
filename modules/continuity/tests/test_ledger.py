import contextlib
import fcntl
import io
import json
import os
import stat
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ContCase, continuity, jobs, L  # noqa: E402


class LayoutTests(ContCase):
    def test_descriptor_is_valid_and_claims_only_planned(self):
        from helpers import CONT
        from modules.contracts import contracts
        descriptor = contracts.check_module_descriptor(CONT, module="continuity")
        self.assertEqual((descriptor["claimed_support"], descriptor["evidence"]), ("planned", []))

    def test_init_layout_and_modes(self):
        self.assertEqual(self.state, self.project / ".ai-continuity" / "ledger")
        for name in ("ledger.json", "config.json", "ledger.lock"):
            path = self.state / name
            if name == "ledger.lock":
                self.cli("status")  # the lock file appears on first use
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, name)
        for path in (self.project / ".ai-continuity", self.state, self.state / "runs", self.state / "spool"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))
        doc = self.doc()
        self.assertEqual((doc["project_root"], doc["revision"], doc["jobs"], doc["checkpoints"], doc["sessions"]),
                         (str(self.project), 0, {}, [], {}))
        self.assertFalse((self.project / ".gitignore").exists(), "init must never edit .gitignore")

    def test_init_reminds_and_is_idempotent(self):
        code, out = self.cli("init", project=False, extra=["--project", str(self.project)])
        self.assertEqual((code, out["created"]), (0, False))
        self.assertIn(".gitignore", out["reminder"])

    def test_init_refuses_a_state_directory_of_another_project(self):
        other = self.dir / "other"
        other.mkdir()
        (other / ".ai-continuity").symlink_to(self.project / ".ai-continuity")
        code, out = self.cli("init", project=False, extra=["--project", str(other)])
        self.assertEqual(code, 2, out)
        self.assertIn("real directory", out["error"])

    def test_init_refuses_a_ledger_that_names_another_root(self):
        self.edit(lambda d: d.update(project_root="/elsewhere"))
        code, out = self.cli("init", project=False, extra=["--project", str(self.project)])
        self.assertEqual(code, 2, out)
        self.assertIn("another project root", out["error"])

    def test_symlinked_project_or_state_component_is_refused(self):
        link = self.dir / "link"
        link.symlink_to(self.project)
        code, out = self.cli("status", project=False, extra=["--project", str(link)])
        self.assertEqual(code, 2, out)
        moved = self.dir / "real-ledger"
        self.state.rename(moved)
        self.state.symlink_to(moved)
        code, out = self.cli("status")
        self.assertEqual(code, 2, out)

    def test_group_writable_state_is_refused(self):
        os.chmod(self.state, 0o770)
        code, out = self.cli("status")
        self.assertEqual(code, 2, out)
        self.assertIn("not group- or other-writable", out["error"])

    def test_exactly_one_of_project_or_state(self):
        argv = ["status", "--project", str(self.project), "--state", str(self.state)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(continuity.main(argv), 2)
            self.assertEqual(continuity.main(["status"]), 2)
        code, out = self.cli("status", project=False, extra=["--state", str(self.state)])
        self.assertEqual((code, out["storage"]), (0, "ok"))

    def test_missing_fcntl_refuses_every_command(self):
        with mock.patch.object(L, "fcntl", None), contextlib.redirect_stdout(io.StringIO()):
            buf_code = continuity.main(["status", "--project", str(self.project)])
            init_code = continuity.main(["init", "--project", str(self.project)])
        self.assertEqual((buf_code, init_code), (2, 2))
        with mock.patch.object(L, "fcntl", None):
            with self.assertRaises(L.Refused) as caught:
                L.Ledger(self.state)
        self.assertIn("unsupported platform: the ledger needs fcntl.flock", str(caught.exception))

    def test_config_json_unknown_keys_are_refused(self):
        path = self.state / "config.json"
        config = json.loads(path.read_text())
        config["surprise"] = 1
        path.write_text(json.dumps(config))
        code, out = self.cli("status")
        self.assertEqual(code, 2, out)
        self.assertIn("unknown keys", out["error"])


class TransactionTests(ContCase):
    def test_commit_increments_the_revision_and_exception_writes_nothing(self):
        ledger = self.ctx().ledger
        before = self.ledger_bytes()
        with ledger.transaction() as doc:
            doc["drain"] = {"since": L.now(), "reason": "x"}
        self.assertEqual(ledger.read()["revision"], 1)
        mid = self.ledger_bytes()
        with self.assertRaises(KeyError):
            with ledger.transaction() as doc:
                doc["drain"] = None
                raise KeyError("boom")
        self.assertEqual(self.ledger_bytes(), mid)
        self.assertNotEqual(before, mid)
        self.assertFalse((self.state / "ledger.json.tmp").exists())

    def test_invalid_change_is_refused_before_it_is_written(self):
        ledger = self.ctx().ledger
        before = self.ledger_bytes()
        with self.assertRaises(L.LedgerInvalid):
            with ledger.transaction() as doc:
                doc["surprise"] = 1
        self.assertEqual(self.ledger_bytes(), before)

    def test_transactions_are_not_reentrant(self):
        ledger = self.ctx().ledger
        with ledger.transaction():
            with self.assertRaises(RuntimeError):
                ledger.read()
            with self.assertRaises(RuntimeError):
                with ledger.transaction():
                    pass
        ledger.read()  # the depth counter was released

    def test_lock_timeout_leaves_the_ledger_untouched(self):
        ledger = L.Ledger(self.state, lock_wait=0.2)
        before = self.ledger_bytes()
        fd = os.open(self.state / "ledger.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(L.LedgerBusy):
            with ledger.transaction():
                pass
        with self.assertRaises(L.LedgerBusy):
            ledger.read()
        self.assertEqual(self.ledger_bytes(), before)

    def test_shared_readers_do_not_block_each_other(self):
        a, b = self.ctx().ledger, self.ctx().ledger
        seen = []
        original = L.Ledger._load

        def slow_load(self):
            if not seen:
                seen.append(1)
                other = b.read()  # a second shared lock while this one is held
                seen.append(other["revision"])
            return original(self)

        with mock.patch.object(L.Ledger, "_load", slow_load):
            self.assertEqual(a.read()["revision"], 0)
        self.assertEqual(seen, [1, 0])

    def test_lock_file_must_be_a_regular_file_of_ours(self):
        (self.state / "ledger.lock").unlink()
        (self.state / "ledger.lock").symlink_to(self.dir / "elsewhere")
        with self.assertRaises(L.LedgerStorageError):
            self.ctx().ledger.read()

    def test_failed_write_keeps_the_previous_revision(self):
        before = self.ledger_bytes()
        (self.state / "ledger.json.tmp").mkdir()  # unlink fails: a storage failure that works as root too
        with self.assertRaises(L.LedgerStorageError):
            with self.ctx().ledger.transaction() as doc:
                doc["drain"] = {"since": L.now(), "reason": "x"}
        self.assertEqual(self.ledger_bytes(), before)
        (self.state / "ledger.json.tmp").rmdir()
        with self.ctx().ledger.transaction() as doc:
            doc["drain"] = {"since": L.now(), "reason": "x"}
        self.assertEqual(self.doc()["revision"], 1)

    def test_directory_fsync_failure_after_replace_is_exit_7_and_visible(self):
        with mock.patch.object(L, "fsync_dir", side_effect=OSError("injected")):
            code, out = self.inproc("drain", "start", "--reason", "x")
        self.assertEqual(code, 7, out)
        self.assertEqual((out["committed"], out["durable"]), (True, "unknown"))
        self.assertEqual(self.doc()["drain"]["reason"], "x")  # the new revision is visible


class InvalidLedgerTests(ContCase):
    def corrupt(self, text):
        (self.state / "ledger.json").write_bytes(text if isinstance(text, bytes) else text.encode())
        return self.ledger_bytes()

    def good_text(self):
        return json.dumps(self.doc())

    def cases(self):
        good = self.good_text()
        yield "truncated", good[: len(good) // 2]
        yield "duplicate key", good.replace('"revision": 0', '"revision": 0, "revision": 1', 1)
        yield "unknown key", good.replace('"revision": 0', '"revision": 0, "surprise": 1', 1)
        yield "NaN", good.replace('"revision": 0', '"revision": NaN', 1)
        yield "non-empty checkpoints", good.replace('"checkpoints": []', '"checkpoints": [1]', 1)
        yield "not an object", "[]"

    def test_every_mutating_command_refuses_and_writes_nothing(self):
        mutating = (
            ["drain", "start", "--reason", "x"], ["drain", "end"], ["reconcile"], ["collect"],
            ["cancel", "--job", "nope"],
            ["external", "register", "--job-id", "e1", "--role", "r", "--access", "read", "--session", "cli:s",
             "--owner-pid", "1"],
        )
        for label, text in self.cases():
            before = self.corrupt(text)
            for argv in mutating:
                code, out = self.cli(*argv)
                self.assertEqual(code, 4, (label, argv, out))
            code, out = self.start()
            self.assertEqual(code, 4, (label, out))
            self.assertEqual(self.ledger_bytes(), before, label)
            self.assertFalse(any((self.state / "spool").iterdir()), f"{label}: a prompt file was left behind")

    def test_status_reports_storage_invalid_with_the_reason(self):
        for label, text in self.cases():
            self.corrupt(text)
            code, out = self.cli("status")
            self.assertEqual((code, out["storage"]), (4, "invalid"), label)
            self.assertTrue(out["reason"], label)

    def test_missing_ledger_after_init_is_invalid_not_empty(self):
        (self.state / "ledger.json").unlink()
        code, out = self.cli("status")
        self.assertEqual(code, 4, out)

    def test_ledger_json_symlink_is_refused(self):
        real = self.dir / "real.json"
        real.write_bytes(self.ledger_bytes())
        (self.state / "ledger.json").unlink()
        (self.state / "ledger.json").symlink_to(real)
        code, out = self.cli("status")
        self.assertEqual((code, out["storage"]), (4, "invalid"))

    def test_nothing_auto_repairs(self):
        before = self.corrupt("{}")
        for _ in range(2):
            self.cli("status")
            self.cli("reconcile")
        self.assertEqual(self.ledger_bytes(), before)


if __name__ == "__main__":
    unittest.main()
