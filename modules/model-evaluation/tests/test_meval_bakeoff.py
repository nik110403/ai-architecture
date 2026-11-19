"""A14 (no launch without --allow-spend; fake runners only), AM9 (bake-off), goldset determinism."""

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meval_helpers import A, B, Isolated  # noqa: E402
import goldset  # noqa: E402

GOLD = {f"t{i}": f"answer-{i}" for i in range(6)}
RUNNER = """#!{python}
import json, os, sys, time
task, arm = os.environ["BAKEOFF_TASK"], os.environ["BAKEOFF_ARM"]
{body}
"""
GOOD = 'print(json.dumps({"answer": "answer-" + task[1:] if not (arm.endswith("b") and task == "t0") else "wrong"}))'


class Bakeoff(Isolated):
    def setUp(self):
        super().setUp()
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.gold = self.write_json("gold.json", GOLD)

    def runner(self, body=GOOD, name="runner"):
        path = self.bin / name
        path.write_text(RUNNER.format(python=sys.executable, body=body))
        path.chmod(0o755)
        return path

    def run_bakeoff(self, runner, *extra, tasks=6, spend=True, timeout="5"):
        argv = ["bakeoff", "run", "--role", "builder", "--arm", A, "--arm", B, "--gold", self.gold, "--out", self.out,
                "--timeout", timeout]
        for i in range(tasks):
            argv += ["--task", f"t{i}"]
        if spend:
            argv.append("--allow-spend")
        argv += list(extra) + ["--runner-cmd", str(runner)]
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            return self.cli(*argv)

    def test_no_real_model_cli_is_reachable(self):
        for name in ("claude", "codex", "opencode", "gemini"):
            self.assertIsNone(shutil.which(name, path=str(self.bin)))

    def test_refused_without_allow_spend_and_starts_nothing(self):
        real = subprocess.Popen
        with mock.patch.object(subprocess, "Popen", wraps=real) as popen:
            code, out = self.run_bakeoff(self.runner(), spend=False)
        self.assertEqual(code, 2)
        self.assertEqual(popen.call_count, 0)
        self.assertFalse((self.out / "evidence.json").exists())

    def test_allow_spend_with_fake_runner_ingests_results(self):
        real = subprocess.Popen
        with mock.patch.object(subprocess, "Popen", wraps=real) as popen:
            code, out = self.run_bakeoff(self.runner())
        self.assertEqual(code, 0, out)
        self.assertEqual(popen.call_count, 12)
        ev = json.loads((self.out / "evidence.json").read_text())
        self.assertEqual(len(ev["scores"]), 12)
        self.assertEqual(ev["unscored"], [])
        self.assertEqual({r["grader"] for r in ev["scores"]}, {goldset.GRADER})
        self.assertEqual(out["match"]["status"], "matched")
        top = out["match"]["ranking"][0]
        self.assertEqual((top["arm"], top["n"]), (A, 6))
        self.assertEqual(out["match"]["ranking_label"], "descriptive (no significance claim)")
        self.assertNotIn("cost", json.dumps(out).lower())  # no usage or cost term enters a ranking

    def test_failed_missing_and_hanging_runs_are_unscored_so_evidence_is_insufficient(self):
        cases = {
            "exit": 'sys.exit(3)',
            "silent": 'pass',
            "hang": 'time.sleep(60)',
            "garbage": 'print("not json")',
        }
        for name, body in cases.items():
            started = time.time()
            code, out = self.run_bakeoff(self.runner(body, name), timeout="1", tasks=1)
            self.assertEqual(code, 0, (name, out))
            self.assertEqual(out["match"]["status"], "insufficient", name)
            self.assertEqual(len(out["unscored"]), 2, name)
            self.assertLess(time.time() - started, 15, name)
            shutil.rmtree(self.out)
            self.out.mkdir()

    def test_runner_cmd_must_be_absolute_and_present(self):
        self.assertEqual(self.run_bakeoff("relative-runner")[0], 2)
        argv = ["bakeoff", "run", "--role", "builder", "--arm", A, "--arm", B, "--gold", self.gold, "--out", self.out,
                "--task", "t0", "--allow-spend"]
        self.assertEqual(self.cli(*argv)[0], 2)  # --runner-cmd is required

    def test_goldset_scoring_is_deterministic(self):
        answers = [{"task_id": f"t{i}", "arm": A, "answer": f"answer-{i}" if i != 2 else "x"} for i in range(6)]
        first = goldset.score_answers(GOLD, answers)
        self.assertEqual(first, goldset.score_answers(GOLD, list(reversed(answers))))
        self.assertEqual(sum(r["score"] for r in first), 5)
        self.assertEqual({r["grader"] for r in first}, {goldset.GRADER})
        path = self.write_json("answers.json", answers)
        code, out = self.cli("goldset", "score", "--gold", self.gold, "--answers", path)
        self.assertEqual(code, 0)
        self.assertEqual(out["scores"], first)
        code, out = self.cli("goldset", "arms", "--answers", path)
        self.assertEqual(out["arms"], {A: 6})


if __name__ == "__main__":
    unittest.main()
