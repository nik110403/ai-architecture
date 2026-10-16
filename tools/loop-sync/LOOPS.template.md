# LOOPS.md — loop registry (template)

Every automation that runs on a cadence or trigger, in one table. A loop that is not listed
is ungoverned; a loop marked live that is not actually running is drift, which `loop-sync`
reports. Copy this file, replace the example rows, then run:

    tools/loop-sync/loop-sync --registry LOOPS.md

## Tiers

| Tier | Autonomy | Writes | Human gate |
|---|---|---|---|
| **L1** | report-only | in-repo report files only | none: output is a proposal |
| **L2** | assisted fix | proposes edits behind a gate | a human approves first |
| **L3** | unattended | commits or mutates infrastructure | none; keep budget and rollback in place |

## The registry

Columns are found by header name, so extra columns are fine. `loop-sync` reads these:

- `Loop`: first cell, in backticks.
- `Hosts`: `any`, or a comma list of host names (as printed by `hostname -s`). Rows scoped to
  another host are skipped, not failed.
- `Check`: `;`-separated wiring checks. Types: `timer:<unit>`, `service:<unit>`,
  `hook:<name>` (searched in the file passed with `--settings`), `file:<path>` (relative to
  this file). `none` means nothing to verify.
- `STATUS`: last column. A leading marker is the row's own claim: `✅` live, `⚠️` warn,
  `⛔` dead. With `--strict`, a `⛔` row makes the run exit 1.

| Loop | Pattern | Tier | Mechanism | Cadence | Hosts | Check | Gate | STATUS |
|---|---|---|---|---|---|---|---|---|
| `nightly-report` | Daily triage | L1 | systemd user timer | daily 06:00 | any | timer:nightly-report.timer | none (report-only) | ✅ live |
| `state-watch` | State sweeper | L1 | systemd user service | continuous | any | service:state-watch.service | none (read-only) | ✅ live |
| `build-server-sync` | State sweeper | L3 | systemd user timer | hourly | build-host | timer:build-sync.timer | none (idempotent) | ⚠️ verify on build-host |
| `commit-guard` | Safety hook | L1 | settings hook | on tool use | any | hook:commit-guard; file:hooks/commit-guard.sh | blocks unsafe commits | ✅ live |
| `retired-example` | Triage | L1 | systemd user timer | weekly | any | none | none | ⛔ retired, row kept for history |
