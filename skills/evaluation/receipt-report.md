# Receipt report v1

Run `python3 -B scripts/receipt_report.py --input RECEIPTS.jsonl --format json`.
Repeat `--input` to read explicitly named JSONL files in caller order. `--since EPOCH`
is optional, accepts only a nonnegative integer, and includes `t == EPOCH`; it never
uses the current time. `--format` is `text` or `json`. A valid invocation exits 0;
an unreadable or invalid input exits 2, emits a controlled diagnostic on stderr, and
emits no report.

Each nonblank line must be one JSON object with no duplicate or unknown keys. Required
`schema_version` is integer `1` (not Boolean). The only accepted fields are
`schema_version`, `t`, `task_id`, `source`, `harness`, `role`, `model`, `charge_id`,
`verify`, `disposition`, `tok_source`, `in_tok`, `out_tok`, `reasoning_tok`,
`cache_read_tok`, `cache_write_tok`, `cost_usd`, and `dur_ms`. Omitted optional fields
become null, except omitted `verify` becomes `none`; explicit null `verify` is invalid.

Identity fields are null or nonblank strings. `t` and every token field are null or
nonnegative integers; `cost_usd` and `dur_ms` are null or finite nonnegative numbers.
`verify` is `pass`, `fail`, or `none`; `disposition` is null, `merged`, `discarded`,
`reworked`, or `abandoned`; `tok_source` is null, `agent`, or `main-fallback`.

The JSON report preserves normalized receipts and supplies input, deduplication, and
window counters. First `(task_id, source)` wins before windowing when `task_id` is not
null; rows without a task ID are never deduplicated. Groups are exact
`(harness, role, model)` values, sorted null-before-string. Every run is counted once
as `usage_known`, `usage_partial`, `usage_missing`, or `usage_excluded`; fallback rows
are excluded regardless of token fields. `observed_tokens_total` and its median use
only known or partial observations, so partial data remains `INCOMPLETE`, never a bill.
Cost and duration use independently present values, including fallback rows.

Deviation: finite inputs do not guarantee a representable aggregate. Integers are
validated without conversion to float and integer sums remain integers. Even-count
integer medians remain integers when their midpoint is integral; fractional
midpoints and mixed integer/float arithmetic use Python floats. Float results must
remain finite (on the tested host, maximum `1.7976931348623157e308`); decimal integer
input/output also retains the interpreter's configured digit limit (4300 by default
on the tested host). Arithmetic overflow or JSON serialization range failure exits
2 with a `numeric range` diagnostic, no report and no traceback, in both formats.
For example, two `cost_usd: 1e308` rows exceed the sum range; two `dur_ms: 1e308`
rows have a supported median of `1e308`, and integer `cost_usd: 10**400` (written as
decimal JSON digits) is preserved exactly. Nothing is nulled, clipped, or dropped
to fit the range. This bounded range exception needs disposition against any
requirement to accept all finite-number aggregates.

This is no more than offline receipt accounting. It does not discover files, read home,
environment, siblings, or rotated logs; write files; lock; invoke subprocesses; access
accounts or a network; select/rank models; create telemetry; join checker outcomes; or
establish an evaluation verdict.
