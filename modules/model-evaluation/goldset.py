"""Deterministic goldset scoring: exact string match against operator-supplied gold answers."""

GRADER = "goldset-exact-v1"


def score_answers(gold, answers):
    """Score rows [{task_id, arm, answer}] against {task_id: answer}; sorted by (arm, task_id), order independent."""
    out = []
    for row in answers:
        task, arm, answer = row.get("task_id"), row.get("arm"), row.get("answer")
        if not isinstance(task, str) or not isinstance(arm, str) or task not in gold:
            raise ValueError(f"answer row has no gold task: {task!r}")
        out.append({"task_id": task, "arm": arm, "grader": GRADER, "score": 1 if answer == gold[task] else 0})
    return sorted(out, key=lambda r: (r["arm"], r["task_id"]))


def arms_of(answers):
    counts = {}
    for row in answers:
        counts[row["arm"]] = counts.get(row["arm"], 0) + 1
    return dict(sorted(counts.items()))
