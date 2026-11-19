#!/usr/bin/env python3
"""Report and validate the declared, offline capability registry."""

import argparse
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "capabilities" / "registry.json"
STATES = {"available", "partial", "planned", "unavailable", "unsupported"}
AVAILABILITY = {"unknown", "declared"}
EVIDENCE_CLASSES = {"none", "artifact"}
IDS = {f"{number:02d}" for number in range(1, 73)}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
REQUIREMENT_KINDS = {"command", "python-module"}
COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def fail(message):
    raise ValueError(message)


def load_registry(path):
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys
        )
    except json.JSONDecodeError as error:
        fail(f"invalid JSON: {error.msg}")
    except OSError as error:
        fail(f"cannot read registry: {error}")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def evidence_path(root, raw):
    if not isinstance(raw, str) or not raw or "\\" in raw:
        fail("invalid evidence path")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        fail(f"escaping evidence path: {raw}")
    try:
        root = root.resolve()
        target = (root / path).resolve()
    except (OSError, RuntimeError):
        fail(f"unresolvable evidence path: {raw}")
    if not target.is_relative_to(root):
        fail(f"escaping evidence path: {raw}")
    try:
        valid = target.is_file()
    except OSError:
        valid = False
    if not valid:
        fail(f"invalid evidence path: {raw}")


def check_requirements(identifier, requirements):
    if not isinstance(requirements, list):
        fail(f"invalid optional requirements: {identifier}")
    seen = set()
    for item in requirements:
        if not isinstance(item, dict) or set(item) != {"kind", "name", "install"}:
            fail(f"optional requirement fields mismatch: {identifier}")
        kind, name, install = item["kind"], item["name"], item["install"]
        if not isinstance(kind, str) or kind not in REQUIREMENT_KINDS:
            fail(f"invalid optional requirement kind: {identifier}")
        pattern = COMMAND_NAME if kind == "command" else MODULE_NAME
        if not isinstance(name, str) or not pattern.fullmatch(name):
            fail(f"invalid optional requirement name: {identifier}")
        if not isinstance(install, str) or not install.strip():
            fail(f"invalid optional requirement install text: {identifier}")
        if (kind, name) in seen:
            fail(f"duplicate optional requirement: {identifier}")
        seen.add((kind, name))


def requirement_found(item):
    """Look up only; the command or module is never run or imported."""
    if item["kind"] == "command":
        return shutil.which(item["name"]) is not None
    try:
        return importlib.util.find_spec(item["name"]) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def check_registry(path=DEFAULT_REGISTRY, root=ROOT):
    registry = load_registry(path)
    if not isinstance(registry, dict) or set(registry) != {"version", "capabilities"}:
        fail("registry schema fields mismatch")
    entries = registry["capabilities"]
    if registry["version"] != 1 or not isinstance(entries, list):
        fail("invalid registry version or capabilities")
    seen_ids, seen_modules = set(), set()
    dependencies = []
    for entry in entries:
        required = {
            "id",
            "name",
            "module",
            "dependencies",
            "optional_requirements",
            "support",
            "availability",
            "evidence_class",
            "evidence",
            "next_setup_action",
        }
        if not isinstance(entry, dict) or set(entry) != required:
            fail("capability schema fields mismatch")
        identifier = entry["id"]
        if not isinstance(identifier, str):
            fail(f"invalid capability ID: {identifier!r}")
        if identifier in seen_ids:
            fail(f"duplicate capability ID: {identifier}")
        if identifier not in IDS:
            fail(f"invalid capability ID: {identifier}")
        seen_ids.add(identifier)
        if not isinstance(entry["name"], str) or not entry["name"].strip():
            fail(f"invalid capability name: {identifier}")
        module = entry["module"]
        if not isinstance(module, str):
            fail(f"invalid or duplicate module: {identifier}")
        if not SLUG.fullmatch(module) or module in seen_modules:
            fail(f"invalid or duplicate module: {identifier}")
        seen_modules.add(module)
        if not isinstance(entry["support"], str) or entry["support"] not in STATES:
            fail(f"invalid support state: {identifier}")
        if not isinstance(entry["availability"], str) or entry["availability"] not in AVAILABILITY:
            fail(f"unknown availability: {identifier}")
        if entry["availability"] == "unknown" and entry["support"] == "available":
            fail(f"unknown availability cannot be available: {identifier}")
        if not isinstance(entry["evidence_class"], str) or entry["evidence_class"] not in EVIDENCE_CLASSES:
            fail(f"invalid evidence class: {identifier}")
        if not isinstance(entry["evidence"], list):
            fail(f"invalid evidence: {identifier}")
        if entry["evidence_class"] == "none" and entry["evidence"]:
            fail(f"evidence class mismatch: {identifier}")
        if entry["evidence_class"] == "artifact" and not entry["evidence"]:
            fail(f"evidence class mismatch: {identifier}")
        for item in entry["evidence"]:
            evidence_path(root, item)
        if not isinstance(entry["dependencies"], list) or not all(
            isinstance(item, str) for item in entry["dependencies"]
        ):
            fail(f"invalid dependencies: {identifier}")
        check_requirements(identifier, entry["optional_requirements"])
        dependencies.extend((identifier, item) for item in entry["dependencies"])
        if (
            not isinstance(entry["next_setup_action"], str)
            or not entry["next_setup_action"].strip()
        ):
            fail(f"invalid next setup action: {identifier}")
    if seen_ids != IDS:
        fail("capability ID inventory must contain exactly 01 through 72")
    for identifier, dependency in dependencies:
        if dependency not in seen_ids or dependency == identifier:
            fail(f"unknown or self dependency: {identifier} -> {dependency}")
    return entries


def registry_arg(value):
    return Path(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=registry_arg, default=DEFAULT_REGISTRY)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check", help="validate declared metadata")
    subcommands.add_parser("list", help="list declared capabilities")
    status = subcommands.add_parser("status", help="show capability support status")
    status.add_argument("id", nargs="?", help="two-digit capability ID")
    args = parser.parse_args(argv)
    try:
        entries = check_registry(args.registry)
    except (OSError, TypeError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    if args.command == "check":
        print("PASS: 72 declared capabilities; metadata is internally consistent")
    elif args.command == "list":
        for entry in entries:
            print(
                f"{entry['id']}\t{entry['name']}\t{entry['support']}\t{entry['module']}"
            )
    else:
        selected = (
            entries
            if args.id is None
            else [item for item in entries if item["id"] == args.id]
        )
        if not selected:
            print(f"FAIL: unknown capability ID: {args.id}", file=sys.stderr)
            return 1
        if args.id is None:
            print(
                "support states: available, partial, planned, unavailable, unsupported"
            )
            print(
                "support and availability are declared; optional requirement lines are "
                "local lookups, nothing is run, and a found requirement does not mean "
                "the capability works; unknown never means available"
            )
        for entry in selected:
            print(
                f"{entry['id']} {entry['name']}: {entry['support']} ({entry['availability']})"
            )
            print(f"  module: {entry['module']}")
            print(f"  depends on: {', '.join(entry['dependencies']) or 'none'}")
            print(f"  evidence class: {entry['evidence_class']}")
            for item in entry["optional_requirements"]:
                label = f"optional requirement: {item['name']} ({item['kind']})"
                if requirement_found(item):
                    print(f"  {label}: found")
                else:
                    print(
                        f"  {label}: MISSING, {item['install']}; "
                        "everything else still works"
                    )
            print(f"  next setup action: {entry['next_setup_action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
