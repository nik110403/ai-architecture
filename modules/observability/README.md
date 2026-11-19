# observability

Normalised usage, cost, quota and session/resource reports. Stdlib only, Python 3, Linux. No network, no process or
model launch, no stored credentials. Claimed support: `planned` (see `descriptor.json`).

```
python3 -B modules/observability/observe.py spend --prices prices.json --receipts receipts.jsonl --envelopes envelopes.jsonl
python3 -B modules/observability/observe.py session --project DIR --session claude-code:ID
python3 -B modules/observability/observe.py resources --pid 1234
python3 -B modules/observability/observe.py quota status --config quota.json
python3 -B modules/observability/observe.py quota gate --config quota.json --provider NAME
```

Exit: 0 ok, 2 refused or bad usage, 3 quota gate `blocked`. `--out DIR` also writes the report (0600) into a 0700 directory.

## What it guarantees

- Missing usage, a missing price or a missing cache price never becomes a zero cost: the row's cost is `null` with a reason
  and the row is listed in the spend report's `unknown` bucket. `known_total` is `null` when no row has a cost and
  `complete` is `false` while any row is unknown. Groups are ordered by key, never by cost.
- A worker-reported `cost_usd` is shown as `reported_cost_usd` and never enters a computed cost, a total or an order.
- Prices are operator input in USD per million tokens (`{"unit": "usd_per_million_tokens", "models": {...}}`); none ship.
- Receipt rows keep `skills/evaluation/scripts/receipt_report.py` `usage_class`; dispatch envelopes are `usage_known` when
  input, output, cache_read and cache_write are all present. Known `reasoning` tokens > 0 make the row's cost `null`
  (reason `reasoning_unpriced`) unless the model's price entry declares `"reasoning": "in_output"`, meaning the reasoning
  tokens are already counted in output. `complete: true` therefore covers only the four priced counts plus reasoning
  that is zero, absent or declared in-output; it does not cover anything the worker did not report.
- The session report calls `modules/continuity/continuity.py status` (5 second timeout) and nothing else: unknown usage
  stays `null` with phase `UNKNOWN`, and no ledger is written.
- The resource report reads `/proc/<pid>/stat` and `status` only, read-only, for the pids you give.
- Quota: only the generic `snapshot-file` adapter is supported (an operator-maintained JSON file). A gate is
  `not_applicable` unless the provider is supported, configured with `gate: true` and its snapshot is fresh.
  A stale, future-dated, mismatched, malformed, oversize or symlinked snapshot is `unavailable` and never blocks.

## Not claimed (staged)

Accounting is staged: there is no per-account or per-plan accounting, no provider-specific live quota reader, and no
price data. A quota gate is advisory input for the operator's own policy; this module does not enforce it anywhere.
No model quality claim is made here; see `modules/model-evaluation`.

Tests: `python3 -B -m unittest discover -s modules/observability/tests`.
