#!/usr/bin/env python3
"""Summarize strict v1 receipt JSONL from explicit input paths only.

This report is offline accounting, not a model evaluation, scoreboard, telemetry
collector, or checker join. It reads the supplied paths once in caller order and
writes a deterministic report to stdout. Invalid input produces no report.
"""

import argparse
import json
import math
import sys
from pathlib import Path


FIELDS = (
    "schema_version",
    "t",
    "task_id",
    "source",
    "harness",
    "role",
    "model",
    "charge_id",
    "verify",
    "disposition",
    "tok_source",
    "in_tok",
    "out_tok",
    "reasoning_tok",
    "cache_read_tok",
    "cache_write_tok",
    "cost_usd",
    "dur_ms",
)
IDENTITIES = ("task_id", "source", "harness", "role", "model", "charge_id")
TOKENS = ("in_tok", "out_tok", "reasoning_tok", "cache_read_tok", "cache_write_tok")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", action="append", required=True, metavar="JSONL")
    result.add_argument("--since", type=nonnegative_integer, metavar="EPOCH")
    result.add_argument("--format", choices=("json", "text"), default="text")
    return result


def nonnegative_integer(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from error
    if str(parsed) != value or parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid(message):
    print(f"receipt-report: invalid input: {message}", file=sys.stderr)
    return 2


def row_error(ordinal, line, message):
    raise ValueError(f"input {ordinal} line {line}: {message}")


def require_nullable_string(value, name, ordinal, line):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        row_error(ordinal, line, f"{name} must be null or a nonblank string")


def require_nullable_int(value, name, ordinal, line):
    if value is not None and (type(value) is not int or value < 0):
        row_error(ordinal, line, f"{name} must be null or a nonnegative integer")


def require_nullable_number(value, name, ordinal, line):
    if value is not None and (
        type(value) not in (int, float)
        or (type(value) is float and not math.isfinite(value))
        or value < 0
    ):
        row_error(ordinal, line, f"{name} must be null or a finite nonnegative number")


def normalize(value, ordinal, line):
    if not isinstance(value, dict):
        row_error(ordinal, line, "must be one JSON object")
    unknown = set(value) - set(FIELDS)
    if unknown:
        row_error(ordinal, line, f"unknown field: {sorted(unknown)[0]}")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        row_error(ordinal, line, "schema_version must be integer 1")
    row = {field: value.get(field) for field in FIELDS}
    for name in IDENTITIES:
        require_nullable_string(row[name], name, ordinal, line)
    for name in ("t", *TOKENS):
        require_nullable_int(row[name], name, ordinal, line)
    for name in ("cost_usd", "dur_ms"):
        require_nullable_number(row[name], name, ordinal, line)
    if "verify" not in value:
        row["verify"] = "none"
    elif row["verify"] not in ("pass", "fail", "none"):
        row_error(ordinal, line, "verify must be pass, fail, or none")
    if row["disposition"] not in (None, "merged", "discarded", "reworked", "abandoned"):
        row_error(ordinal, line, "disposition is invalid")
    if row["tok_source"] not in (None, "agent", "main-fallback"):
        row_error(ordinal, line, "tok_source is invalid")
    return row


def load(paths):
    rows, blank_lines = [], 0
    for ordinal, path in enumerate(paths, 1):
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                for line_number, raw in enumerate(handle, 1):
                    if not raw.strip():
                        blank_lines += 1
                        continue
                    try:
                        value = json.loads(raw, object_pairs_hook=duplicate_keys)
                    except (json.JSONDecodeError, RecursionError, ValueError) as error:
                        row_error(ordinal, line_number, str(error))
                    rows.append(normalize(value, ordinal, line_number))
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"input {ordinal}: {error}") from error
    return rows, blank_lines


def usage_class(row):
    if row["tok_source"] == "main-fallback":
        return "usage_excluded"
    known = sum(row[name] is not None for name in TOKENS)
    if known == 0:
        return "usage_missing"
    return "usage_known" if known == len(TOKENS) else "usage_partial"


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    lower, upper = ordered[middle - 1 : middle + 1]
    if type(lower) is int and type(upper) is int:
        total = lower + upper
        return total // 2 if total % 2 == 0 else total / 2
    # Inputs are nonnegative: the difference cannot overflow for finite floats.
    return lower + (upper - lower) / 2


