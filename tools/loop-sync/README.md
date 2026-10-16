# loop-sync

Checks that every loop in a registry (a LOOPS-format table) is actually wired, so a loop
documented as live but not running shows up as drift. Read-only; prints a report.
Status: runnable.

## Usage

```
tools/loop-sync/loop-sync --registry LOOPS.md [--strict] [--host <name>] [--settings <file>]
bash tools/loop-sync/tests/run.sh      # tests, fictional registries and a fake systemctl
```

Start from `LOOPS.template.md`. The registry is a Markdown table; columns are found by header
name, so extra columns are fine:

| Column | Meaning |
|---|---|
| `Loop` | first cell, in backticks |
| `Hosts` | `any` or a comma list of host names; other hosts skip the row |
| `Check` | `;`-separated: `timer:<unit>`, `service:<unit>`, `hook:<name>`, `file:<path>`, or `none` |
| `STATUS` | last column; a leading stop-sign marks the row as documented-dead |

## Behavior

- Default run always exits 0 and prints the same output as `--strict`.
- `--strict` exits 1 if a check finds a missing unit or hook, or a row's STATUS starts with the
  dead marker. Inactive-but-installed units are warnings, not violations.
- No systemd user manager (container, CI, other OS): unit checks report "not checkable" and the
  run does not fail. Hook checks without `--settings` are also "not checkable".
- Usage errors exit 2.

## What it does not do

It verifies that a unit exists and is active, a hook name appears in a settings file, or a
file exists. It does not check that the loop's work is correct, that output is fresh, or what
the loop costs. It does not start, enable or edit anything.
