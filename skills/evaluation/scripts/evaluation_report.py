#!/usr/bin/env python3
"""Join explicit receipt JSONL inputs to checker verdicts, offline and deterministically."""

import argparse
import json
import sys

import receipt_report


MAKER_ROLES = {"implementer", "design-builder"}
CHECKER_ROLES = {
    "verifier",
    "refuter",
    "tribune",
    "security-reviewer",
    "design-critic",
}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", action="append", required=True, metavar="JSONL")
    result.add_argument(
        "--since", type=receipt_report.nonnegative_integer, metavar="EPOCH"
    )
    result.add_argument("--format", choices=("json", "text"), default="text")
    return result


def base_role(role):
    return role.split("--", 1)[0] if role is not None else None


def sort_key(values):
    return tuple("" if value is None else "\x01" + value for value in values)


def window(rows, since):
    included, null_t, stale_t = [], 0, 0
    for row in rows:
        if since is not None and row["t"] is None:
            null_t += 1
        elif since is not None and row["t"] < since:
            stale_t += 1
        else:
            included.append(row)
    return included, null_t, stale_t


def latest_verdicts(rows):
    """Return charge_id -> verdict from the latest known-time checker input row.

    Iteration order is caller file order then line order. Equal timestamps replace the
    prior result, making the last input row the declared tie-breaker.
    """
    latest = {}
    for row in rows:
        if (
            base_role(row["role"]) in CHECKER_ROLES
            and row["charge_id"] is not None
            and row["verify"] in ("pass", "fail")
            and row["t"] is not None
        ):
            current = latest.get(row["charge_id"])
            if current is None or row["t"] >= current[0]:
                latest[row["charge_id"]] = (row["t"], row["verify"])
    return latest


def table(rows, roles, verdicts=None):
    groups = {}
    for row in rows:
        if base_role(row["role"]) not in roles:
            continue
        key = (row["harness"], row["role"], row["model"])
        group = groups.setdefault(key, {"runs": 0, "pass": 0, "fail": 0, "none": 0})
        group["runs"] += 1
        if verdicts is None:
            outcome = row["verify"] if row["verify"] in ("pass", "fail") else "none"
        else:
            outcome = (
                verdicts.get(row["charge_id"], (None, "none"))[1]
                if row["charge_id"]
                else "none"
            )
        group[outcome] += 1
    result = []
    for key in sorted(groups, key=sort_key):
        group = groups[key]
        row = {
            "harness": key[0],
            "role": key[1],
            "model": key[2],
            "runs": group["runs"],
        }
        if verdicts is None:
            row.update(
                {"pass": group["pass"], "fail": group["fail"], "none": group["none"]}
            )
        else:
            denominator = group["pass"] + group["fail"]
            row.update(
                {
                    "checker_pass": group["pass"],
                    "checker_fail": group["fail"],
                    "checker_none": group["none"],
                    "pass_rate": group["pass"] / denominator if denominator else None,
                }
            )
        result.append(row)
    return result


def summarize(rows, paths, blank_lines, since):
    included, null_t, stale_t = window(rows, since)
    verdicts = latest_verdicts(included)
    maker_charges = {
        row["charge_id"]
        for row in included
        if base_role(row["role"]) in MAKER_ROLES and row["charge_id"] is not None
    }
    real_checkers = [
        row
        for row in included
        if base_role(row["role"]) in CHECKER_ROLES
        and row["charge_id"] is not None
        and row["verify"] in ("pass", "fail")
    ]
    return {
        "schema_version": 1,
        "evidence": "observed" if included else "none",
        "inputs": {
            "files": len(paths),
            "nonblank_lines": len(rows),
            "blank_lines": blank_lines,
        },
        "window": {
            "since": since,
            "included": len(included),
            "excluded_null_t": null_t,
            "excluded_stale_t": stale_t,
        },
        "joins": {
            "rule": "charge_id only; latest known timestamp wins; equal timestamps use last input order",
            "unmatched_verdicts": sum(
                row["charge_id"] not in maker_charges for row in real_checkers
            ),
            "unknown_timestamp_verdicts": sum(
                row["t"] is None for row in real_checkers
            ),
        },
        "maker_verdicts": table(included, MAKER_ROLES, verdicts),
        "checker_verdicts": table(included, CHECKER_ROLES),
    }


def emit(report, output, serialized):
    if output == "json":
        print(serialized)
        return
    print(
        "evaluation-report: offline charge_id checker joins; no rankings or causal claims"
    )
    print("evidence: {evidence}".format(**report))
    print(
        "window: since={since}, included={included}, null={excluded_null_t}, stale={excluded_stale_t}".format(
            **report["window"]
        )
    )
    print(
        "joins: unmatched_verdicts={unmatched_verdicts}, unknown_timestamp_verdicts={unknown_timestamp_verdicts}".format(
            **report["joins"]
        )
    )
    for group in report["maker_verdicts"]:
        print(
            "maker {harness!r} | {role!r} | {model!r} | runs={runs} | pass={checker_pass} fail={checker_fail} none={checker_none} rate={pass_rate}".format(
                **group
            )
        )
    for group in report["checker_verdicts"]:
        print(
            "checker {harness!r} | {role!r} | {model!r} | pass={pass} fail={fail} none={none}".format(
                **group
            )
        )


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        rows, blank_lines = receipt_report.load(args.input)
        report = summarize(rows, args.input, blank_lines, args.since)
        serialized = json.dumps(report, sort_keys=True, indent=2, allow_nan=False)
    except (ValueError, OverflowError) as error:
        return receipt_report.invalid(str(error))
    emit(report, args.format, serialized)
    return 0


if __name__ == "__main__":
    sys.exit(main())
