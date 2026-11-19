"""Scoreboard: counts only. Completion and verdict counts are outcome evidence and never produce a ranking."""

import evidence

NO_RANKING = ("completion and verdict counts are outcome evidence, not quality; "
              "a ranking needs matched per-task scores (--scores): same tasks, same grader, enough pairs")


def build(rows, score_doc=None):
    groups = {}
    for row in rows:
        g = groups.setdefault((row["harness"], row["role"], row["model"]),
                              dict.fromkeys(("runs", "pass", "fail", "none", *evidence.USAGE_CLASSES), 0))
        g["runs"] += 1
        g[row["verify"]] += 1
        g[evidence.usage_class(row)] += 1
    key = lambda k: tuple("" if v is None else "\x01" + v for v in k)  # noqa: E731
    arms = [{"harness": k[0], "role": k[1], "model": k[2], **groups[k]} for k in sorted(groups, key=key)]
    verdicts = evidence.latest_verdicts(rows)
    doc = {"evidence_class": "outcome", "arms": arms, "ranking": None, "ranking_reason": NO_RANKING,
           "checker_verdicts": {v: sum(1 for _, x in verdicts.values() if x == v) for v in ("pass", "fail")},
           "match": None}
    if score_doc is not None:
        verdict = evidence.evidence_verdict(score_doc)
        doc["match"] = verdict
        if verdict["status"] == "matched":
            doc["ranking"] = verdict["ranking"]
            doc["ranking_reason"] = verdict["ranking_label"]
    return doc
