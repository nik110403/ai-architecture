# Evaluation report v1

Run `python3 -B scripts/evaluation_report.py --input RECEIPTS.jsonl --format json`.
Repeat `--input` to read explicit JSONL paths in caller order. `--since EPOCH` is an
optional nonnegative integer; it includes `t == EPOCH`, excludes null timestamps, and
filters every validated row before any join. The report never reads ledger data, home,
environment, siblings, or rotated logs, and never writes files, invokes a shell or
subprocess, or accesses a network.

This command imports the sibling `scripts/receipt_report.py` reader. It deliberately
shares that reader's strict receipt v1 schema, duplicate-key detection, normalization,
and controlled exit-2 errors. A selective bundle must therefore include both scripts
and this document; the standalone test demonstrates the required sibling dependency.
All input is validated before window filtering. Invalid, unknown, or duplicate JSON
fields exit 2 with no stdout.

Only nonnull `charge_id` joins are performed. Maker roles are `implementer` and
`design-builder`; checker roles are `verifier`, `refuter`, `tribune`,
`security-reviewer`, and `design-critic`. Membership uses the part of a role before
`--`, while report groups preserve the raw role. For each maker charge ID, the result
uses the latest checker row with a real `pass` or `fail` and a known timestamp. Equal
timestamps use the last input row, in caller file then line order. An unknown-timestamp
checker never overrides a known one; if no known checker exists, the maker is unjoined.
Rows without a charge ID are also unjoined. No sibling, parent, or other inferred join
is attempted.

Maker groups are exact `(harness, role, model)` values, null-first, and report runs,
`checker_pass`, `checker_fail`, `checker_none`, and `pass_rate`. The rate denominator
is pass plus fail only and is null when it is zero. Checker groups use the same exact
keys and report their own pass, fail, and none counts. The join section separately
counts real checker verdict rows with no maker charge and those with unknown timestamps.
An empty window says `evidence: none` explicitly.

This is an offline join report, not a ranking, score, causal quality claim, benefit
claim, telemetry collector, or cost/usage report.
