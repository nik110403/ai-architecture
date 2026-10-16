# Recipe Box — Strategy

**Status:** active · **Opened:** 2026-01-05 · **Owner:** example author
**Companion:** [SPEC.md](SPEC.md) (what to build) · [TRACKER.md](TRACKER.md) (what is done)

A fictional sample pack that shows the plan-pack file shapes. Nothing here refers to a real project.

---

## 1. Why this exists

A household keeps recipes in photos and chat messages. Finding "the lentil soup from last winter" takes minutes. A single searchable file would cut that to seconds.

## 2. Baseline / evidence

Measured by hand: 5 lookups took 4 minutes on average (stopwatch, 2026-01-04).

## 3. Constraints

- No hosted service; everything stays on the local machine.
- Plain text storage so it survives any tool change.

## 4. Approach

One Markdown file per recipe plus a small search script.

**60% version:** a single text file searched with grep. It is insufficient only because ingredient lists need per-recipe tags, which grep over one file cannot return as a list.

**Exploration licence:** diagnostic looks at real data are always allowed, labelled `DIAGNOSTIC`, and never feed a final verdict.

**Triggers** — dated checks that change the plan:

Trigger: 2026-01-20 | count recipes imported so far | stop importing if fewer than 5 are used in practice | passed 2026-01-19

## 5. Rejected approaches

| Approach | Why rejected | Evidence |
|---|---|---|
| Hosted notes app | Violates the local-only constraint | Constraint 1 |

## 6. What NOT to build

- No web interface. No sharing features.

## 7. Assumptions

- Assumption: recipes are text-first — basis: inferred; reversible by adding an attachments folder; wrong if most recipes are photos only
