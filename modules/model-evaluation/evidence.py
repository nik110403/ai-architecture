"""Evidence: receipts reused from the offline reporters, matched score evidence, and safe file helpers.

Completion and verdict counts are `outcome` evidence: they describe what ran, never which model is better. A ranking or
a weight change needs `matched` evidence: every arm scored on the same task ids, by the same single grader, with at
least `min_pairs` tasks. Matching is always recomputed from the per-task score rows; a status flag in a file is ignored.
"""

import importlib.util
import json
import math
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "evaluation" / "scripts"
DEFAULT_MIN_PAIRS = 5
MAX_BYTES = 1_000_000
RANK_LABEL = "descriptive (no significance claim)"
OUTCOME_REASON = "outcome evidence (completion and verdict counts) never ranks models or moves weights"


def _reporter(name):
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


receipt_report = _reporter("receipt_report")
evaluation_report = _reporter("evaluation_report")
usage_class = receipt_report.usage_class  # reused, not redefined
latest_verdicts = evaluation_report.latest_verdicts  # reused, not redefined
USAGE_CLASSES = ("usage_known", "usage_partial", "usage_missing", "usage_excluded")


def load_receipts(paths):
    rows, _blank = receipt_report.load([str(p) for p in paths])
    return rows


def usage_class_counts(rows):
    counts = dict.fromkeys(USAGE_CLASSES, 0)
    for row in rows:
        counts[usage_class(row)] += 1
    return counts


# --- safe files ------------------------------------------------------------------------------------------------

class Refused(Exception):
    """Bad input or a refused operation (exit 2)."""


def _no_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def read_regular(path):
    """Bytes of a regular, non-symlink file of at most 1 MB."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise Refused(f"cannot open {Path(path).name}: {error.strerror}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise Refused(f"{Path(path).name} is not a regular file of at most 1 MB")
        return os.read(fd, MAX_BYTES + 1), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def strict_json(data, what):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise Refused(f"{what} is not strict JSON: {str(error)[:80]}")


def load_json_file(path, what):
    return strict_json(read_regular(path)[0], what)


def prepare_out(out):
    """The output directory, 0700, never a symlink or someone else's."""
    out = Path(out).expanduser()
    if out.is_symlink() or os.path.abspath(out) != os.path.realpath(out):
        raise Refused("--out is, or lies below, a symlink")
    if out.exists():
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Refused("--out is not a directory owned by you")
        if info.st_mode & 0o077:
            out.chmod(0o700)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(mode=0o700)
    return out


def write_private(path, data, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_EXCL if exclusive else os.O_TRUNC)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


# --- matched evidence ------------------------------------------------------------------------------------------

def _valid_row(row):
    return (isinstance(row, dict) and isinstance(row.get("task_id"), str) and isinstance(row.get("arm"), str)
            and isinstance(row.get("grader"), str) and type(row.get("score")) in (int, float)
            and math.isfinite(row["score"]) and 0 <= row["score"] <= 1)


def _insufficient(reasons, **extra):
    return {"status": "insufficient", "reasons": reasons, "ranking": None, "ranking_label": None,
            "paired_task_ids": [], **extra}


def match_check(score_rows, min_pairs=DEFAULT_MIN_PAIRS):
    """matched only when all arms share one task-id set of >= min_pairs ids, one grader, one valid score per task."""
    if not isinstance(score_rows, list) or not all(_valid_row(r) for r in score_rows):
        return _insufficient(["score rows are missing or invalid (task_id, arm, grader, score in 0..1)"])
    by_arm, graders, seen = {}, set(), set()
    for row in score_rows:
        if (row["task_id"], row["arm"]) in seen:
            return _insufficient([f"duplicate score for task {row['task_id']} and arm {row['arm']}"])
        seen.add((row["task_id"], row["arm"]))
        by_arm.setdefault(row["arm"], {})[row["task_id"]] = row["score"]
        graders.add(row["grader"])
    reasons = []
    if len(by_arm) < 2:
        reasons.append("fewer than two arms")
    if len(graders) != 1:
        reasons.append(f"scores come from {len(graders)} graders; one grader id is required")
    task_sets = {frozenset(tasks) for tasks in by_arm.values()}
    if len(task_sets) > 1:
        reasons.append("arms were not scored on the same task set")
    elif task_sets and len(next(iter(task_sets))) < min_pairs:
        reasons.append(f"fewer than min_pairs={min_pairs} shared tasks")
    if reasons:
        return _insufficient(reasons)
    tasks = sorted(next(iter(task_sets)))
    ranking = []
    for arm in by_arm:
        mean = sum(by_arm[arm][t] for t in tasks) / len(tasks)
        wins = {o: sum(by_arm[arm][t] > by_arm[o][t] for t in tasks) for o in sorted(by_arm) if o != arm}
        ranking.append({"arm": arm, "n": len(tasks), "mean": mean, "wins": wins})
    ranking.sort(key=lambda r: (-r["mean"], r["arm"]))
    return {"status": "matched", "reasons": [], "ranking": ranking, "ranking_label": RANK_LABEL,
            "paired_task_ids": tasks, "grader": next(iter(graders)), "min_pairs": min_pairs}


def evidence_verdict(doc, min_pairs=DEFAULT_MIN_PAIRS):
    """Verdict for an evidence document. A declared evidence_class other than matched vetoes; otherwise only the rows count."""
    if not isinstance(doc, dict):
        return _insufficient(["evidence is not an object"])
    declared = doc.get("evidence_class")
    if declared is not None and declared != "matched":
        return _insufficient([f"declared evidence_class {declared!r} is not 'matched'; it moves no weight"])
    if isinstance(doc.get("scores"), list):
        if doc.get("unscored"):
            return _insufficient(["unscored runs are present"])
        return match_check(doc["scores"], min_pairs)
    if isinstance(doc.get("outcomes"), list):
        return {"status": "outcome", "reasons": [OUTCOME_REASON], "ranking": None, "ranking_label": None,
                "paired_task_ids": []}
    return _insufficient(["no per-task score rows"])
