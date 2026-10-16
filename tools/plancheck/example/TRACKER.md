# Recipe Box — Tracker

**Status: active**
Status + decision log for [SPEC.md](SPEC.md) · [STRATEGY.md](STRATEGY.md).
Read this first in a fresh session.

**Outcome:** the household finds any saved recipe in under 30 seconds.
**First-result:** one recipe found by keyword from the command line — due 2026-01-12 — delivered 2026-01-10

## Status

| Item | Lane | State | Evidence |
|---|---|---|---|
| S1 | use | done | `bash find.sh lentil` printed recipes/lentil-soup.md |
| S2 | build | pending | |

**Goal:** `bash find.sh --tag vegan` exits 0 and lists only vegan recipes (S2 verify command).

## Resume here

- **Delivered:** keyword search over recipe files (2026-01-10); run `bash find.sh lentil`.
- **State:** S1 merged; S2 not started.
- **Next:** build S2.
- **Watch out:** recipes without a `tags:` line must be skipped, not treated as untagged matches.

## Decision log (append-only)

| Date | Decision | Evidence | Assumption that would overturn it |
|---|---|---|---|
| 2026-01-05 | Plain Markdown files, no database | Strategy section 4 | Recipes turn out to be mostly photos |
| 2026-01-19 | Keep importing after the day-14 check | Trigger passed | Fewer than 5 recipes used in practice |

---
Last verified: 2026-01-19
