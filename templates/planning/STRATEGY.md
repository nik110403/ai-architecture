# <Topic> — Strategy

**Status:** active · **Opened:** YYYY-MM-DD · **Owner:** <name>
**Companion:** [SPEC.md](SPEC.md) (what to build) · [TRACKER.md](TRACKER.md) (what's done)

---

## 1. Why this exists

<The problem, in numbers where possible. What breaks or bleeds without this work.
Which layer is this on — output the next model release commoditizes, or context only we have?>

## 2. Baseline / evidence

<Measured starting point and HOW it was measured — the re-measure later must use the
same method. Include the raw command/source.>

## 3. Constraints

<Non-negotiables. User-stated constraints verbatim.>

## 4. Approach

<Chosen approach and why it beats the alternatives in §5.>

**60% version:** <the cheapest version using tools that already exist (manual, existing skill,
one script) — and why it is insufficient, if it is. If it is sufficient, this pack stops here.>

**Exploration licence:** diagnostic looks at real data/output are always allowed from day one,
labelled `DIAGNOSTIC`, and never feed a preregistered or final verdict. Rigour protects the
verdict, not the look.

**Triggers** — dated checks that change the plan. One per line, machine-read by `plancheck`
(an `open` trigger past its date is ⛔ and overrides Resume `Next:`):

Trigger: YYYY-MM-DD | <check to run> | <action if it fires> | open
<status: open · fired YYYY-MM-DD · passed YYYY-MM-DD — update when read; include at least one
early kill/continue check before heavy build-out>

## 5. Rejected approaches

<Wrong turns recorded so no future session re-derives them. Include findings that
LOOKED right but were refuted — those are the ones most likely to be re-derived.>

| Approach | Why rejected | Evidence |
|---|---|---|
| | | |

## 6. What NOT to build

<Deliberate exclusions. Must not be reintroduced without revisiting this section.>

## 7. Assumptions

<Pack-level defaults, same grammar as SPEC.md item Assumptions.>

- Assumption: <default chosen> — basis: measured|inferred|guess; reversible by <what>;
  wrong if <signal>
