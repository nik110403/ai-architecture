# <Topic> — Tracker

**Status: active** <!-- active | parked | done | abandoned | promoted (promoted → leave POINTER.md; parked → add `Parked-until: <date|condition>`) -->
Status + decision log for [SPEC.md](SPEC.md) · [STRATEGY.md](STRATEGY.md) ·
[QUESTIONS.md](QUESTIONS.md) (if present).
**Read this first** in a fresh or post-compact session — this file replaces the
transcript.

**Outcome:** <one line — the user-facing question or result this pack exists for, in the owner's terms
("is there an executable edge after fees?", "the owner logs workouts on it weekly"). Never an ops metric.>
**First-result:** <the crudest artifact that answers Outcome at all — a notebook, one post, one
install the owner uses> — due YYYY-MM-DD — open|delivered YYYY-MM-DD <due ≤14 days from opening;
`plancheck` flags an open First-result past its due date as ⛔>

<!-- Outcome = why the pack exists (stable). Goal (below) = the current milestone's check. A Goal
that is pure ops ("doctor exits 0") must name the Outcome step it unblocks. -->


## Status

| Item | Lane | State | Evidence |
|---|---|---|---|
| S1 | use / build | pending / in-progress / done / dropped | <verify output fragment or commit> |

**Goal:** <one-line completion condition for the current milestone — written at
planning time, restated at execution kickoff. Specific and measurable, not vague
("feature X works") — name the concrete check (a command's exit code, a count, a file). Phrase
it so the evidence (test output, verify results) appears in the session transcript: the goal
judge reads the transcript, not the disk. Rewrite per milestone, don't accumulate.>

## Resume here

<Rewritable — overwrite this block each time, never append. This is the one thing a
fresh or post-`/clear` session reads to continue. Keep it under ~15 lines: if it is
growing, the detail belongs in SPEC or the decision log, not here.>

- **Delivered:** <what the owner can see / use / decide now that they couldn't before — or `none`>
  (YYYY-MM-DD) <first line, always. A real delivery names a path the owner opens or the `Lane: use`
  item that produced it; evidence (hashes, test counts, session ids) is NOT delivery. `none` is
  honest but does not reset the clock: >21 days since the last non-`none` Delivered (or since
  opening) → continue/descope/park/abandon prompt. Same rule for `First-result:
  delivered` — it names the path or item>
  - **Last delivered:** <date + what, carried forward while Delivered is `none` — the clock reads this>

- **State:** <what is true right now, incl. committed vs uncommitted>
- **Next:** <item ID + verb, e.g. "build S4" — executable by default; "on the owner's go" only when
  that item carries `Gate: human`. With two lanes, split into `- **Next (use):**` and
  `- **Next (build):**` — one tracker, no separate pack per lane>
- **Watch out:** <the trap that would cost the next session an hour>

## Decision log (append-only)

<Never rewrite rows. New evidence overturning an old decision = new row. DECISIONS only,
never routine progress — and keep each row to ~1 line (~250 chars): fat rows are how
trackers cross the 50KB read gate. `plancheck` flags both. The last column records the
load-bearing assumption the model made — the thing that, if false, reopens the row.>

| Date | Decision | Evidence | Assumption that would overturn it |
|---|---|---|---|
| | | | |

---
Last verified: YYYY-MM-DD
