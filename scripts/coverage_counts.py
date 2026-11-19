#!/usr/bin/env python3
"""Run the project's test suites and rewrite the generated block in COVERAGE.md.

    python3 -B scripts/coverage_counts.py          # run everything, rewrite the block
    python3 -B scripts/coverage_counts.py --check  # run everything, fail if the block differs

The block holds suite, command, result and count only: no timings, paths of the
host or dates, so the same tree on the same host gives the same text. Skipped
parts depend on what is installed (bun, a browser, ...) and show up in the result.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COVERAGE = ROOT / "COVERAGE.md"
BEGIN, END = "<!-- counts:begin -->", "<!-- counts:end -->"
TIMEOUT = 10800  # per step: the full unittest step took 7897 s on a loaded host (2026-11-17)
PY = ["python3", "-B"]


def suites(root=ROOT):
    """(suite, argv, needs_temp_home) in a fixed order."""
    items = [
        ("collection structure", PY + ["scripts/check_collection.py"], False),
        ("privacy scan", PY + ["scripts/privacy_scan.py"], False),
        ("unittest tests/", PY + ["-m", "unittest", "discover", "-s", "tests"], False),
        (
            "pytest tests, worked example",
            PY + ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", "examples/worked-example/tests"],
            False,
        ),
    ]
    for path in sorted(root.glob("skills/*/scripts/test_*.py")):
        rel = path.relative_to(root).as_posix()
        items.append((rel, PY + [rel], False))
    items.append(
        ("native-system example", PY + ["-m", "unittest", "discover", "-s", "examples/native-system/tests"], False)
    )
    for path in sorted(root.glob("modules/*/tests")):
        if any(path.glob("test_*.py")):
            rel = path.relative_to(root).as_posix()
            items.append((rel, PY + ["-m", "unittest", "discover", "-s", rel], False))
    runners = sorted(root.glob("tools/*/tests/run.sh"))
    runners += [root / "plugins/gates/tests/run.sh", root / "opencode/tests/run.sh",
                root / "opencode/rotation/tests/run.sh"]
    for path in runners:
        rel = path.relative_to(root).as_posix()
        items.append((rel, ["bash", rel], True))
    return items


def parse(output, returncode):
    """Return (result, count) from one suite's output and exit code."""
    summary = output
    # count classes in priority order; the first class present gives the number
    passed = [int(n) for n in re.findall(r"(\d+) passed", summary)]
    pass_eq = [int(n) for n in re.findall(r"\bpass=(\d+)", summary)]
    ran = [int(n) for n in re.findall(r"^Ran (\d+) tests?\b", summary, re.M)]
    skipped = sum(int(n) for n in re.findall(r"skipped=(\d+)", summary))
    skipped += sum(int(n) for n in re.findall(r"(\d+) skipped", summary))
    for values in (passed, pass_eq, ran):
        if values:
            count = f"{sum(values)} tests"
            break
    else:
        count = "n/a"
    if skipped:
        count += f", {skipped} skipped"
    # runners mark parts they could not run with "NOT RUN" or, for the opencode
    # structural parser, "ACCEPTANCE SKIPPED"; plain unittest skips are not a marker
    not_run = "NOT RUN" in output or "ACCEPTANCE SKIPPED" in output
    if returncode != 0:
        result = f"FAIL (exit {returncode})"
    else:
        result = "PASS, part NOT RUN" if not_run else "PASS"
    return result, count


def run_suite(argv, temp_home):
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as home:
        if temp_home:
            env["HOME"] = home
        try:
            proc = subprocess.run(
                argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return "FAIL (timeout)", "n/a"
    return parse(proc.stdout + proc.stderr, proc.returncode)


def render(rows):
    lines = [
        "| Suite | Command | Result | Count |",
        "| --- | --- | --- | --- |",
    ]
    for suite, argv, result, count in rows:
        command = " ".join(argv)
        if argv[0] == "bash":
            command = "HOME=<temp dir> " + command
        lines.append(f"| `{suite}` | `{command}` | {result} | {count} |")
    return "\n".join(lines)


def block_of(text):
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        raise SystemExit(f"coverage_counts: markers {BEGIN} / {END} missing in COVERAGE.md")
    return start + len(BEGIN), end


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the block would change")
    args = parser.parse_args(argv)
    rows = []
    for suite, command, temp_home in suites():
        result, count = run_suite(command, temp_home)
        rows.append((suite, command, result, count))
        print(f"{suite}: {result}, {count}", file=sys.stderr)
    table = "\n" + render(rows) + "\n"
    text = COVERAGE.read_text(encoding="utf-8")
    start, end = block_of(text)
    if args.check:
        if text[start:end] != table:
            print("coverage_counts: COVERAGE.md block differs from a fresh run", file=sys.stderr)
            return 1
        print("coverage_counts: block matches")
        return 0
    COVERAGE.write_text(text[:start] + table + text[end:], encoding="utf-8")
    print(f"coverage_counts: wrote {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
