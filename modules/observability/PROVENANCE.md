# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` = sha256 of the source file
as read on 2026-11-06). Sources named `~/...` or `private operator repository, ...` are an author's installed files and
private operator-repository scripts; they are not shipped. `scripts/check_collection.py` validates this file, and
the module's own test suite runs that check on it because the collection walk covers only `plugins/` and `opencode/`.

## spend-split

Kit file: modules/observability/cost.py
Source: private operator repository, bin/spend-split
Source-sha256: 37fe53199e05670f9dfda74ae8732c03459bb344c57b2eb4f3e640bccc4e49c1
State: re-authored

Deltas:
- the cost rule is new: a cost is computed only from fully known token counts and a known operator price, otherwise it is `null` with a reason (`usage_missing`, `usage_partial`, `usage_excluded`, `price_missing`, `cache_price_missing`)
- no price ships; the price table is operator input with placeholder models in the fixtures
- exact arithmetic (fractions) so a hand-computed cost compares equal

## burn-report

Kit file: modules/observability/reports.py
Source: private operator repository, bin/burn-report
Source-sha256: bc449b6420cb8bf3d74ffa0acdbb8d458df6c6bb984ab3082c44991ca5f15041
State: re-authored

Deltas:
- the spend report lists every unknown-cost row in an `unknown` bucket, reports `known_total: null` when no row has a cost, sets `complete: false`, and orders groups by key, never by cost
- a worker-reported cost is shown next to the row and never enters a computed cost, a total or an order
- no private ledger or account parsing; inputs are strict v1 receipts (reused from `skills/evaluation`) and dispatch usage envelopes

## agent-usage

Kit file: modules/observability/usage.py
Source: private operator repository, bin/agent-usage
Source-sha256: 5e4cc2e82822d7c50f15e036cd66dfe691c71a21b95b5d3eb1247e989fa5c37c
State: re-authored

Deltas:
- normalised usage row with `null` for every unknown token count; `usage_class` for receipts is the offline reporter's own function, imported, not copied
- the private ledger parsing is not ported

## session-digest

Kit file: modules/observability/reports.py
Source: private operator repository, bin/session-digest
Source-sha256: ae6358f9f61a4168dcd0d240bd960bfd64289aeaf62c458fb6e29488ed5a228f
State: re-authored

Deltas:
- the session report reads sessions only through `continuity.py status` as a subprocess with a 5 second timeout; it keeps no second usage store and never records a guess
- transcript parsing is not ported

## capacity-sample

Kit file: modules/observability/probes.py
Source: private operator repository, bin/capacity-sample
Source-sha256: 98c769140d45bd2ef98c4daa1358a641b19185c1dd7ad35d85e739cbec091657
State: re-authored

Deltas:
- the resource report reads only `/proc/<pid>/stat` and `/proc/<pid>/status` for the pids it is given; unreadable fields are `null`; it opens read-only with `O_NOFOLLOW`, never reads cmdline, environ or fd, and starts no process
- host-wide sampling and service probes are not ported

## provider-quota

Kit file: modules/observability/quota.py
Source: private operator repository, plans/model-program/opencode/efficiency/quota/provider_quota.py
Source-sha256: f31e09de5bee2827f97f5d5fcffa503a3e99939928c1259124d978b714b31691
State: re-authored

Deltas:
- only the generic `snapshot-file` adapter is supported: an operator-maintained JSON file, opened with `O_NOFOLLOW`, at most 1 MB, provider name and freshness checked
- the source's live HTTPS call with stored OAuth credentials and its subprocess reader for a vendor app server are not ported; neither is its reader of a local OpenCode quota cache (a candidate optional adapter)
- every other adapter is registered `unsupported` and never read; an adapter missing from the registry is also unsupported
- a stale, future-dated, mismatched, malformed, oversize or symlinked snapshot is `unavailable`: it never blocks and never counts as headroom

## claude-quota

Kit file: modules/observability/quota.py
Source: ~/.config/opencode/lib/claude-quota.ts
Source-sha256: a345ade58a8b45812053c596c7d0cd228470f2babd956d3d468be8af458dc41e
State: re-authored

Deltas:
- the gate is advisory and fails open: a configured gate whose data is stale or unreadable does not block, which inverts the source's fail-closed rule; `blocked` needs a supported provider, `gate: true` and a fresh snapshot over `threshold_percent`
- the statusline cache reader is not ported

## Not ported

- effort-trial, style-parity, human-time, fixture-run, oc-metrics: not ported (personal tuning or harness-specific tooling, outside this module's scope)
- agent-usage's private ledger parsing: not ported (reads an operator-specific store)
- the Claude statusline cache reader, the codex sidecar reader and the OpenCode local quota cache reader: not ported (personal tooling; the cache reader is a candidate optional adapter)
- per-account and per-plan accounting: not ported (accounting stays staged; see README)
