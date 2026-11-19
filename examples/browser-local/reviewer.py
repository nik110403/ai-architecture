#!/usr/bin/env python3
"""Independent reviewer: reads {survey, proposal, facts} on stdin, re-surveys the URL itself, prints a verdict.

It trusts nothing on the page: page text is never read for instructions, and reasons never echo it.
Usage: reviewer.py [--pid-file FILE]   (the pid file lets a test compare pids)
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules" / "browser"))
import backends  # noqa: E402


def judge(survey, proposal, facts, seen):
    reasons = []
    if seen["dom_sha256"] != survey["dom_sha256"]:
        reasons.append("the page changed since the survey")
    if seen["takeover"]:
        reasons.append("the page needs the user (login or password field)")
    form = next((f for f in seen["forms"] if f["id"] == proposal["form_id"]), None)
    if form is None:
        reasons.append("form not on the page")
    else:
        names = {f["name"] for f in form["fields"]}
        reasons += [f"field not in the form: {k}" for k in proposal["values"] if k not in names]
        try:
            backends.same_origin_action(seen["url"], form["action"])
        except backends.Refused:
            reasons.append("form action is not same-origin")
    for key, value in proposal["values"].items():
        if facts.get(key) != value:
            reasons.append(f"value for {key} is not in the facts")
    return reasons


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid-file")
    args = ap.parse_args(argv)
    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()))
    req = json.load(sys.stdin)
    survey, proposal, facts = req["survey"], req["proposal"], req["facts"]
    try:
        seen = backends.FixtureBackend().survey(survey["url"])
    except (backends.Refused, backends.Unavailable) as e:
        print(json.dumps({"verdict": "REJECT", "reasons": [f"could not re-survey: {type(e).__name__}"],
                          "dom_sha256_seen": "", "pid": os.getpid()}))
        return 0
    reasons = judge(survey, proposal, facts, seen)
    print(json.dumps({"verdict": "REJECT" if reasons else "APPROVE", "reasons": reasons,
                      "dom_sha256_seen": seen["dom_sha256"], "pid": os.getpid()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
