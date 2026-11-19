"""Shared helpers for the model-evaluation tests: isolated HOME/XDG, in-process CLI, hook runners, fixtures."""

import contextlib
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
HOOK = ROOT / "plugins" / "gates" / "hooks" / "rotation" / "model-rotate.py"
BUN_CHECK = HERE / "meval_bun_check.ts"
for p in (str(MOD), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import evaluate  # noqa: E402

A, B, P, OFF, NEW = ("provider/model-a", "provider/model-b", "provider/model-p", "provider/model-off", "provider/model-new")


def pool_doc():
    return {"version": 1, "mode": "on", "independent_roles": ["reviewer"], "roles": {
        "builder": [{"model": A, "weight": 30}, {"model": B, "weight": 10},
                    {"model": P, "weight": 20, "pinned": True}, {"model": OFF, "weight": 5, "enabled": False},
                    {"model": NEW, "weight": 5}],
        "reviewer": [{"model": A, "weight": 1}, {"model": B, "weight": 1}]}}


def scores(arm_scores, grader="grader-1", tasks=6, role="builder"):
    """Evidence document: arm_scores = {arm: [score per task]}."""
    rows = [{"task_id": f"t{i}", "arm": arm, "grader": grader, "score": vals[i]}
            for arm, vals in arm_scores.items() for i in range(min(tasks, len(vals)))]
    return {"version": 1, "role": role, "scores": rows}


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


class Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.t = Path(self._tmp.name).resolve()
        for name in ("home", "state", "config", "work", "out"):
            (self.t / name).mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.t / "home"), "XDG_STATE_HOME": str(self.t / "state"),
                                             "XDG_CONFIG_HOME": str(self.t / "config")})
        patch.start()
        self.addCleanup(patch.stop)
        self.out = self.t / "out"

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = evaluate.main([str(a) for a in argv])
        text = buf.getvalue()
        return code, (json.loads(text) if text.strip() else None)

    def write_json(self, name, doc):
        path = self.t / name
        path.write_text(json.dumps(doc, indent=2))
        return path

    def pool(self, doc=None, name="pool.json"):
        path = self.write_json(name, doc or pool_doc())
        path.chmod(0o640)
        return path

    def evidence(self, doc=None, name="evidence.json"):
        # pinned (P) and disabled (OFF) arms carry strong evidence on purpose: they must still keep their weight
        default = scores({A: [.9] * 6, B: [.3] * 6, P: [.5] * 6, OFF: [.95] * 6})
        return self.write_json(name, doc or default)

    def reweight(self, pool, evidence, *extra):
        return self.cli("reweight", "--pool", pool, "--evidence", evidence, "--out", self.out, *extra)

    def dry_plan(self, pool, evidence, *extra):
        code, out = self.reweight(pool, evidence, "--dry-run", *extra)
        self.assertEqual(code, 0, out)
        return self.out / out["plan_file"], json.loads((self.out / out["plan_file"]).read_text())


def hook_pick(pool, prompt, seed, role="builder"):
    """Run the Claude Code hook (seeded, test mode) and return the model it chose, or None."""
    cwd = Path(pool).parent / "hookcwd"
    cwd.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"], "ROTATION_POOL": str(pool),
           "ROTATION_TEST": "1", "ROTATION_SEED": str(seed)}
    payload = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Task",
               "tool_input": {"subagent_type": role, "description": "d", "prompt": prompt, "model": "default-model"}}
    res = subprocess.run([sys.executable, "-B", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                         env=env, cwd=cwd, timeout=20)
    if not res.stdout.strip():
        return None
    return json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["model"]


def expected_pick(arms, seed):
    """The documented algorithm: random.Random('rotation:'+seed).randrange(total), walked cumulatively."""
    total = sum(w for _, w in arms)
    r = random.Random("rotation:" + str(seed)).randrange(total)
    for model, w in arms:
        if r < w:
            return model
        r -= w
    return arms[-1][0]
