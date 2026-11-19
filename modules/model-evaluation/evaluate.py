#!/usr/bin/env python3
"""Model evaluation: scoreboard, bake-off, goldset and guarded reweighting (stdlib only; no network).

  evaluate.py scoreboard --receipts JSONL... [--scores EVIDENCE]
  evaluate.py goldset score --gold FILE --answers FILE      |  evaluate.py goldset arms --answers FILE
  evaluate.py bakeoff run --role R --arm A... --task T... --gold FILE --out DIR [--timeout S] [--allow-spend]
                          --runner-cmd /absolute/runner [args...]
  evaluate.py reweight --pool P --evidence E --out DIR [--min-samples N --floor F --cap C]
                       (--dry-run | --apply PLAN | --rollback ID) [--quota-config FILE]

Every command prints one JSON object. Exit: 0 ok, 2 refused or bad usage, 4 the pool, evidence or plan no longer
agree (nothing changed). A ranking exists only on matched evidence (same tasks, same grader, enough pairs); completion
and verdict counts never rank and never move a weight. `bakeoff run` starts a process per task and arm: it needs
--allow-spend and an operator-supplied runner.
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bakeoff  # noqa: E402
import evidence  # noqa: E402
import goldset  # noqa: E402
import reweight  # noqa: E402
import scoreboard  # noqa: E402
from evidence import Refused  # noqa: E402


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scoreboard")
    s.add_argument("--receipts", action="append", required=True)
    s.add_argument("--scores")
    g = sub.add_parser("goldset").add_subparsers(dest="action", required=True)
    s = g.add_parser("score")
    s.add_argument("--gold", required=True)
    s.add_argument("--answers", required=True)
    s = g.add_parser("arms")
    s.add_argument("--answers", required=True)
    b = sub.add_parser("bakeoff").add_subparsers(dest="action", required=True)
    s = b.add_parser("run")
    s.add_argument("--role", required=True)
    s.add_argument("--arm", action="append", required=True)
    s.add_argument("--task", action="append", required=True)
    s.add_argument("--gold", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--timeout", type=float, default=60.0)
    s.add_argument("--allow-spend", action="store_true")
    s.add_argument("--runner-cmd", nargs=argparse.REMAINDER, default=[])
    s = sub.add_parser("reweight")
    s.add_argument("--pool", required=True)
    s.add_argument("--evidence")
    s.add_argument("--out", required=True)
    s.add_argument("--min-samples", type=int, default=reweight.DEFAULTS["min_samples"])
    s.add_argument("--floor", type=float, default=reweight.DEFAULTS["floor"])
    s.add_argument("--cap", type=float, default=reweight.DEFAULTS["cap"])
    s.add_argument("--quota-config")
    mode = s.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", metavar="PLAN")
    mode.add_argument("--rollback", metavar="ID")
    return p


def cmd_scoreboard(args):
    rows = evidence.load_receipts(args.receipts)
    scores = evidence.load_json_file(args.scores, "scores") if args.scores else None
    return 0, scoreboard.build(rows, scores)


def cmd_goldset(args):
    if args.action == "arms":
        return 0, {"arms": goldset.arms_of(evidence.load_json_file(args.answers, "answers"))}
    gold = evidence.load_json_file(args.gold, "gold")
    if not isinstance(gold, dict):
        raise Refused("gold must be an object {task_id: answer}")
    try:
        return 0, {"grader": goldset.GRADER, "scores": goldset.score_answers(gold, evidence.load_json_file(args.answers, "answers"))}
    except (ValueError, TypeError, AttributeError) as error:
        raise Refused(str(error))


def cmd_bakeoff(args):
    argv = args.runner_cmd
    if not args.allow_spend:
        raise Refused("bakeoff run starts a process per task and arm: pass --allow-spend")
    if not argv or not os.path.isabs(argv[0]) or not os.access(argv[0], os.X_OK):
        raise Refused("--runner-cmd must start with the absolute path of an executable runner")
    if args.timeout <= 0 or len(set(args.arm)) != len(args.arm) or len(set(args.task)) != len(args.task):
        raise Refused("--timeout must be > 0; arms and tasks must be unique")
    gold = evidence.load_json_file(args.gold, "gold")
    if not isinstance(gold, dict) or any(t not in gold for t in args.task):
        raise Refused("gold must be an object with an answer for every --task")
    out = evidence.prepare_out(args.out)
    doc = bakeoff.run_plan(args.task, args.arm, gold, argv, args.timeout, args.role)
    evidence.write_private(out / "evidence.json", json.dumps(doc, indent=2, sort_keys=True).encode() + b"\n")
    return 0, {"evidence_file": "evidence.json", "runs": len(args.task) * len(args.arm), "scored": len(doc["scores"]),
               "unscored": doc["unscored"], "match": evidence.evidence_verdict(doc)}


def quota_states(path):
    from modules.observability import quota
    config = quota.load_config(path)
    return {p["provider"]: p["state"] for p in quota.status(config, Path(path).resolve().parent)["providers"]}


def cmd_reweight(args):
    out = evidence.prepare_out(args.out)
    params = {"min_samples": args.min_samples, "floor": args.floor, "cap": args.cap}
    if args.rollback:
        return 0, reweight.rollback(args.pool, out, args.rollback)
    if not args.evidence:
        raise Refused("--evidence is required")
    if args.apply:
        return 0, reweight.apply(args.pool, args.evidence, out, args.apply)
    plan, rel = reweight.dry_run(args.pool, args.evidence, out, params)
    doc = {"plan_id": plan["plan_sha256"][:16], "plan_file": rel, "changed": plan["changed"], "plan": plan}
    if args.quota_config:  # reported only: quota is gate-time state and never reaches the plan or the pool
        doc["quota"] = quota_states(args.quota_config)
    return 0, doc


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    try:
        code, doc = {"scoreboard": cmd_scoreboard, "goldset": cmd_goldset, "bakeoff": cmd_bakeoff,
                     "reweight": cmd_reweight}[args.command](args)
    except reweight.Conflict as error:
        print(json.dumps({"error": str(error), "changed": False}))
        return 4
    except (Refused, ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {str(error)[:200]}"}))
        return 2
    print(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