def summarize(rows, paths, blank_lines, since):
    seen, deduped, duplicates = set(), [], 0
    for row in rows:
        if row["task_id"] is not None:
            key = (row["task_id"], row["source"])
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
        deduped.append(row)
    receipts, null_t, stale_t = [], 0, 0
    for row in deduped:
        if since is not None and row["t"] is None:
            null_t += 1
        elif since is not None and row["t"] < since:
            stale_t += 1
        else:
            receipts.append(row)
    grouped = {}
    for row in receipts:
        grouped.setdefault((row["harness"], row["role"], row["model"]), []).append(row)
    groups = []
    key_sort = lambda item: tuple(
        "" if value is None else "\x01" + value for value in item
    )
    for key in sorted(grouped, key=key_sort):
        group = grouped[key]
        classes = {
            name: 0
            for name in (
                "usage_known",
                "usage_partial",
                "usage_missing",
                "usage_excluded",
            )
        }
        observed = []
        for row in group:
            kind = usage_class(row)
            classes[kind] += 1
            if kind in ("usage_known", "usage_partial"):
                observed.append(
                    sum(row[name] for name in TOKENS if row[name] is not None)
                )
        costs = [row["cost_usd"] for row in group if row["cost_usd"] is not None]
        durations = [row["dur_ms"] for row in group if row["dur_ms"] is not None]
        label = (
            "INCOMPLETE"
            if classes["usage_partial"] or classes["usage_excluded"]
            else "OBSERVED"
        )
        if not observed:
            label = "MISSING"
        groups.append(
            {
                "harness": key[0],
                "role": key[1],
                "model": key[2],
                "runs": len(group),
                **classes,
                "usage_label": label,
                "observed_tokens_total": sum(observed) if observed else None,
                "observed_tokens_median": median(observed),
                "cost_usd_sum": sum(costs) if costs else None,
                "cost_usd_measured": len(costs),
                "cost_usd_missing": len(group) - len(costs),
                "dur_ms_median": median(durations),
                "dur_ms_measured": len(durations),
                "dur_ms_missing": len(group) - len(durations),
            }
        )
    return {
        "schema_version": 1,
        "inputs": {
            "files": len(paths),
            "nonblank_lines": len(rows),
            "blank_lines": blank_lines,
        },
        "dedup": {"duplicates": duplicates, "receipts_after_dedup": len(deduped)},
        "window": {
            "since": since,
            "included": len(receipts),
            "excluded_null_t": null_t,
            "excluded_stale_t": stale_t,
        },
        "receipts": receipts,
        "groups": groups,
    }


def emit(report, output, serialized):
    if output == "json":
        print(serialized)
        return
    print("receipt-report: offline accounting")
    print(
        "inputs: {files} files, {nonblank_lines} rows, {blank_lines} blank".format(
            **report["inputs"]
        )
    )
    print(
        "dedup: {duplicates} duplicates, {receipts_after_dedup} receipts".format(
            **report["dedup"]
        )
    )
    print(
        "window: since={since}, included={included}, null={excluded_null_t}, stale={excluded_stale_t}".format(
            **report["window"]
        )
    )
    for group in report["groups"]:
        print(
            "{harness!r} | {role!r} | {model!r} | runs={runs} | {usage_label} | observed_tokens={observed_tokens_total}".format(
                **group
            )
        )


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        rows, blank_lines = load(args.input)
    except ValueError as error:
        return invalid(str(error))
    try:
        report = summarize(rows, args.input, blank_lines, args.since)
    except OverflowError:
        return invalid("numeric range: aggregate exceeds supported arithmetic range")
    try:
        # Preflight both formats before emitting anything; never print partial reports.
        serialized = json.dumps(report, sort_keys=True, indent=2, allow_nan=False)
    except (ValueError, OverflowError):
        return invalid("numeric range: report exceeds JSON serializer range")
    emit(report, args.format, serialized)
    return 0


if __name__ == "__main__":
    sys.exit(main())
