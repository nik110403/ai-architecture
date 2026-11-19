"""R9-U04b: the Claude Code hooks (SessionStart resume, Stop usage) run as real subprocesses."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import CONT, ContCase, L  # noqa: E402
from test_receipts import CpMixin  # noqa: E402

START, USAGE = CONT / "hooks" / "session_start.py", CONT / "hooks" / "claude_usage.py"


class HookCase(CpMixin, ContCase):
    def hook(self, script, payload, env=None, raw=None):
        run_env = {k: v for k, v in os.environ.items() if k != "AI_ARCH_JOB_MARKER"}
        run_env.update(env or {})
        proc = subprocess.run([sys.executable, "-B", str(script)], input=raw if raw is not None else json.dumps(payload),
                              capture_output=True, text=True, env=run_env, cwd=self.dir, timeout=60)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""), proc.stdout)  # a hook never fails or prints a traceback
        return proc.stdout

    def start_payload(self, **over):
        return {"hook_event_name": "SessionStart", "session_id": "sid1", "source": "startup", "cwd": str(self.project),
                **over}

    def consumed_by(self):
        return self.doc()["checkpoints"][-1]["resume"]["consumed_by"]

    def doc_consumed(self, state):
        return json.loads((state / "ledger" / "ledger.json").read_text())["checkpoints"][-1]["resume"]["consumed_by"]

    def transcript(self, *entries, name="t.jsonl"):
        path = self.dir / name
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return str(path)

    def recorded(self, session="claude-code:sid1"):
        return self.doc()["sessions"].get(session)


def assistant(model="claude-x", **usage):
    base = {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 40}
    base.update(usage)
    return {"type": "assistant", "message": {"model": model, "usage": base}}


class SessionStartHook(HookCase):
    def setUp(self):
        super().setUp()
        self.write("ev.txt")
        self.cp(nxt="hook next step", session=None)

    def test_injects_the_next_once_as_additional_context(self):
        out = json.loads(self.hook(START, self.start_payload()))
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "SessionStart")
        self.assertIn("hook next step", spec["additionalContext"])
        self.assertIn("<untrusted-file-content>", spec["additionalContext"])
        self.assertEqual(self.consumed_by(), "claude-code:sid1")
        other = json.loads(self.hook(START, self.start_payload(session_id="sid2")))["hookSpecificOutput"]
        self.assertNotIn("hook next step", other["additionalContext"])  # told who has it, never the Next

    def test_compact_source_does_not_consume(self):
        out = json.loads(self.hook(START, self.start_payload(source="compact")))
        self.assertIn("pending for the next session", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.consumed_by())

    def test_dispatched_worker_and_subagent_consume_nothing(self):
        self.assertEqual(self.hook(START, self.start_payload(), env={"AI_ARCH_JOB_MARKER": "x"}), "")
        self.assertEqual(self.hook(START, self.start_payload(agent_id="sub1")), "")
        self.assertIsNone(self.consumed_by())

    def test_no_ledger_wrong_root_bad_payload_and_unknown_source_print_nothing(self):
        self.assertEqual(self.hook(START, self.start_payload(cwd=str(self.dir))), "")  # no ledger up the tree
        copy = self.dir / "copy"
        copy.mkdir()
        subprocess.run(["cp", "-a", str(self.project / ".ai-continuity"), str(copy)], check=True)
        self.assertEqual(self.hook(START, self.start_payload(cwd=str(copy))), "")  # recorded project_root differs
        self.assertEqual(self.hook(START, None, raw="not json"), "")
        self.assertEqual(self.hook(START, self.start_payload(source="fork")), "")
        self.assertEqual(self.hook(START, self.start_payload(session_id=["x"])), "")
        self.assertEqual(self.hook(START, self.start_payload(session_id="bad id!")), "")
        self.assertIsNone(self.consumed_by())

    def test_symlinked_ledger_directory_is_refused(self):
        link = self.dir / "linked"
        link.mkdir()
        os.symlink(self.project / ".ai-continuity", link / ".ai-continuity")
        self.assertEqual(self.hook(START, self.start_payload(cwd=str(link))), "")
        self.assertIsNone(self.consumed_by())

    def test_symlinked_state_directory_inside_the_project_is_refused(self):
        moved = self.dir / "moved-state"
        os.rename(self.project / ".ai-continuity", moved)
        os.symlink(moved, self.project / ".ai-continuity")  # project_root still matches: only no_symlinks refuses
        self.assertEqual(self.hook(START, self.start_payload()), "")
        self.assertIsNone(self.doc_consumed(moved))

    def test_walks_up_from_a_subdirectory(self):
        sub = self.project / "a" / "b"
        sub.mkdir(parents=True)
        out = json.loads(self.hook(START, self.start_payload(cwd=str(sub))))
        self.assertIn("hook next step", out["hookSpecificOutput"]["additionalContext"])

    def test_corrupt_ledger_prints_nothing(self):
        (self.state / "ledger.json").write_text("{")
        self.assertEqual(self.hook(START, self.start_payload()), "")


class UsageHook(HookCase):
    def stop(self, transcript, **over):
        payload = {"hook_event_name": "Stop", "session_id": "sid1", "cwd": str(self.project),
                   "transcript_path": transcript, **over}
        return self.hook(USAGE, payload)

    def test_records_the_last_assistant_usage_and_model(self):
        path = self.transcript(assistant(input_tokens=1), {"type": "user", "message": {}}, assistant(model="m2"))
        self.assertEqual(self.stop(path), "")
        rec = self.recorded()
        self.assertEqual((rec["usage_tokens"], rec["model"], rec["source"]), (100, "m2", "claude-transcript"))

    def test_unparseable_or_incomplete_usage_records_unknown_never_a_guess(self):
        self.usage(999, "claude-code:sid1")
        cases = {"missing field": [assistant(), {"type": "assistant", "message": {"usage": {"input_tokens": 5}}}],
                 "non-integer": [assistant(output_tokens="40")], "bool": [assistant(output_tokens=True)],
                 "negative": [assistant(output_tokens=-1)], "no assistant": [{"type": "user", "message": {}}],
                 "no usage": [{"type": "assistant", "message": {"model": "m"}}]}
        for name, entries in cases.items():
            self.usage(999, "claude-code:sid1")
            self.stop(self.transcript(*entries))
            self.assertIsNone(self.recorded()["usage_tokens"], name)
        self.usage(999, "claude-code:sid1")
        bad = self.dir / "bad.jsonl"
        bad.write_text('{"type": "assistant", "message": {"usage": {"inp')
        self.stop(str(bad))
        self.assertIsNone(self.recorded()["usage_tokens"])
        self.usage(999, "claude-code:sid1")
        self.stop(str(self.dir / "missing.jsonl"))
        self.assertIsNone(self.recorded()["usage_tokens"])
        self.stop(None)
        self.assertIsNone(self.recorded()["usage_tokens"])

    def test_only_the_last_256_kib_are_read_and_a_cut_first_line_is_dropped(self):
        path = self.dir / "big.jsonl"
        junk = json.dumps({"type": "user", "message": {"content": "x" * 1000}}) + "\n"
        with open(path, "w") as handle:
            handle.write(json.dumps(assistant(input_tokens=1000)) + "\n")  # far outside the tail
            handle.write(junk * 400)
            handle.write(json.dumps(assistant(model="late")) + "\n")
        self.assertGreater(path.stat().st_size, 256 * 1024)
        self.stop(str(path))
        self.assertEqual(self.recorded()["usage_tokens"], 100)
        only_old = self.dir / "old.jsonl"
        with open(only_old, "w") as handle:
            handle.write(json.dumps(assistant(input_tokens=1000)) + "\n" + junk * 400)
        self.stop(str(only_old))
        self.assertIsNone(self.recorded()["usage_tokens"])  # the old entry is outside the tail: unknown, not stale

    def test_symlinked_transcript_is_not_followed(self):
        real = self.transcript(assistant())
        link = self.dir / "link.jsonl"
        os.symlink(real, link)
        self.stop(str(link))
        self.assertIsNone(self.recorded()["usage_tokens"])

    def test_worker_subagent_and_no_ledger_record_nothing(self):
        path = self.transcript(assistant())
        payload = {"session_id": "sid1", "cwd": str(self.project), "transcript_path": path}
        self.hook(USAGE, payload, env={"AI_ARCH_JOB_MARKER": "x"})
        self.hook(USAGE, dict(payload, agent_id="sub"))
        self.hook(USAGE, dict(payload, cwd=str(self.dir)))
        self.hook(USAGE, None, raw="{")
        self.assertEqual(self.doc()["sessions"], {})

    def test_recorded_usage_feeds_the_phase(self):
        (self.state / "config.json").write_text(json.dumps({"context_limits": {"claude-x": 1000}, "handoff_reserve_tokens": 0}))
        self.stop(self.transcript(assistant(input_tokens=950)))
        code, out = self.cli("status", extra=["--session", "claude-code:sid1"])
        self.assertEqual((out["phase"], out["usage_tokens"], out["limit_tokens"]), ("HANDOFF", 1040, 1000))


if __name__ == "__main__":
    unittest.main()
