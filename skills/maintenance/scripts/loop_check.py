#!/usr/bin/env python3
"""Report declared loop wiring from two explicit JSON snapshots.

This reads only --registry and --adapters. It neither probes a host nor proves
that a scheduler, service, or hook is healthy, fresh, or running.
"""

import argparse
import json
import sys
from pathlib import Path

COUNTS = ("checked", "info", "drift", "unavailable", "skipped")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--registry", required=True, help="explicit loop registry JSON")
    result.add_argument(
        "--adapters", required=True, help="explicit adapter inventory JSON"
    )
    result.add_argument("--format", choices=("text", "json"), default="text")
    result.add_argument("--strict", action="store_true", help="exit 1 only for drift")
    return result


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def controlled(message):
    print(f"loop-check: invalid input: {message}", file=sys.stderr)
    return 2


def load(path, label):
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=duplicate_keys
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise ValueError(f"{label}: {error}") from error


def require_object(value, label, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{label} must contain exactly: {', '.join(fields)}")


def require_version(value, label):
    if type(value) is not int or value != 1:
        raise ValueError(f"{label} schema_version must be integer 1")


def require_text(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")


def registry(data):
    require_object(data, "registry", ("schema_version", "loops"))
    require_version(data["schema_version"], "registry")
    if not isinstance(data["loops"], list):
        raise ValueError("registry loops must be an array")
    identifiers, rows = set(), []
    for index, row in enumerate(data["loops"]):
        label = f"registry loops[{index}]"
        require_object(row, label, ("id", "adapter", "status"))
        require_text(row["id"], f"{label}.id")
        require_text(row["adapter"], f"{label}.adapter")
        if row["id"] in identifiers:
            raise ValueError(f"duplicate registry loop id: {row['id']}")
        if row["status"] not in ("live", "paused", "retired"):
            raise ValueError(f"{label}.status is invalid")
        identifiers.add(row["id"])
        rows.append(row)
    return rows


def adapters(data):
    require_object(data, "adapters", ("schema_version", "adapters"))
    require_version(data["schema_version"], "adapters")
    if not isinstance(data["adapters"], list):
        raise ValueError("adapters adapters must be an array")
    identifiers, rows = set(), {}
    for index, row in enumerate(data["adapters"]):
        label = f"adapters adapters[{index}]"
        require_object(row, label, ("id", "state"))
        require_text(row["id"], f"{label}.id")
        if row["id"] in identifiers:
            raise ValueError(f"duplicate adapter id: {row['id']}")
        if row["state"] not in ("active", "inactive", "missing", "unavailable"):
            raise ValueError(f"{label}.state is invalid")
        identifiers.add(row["id"])
        rows[row["id"]] = row["state"]
    return rows


def report(loop_rows, adapter_rows):
    results = []
    for row in sorted(loop_rows, key=lambda item: item["id"]):
        state = adapter_rows.get(row["adapter"])
        if row["status"] != "live":
            outcome = "skipped"
        elif state == "active":
            outcome = "checked"
        elif state == "inactive":
            outcome = "info"
        elif state == "missing":
            outcome = "drift"
        else:
            outcome = "unavailable"
        results.append({"id": row["id"], "adapter": row["adapter"], "outcome": outcome})
    counts = {name: sum(row["outcome"] == name for row in results) for name in COUNTS}
    return {
        "complete": counts["unavailable"] == 0,
        "counts": counts,
        "results": results,
    }


def emit(value, output):
    if output == "json":
        print(json.dumps(value, sort_keys=True, indent=2))
        return
    print("loop-check: snapshot report")
    print(f"complete: {str(value['complete']).lower()}")
    for name in COUNTS:
        print(f"{name}: {value['counts'][name]}")
    for row in value["results"]:
        print("{id} | {adapter} | {outcome}".format(**row))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        value = report(
            registry(load(args.registry, "registry")),
            adapters(load(args.adapters, "adapters")),
        )
    except ValueError as error:
        return controlled(str(error))
    emit(value, args.format)
    return 1 if args.strict and value["counts"]["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
