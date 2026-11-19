"""Bake-off: a plan of task ids x arms run through an operator-supplied runner, scored by the goldset grader.

Nothing starts without --allow-spend. The runner is an absolute path to an executable the operator provides; it
receives BAKEOFF_TASK and BAKEOFF_ARM in its environment and prints {"answer": "..."}. A runner that exits non-zero,
prints nothing usable or exceeds the timeout leaves that run unscored, and unscored runs make the evidence insufficient.
No usage or cost enters a score or a ranking.
"""

import json
import os
import signal
import subprocess

import evidence
import goldset

MAX_OUT = 65536


def run_one(argv, task, arm, timeout):
    env = dict(os.environ, BAKEOFF_TASK=task, BAKEOFF_ARM=arm)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                            start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return None, "timeout"
    if proc.returncode != 0:
        return None, f"runner exit {proc.returncode}"
    if len(out) > MAX_OUT:
        return None, "runner output too large"
    try:
        doc = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "runner output is not JSON"
    if not isinstance(doc, dict) or not isinstance(doc.get("answer"), str):
        return None, "runner output has no answer"
    return doc["answer"], None


def run_plan(tasks, arms, gold, argv, timeout, role):
    scores, unscored = [], []
    for task in tasks:
        for arm in arms:
            answer, problem = run_one(argv, task, arm, timeout)
            if problem:
                unscored.append({"task_id": task, "arm": arm, "reason": problem})
            else:
                scores += goldset.score_answers(gold, [{"task_id": task, "arm": arm, "answer": answer}])
    return {"version": 1, "role": role, "scores": scores, "unscored": unscored}
