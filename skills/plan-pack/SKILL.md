---
name: plan-pack
description: Work needs a plan - "plan this", "spec it out", a roadmap, a strategy/spec/tracker request, or any multi-session or multi-file effort about to start. Picks the planning tier T0-T3, writes the strategy/spec/tracker files at that tier from the shipped templates, and wires each file to the agent that consumes it.
---

## When this applies

Use when work needs a plan: "plan this", "spec it out", a roadmap, a strategy/spec/tracker request, or any multi-session or multi-file effort about to start. It decides how much planning the task deserves (tier ladder T0-T3), announces the tier with a one-line reason and proceeds (the human owner may object and change it), then writes the strategy/spec/tracker files (plus a questions file when open decisions exist) at that tier from `templates/planning/` and wires each file to its consumer agent: spec to implementer, acceptance criteria to verifier, strategy to tribune, tracker to fresh sessions, questions to the owner. Tiers escalate on signal and never demote. Not for whole-codebase audits and not for rendering plans visually.

Status of this port: the file formats, the tier ladder and the linter (`tools/plancheck/plancheck`) are runnable. Automatic hand-off into execution works only where your harness can run the companion `exec-pack` skill; that behaviour is UNVERIFIED outside the author's own setup. Unattended runners, morning reports and ledgers that the original setup used around this skill are not included.

**Selective adoption dependencies.** Copying `skills/plan-pack/` alone is insufficient. Preserve collection-root-relative paths (or explicitly adapt every reference): `templates/planning/TRACKER.md` for T1; add `templates/planning/SPEC.md` for T2 and `templates/planning/STRATEGY.md` for T3; add `templates/planning/QUESTIONS.md` and `templates/planning/GATES.md` when those contracts are used. Keep `tools/plancheck/plancheck` and `rules/stakes-dial.md` available. T2+ execution also needs the companion `skills/exec-pack/` and configured implementer/verifier roles; T3 review needs `skills/tribunal/` and its declared tools/roles. A GATES file requires `tools/gate-check/gate-check`. These are manual dependencies, not a bundle installer or an automatic role import. Resolve them before promising an execution handoff; report unavailable harness capabilities explicitly.

# plan-pack - right-sized planning docs

**Outcome first.** A pack exists to reach a result the owner uses, not to finish a platform. Packs that only describe foundations stall at first contact with the world. Every pack therefore names its `Outcome:` and a dated `First-result:`, opens with a walking skeleton, and keeps a `use` lane moving while `build` work continues.

One planning system: whatever dialogue or brainstorming tool you use runs the process; this skill defines the artifacts and how extensive they get. Never write planning docs freeform; never write all three files by default.

## Tier ladder

Before proposing a tier, name the **60% version**: the cheapest version using tools that already exist (manual, an existing skill, a spreadsheet, one script). Propose a tier only if that version is insufficient, and say why. The line goes in STRATEGY section 4 (`**60% version:**`) or, at T1, in the tracker header. Over-building is the dominant failure mode; this step exists to catch it.

Classify from signals. Announce **"Tier N - <one-line reason>"**, then proceed unless the owner objects. When between tiers, pick the lower one.

| Tier | Files | Signals |
|---|---|---|
| T0 | none - in-chat todos | up to about 3 files, single session, reversible |
| T1 | TRACKER | approach obvious, but work spans sessions and needs disk state |
| T2 | SPEC + TRACKER | multi-file, delegation to an implementer intended, acceptance criteria non-obvious |
| T3 | STRATEGY + SPEC(-n) + TRACKER | programme-level: contested why, measured baseline, several workstreams, decisions a future session might re-derive |

**Start low, escalate on signal - never classify big upfront, never demote** (files already written are history, not clutter):

- Tracker rows describing what to BUILD rather than what happened: go to T2 and extract a SPEC.
- Approach contested, wrong turn worth recording, baseline measured: go to T3 and extract a STRATEGY.
- **Addition-only flexibility:** a lower tier may ADD a file on real signal ("T1 + STRATEGY - contested approach"). The TRACKER is never dropped; resume logic and the linter key on it.
- A workstream that is independently buildable AND verifiable gets split into `SPEC-<workstream>` (a separate spec file); each split spec is a self-contained implementer brief. Never split by headings alone.

## File contracts

Templates live in `templates/planning/` - copy, do not invent structure. Mechanical shape rules (Resume block length, tracker size, decision-row length, footer, filenames, link integrity) are owned and checked by `tools/plancheck/plancheck` - do not restate them per pack.

- **STRATEGY** (`templates/planning/STRATEGY.md`) - why, constraints, measured baseline, chosen approach, **rejected approaches with reasons** (wrong findings are recorded precisely so no future session re-derives them), what NOT to build. **T3 gate:** a T3 STRATEGY gets a tribune pass (see the tribunal skill) before its first SPEC dispatches; verdicts land in the decision log.
- **SPEC** (`templates/planning/SPEC.md`) - the implementable what. Every item carries an acceptance criterion and a verify command. Self-contained: an implementer dispatched with only this file can build it. Items gated on an open question carry `Blocked-by: Qn` (the ONLY place that edge lives) - blocked items are not built. Implementers append `Deviation:` lines under an item when the build diverges; writing one is the pause signal for anything architectural. Optional `Needs:` (ordering) and `Touches:` (write scope, literal path prefixes, no globs) fields enable parallel scheduling; overlapping `Touches:` with no `Needs:` edge between the items is a planning defect - resolve it when the pack is written (add the edge or merge). Packs predating the fields backfill on next touch; the executor serializes items missing them.
- **Outcome fields** (TRACKER header, tiers T1 and up): `Outcome:` is why the pack exists in the owner's terms, never an ops metric. `First-result:` is the crudest artifact answering it, due within 14 days of opening, `open` or `delivered <date>`. The Resume block opens with `Delivered:` - what the owner can see, use or decide now that they could not before, or `none (date)`. Evidence is not delivery.
- **Lanes:** SPEC items carry `Lane: use|build` (absent = build). Use items may say `Needs: soft` (run on what exists now). One pack holds both lanes - split Resume into `Next (use)` and `Next (build)` instead of splitting packs. Infrastructure gaps become `Lane: build` items in the same pack; open a new pack only when the work serves at least two repositories (more than 3 active packs per repo is a plancheck smell).
- **Exploration licence:** diagnostic looks at real data or output are allowed from day one, labelled DIAGNOSTIC, and never feed a preregistered or final verdict. A rigorous verdict gate is never a reason not to look.
- **TRACKER** (`templates/planning/TRACKER.md`) - `Status:` header (`active | parked | done | abandoned | promoted`), status table, a **`Goal:` line**, a **rewritable `## Resume here` block**, and an append-only decision log (`date | decision | evidence | assumption`). It is the first read for any fresh or post-compaction session.
  - `Goal:` is a one-line completion condition for the current milestone, written at planning time and phrased so evidence lands in the transcript. Specific and measurable, not vague ("feature X works") - name the concrete check (exit code, count, file). Rewritten per milestone.
  - The Resume block is the "clear context" contract: state plus next action, **overwritten each time**, updated at session boundaries without being asked.
  - The decision log is decision-granularity, about one line per row - progress goes in the Resume block. A correction or discovery that invalidates a spec assumption is synced to the artifact when it happens, not at session end - chat is not durable.
- **GATES** (`templates/planning/GATES.md`) - optional, T2 and up. Add one next to the SPEC when item-level `Acceptance:` / `Verify:` lines alone will not settle "is this leaf actually done" - for example a leaf with several independently checkable outcomes. Format and rules are in the template; `tools/gate-check/gate-check` runs it, and the verifier runs it first and cites the output when a pack has one.
- **QUESTIONS** (`templates/planning/QUESTIONS.md`) - open decisions only the owner can make, answered asynchronously: numbered `## Qn` blocks (never renumbered) with inline context, checkbox options and a recommended pick; a `## Parked` section for unripe items. Resolution = an `**Answered:** <date>` marker plus ONE graduation row in the tracker decision log. Mid-work ambiguity is never guessed silently: if reversing the default later costs less than a session and the owner does not alone own the choice (money, external parties, taste), write an `Assumption:` line (`basis: measured|inferred|guess`) under the item and proceed; otherwise append a Q, mark the item `Blocked-by:`, and continue with unblocked work. A `guess` on an irreversible default is always a Q. Soft cap: 5 open Qn per pack (plancheck counts).

## Handoff - plans run themselves

**Walking skeleton.** At T2+, the first wave written is the thinnest end-to-end slice - input to a result the owner reads or uses, crude is fine - with at least one `Lane: use` item ready at write time (if none can be, say why in one line in the SPEC). At T1, the tracker's First-result and Next action serve this purpose without SPEC fields. Hardening `Needs:` the skeleton, never the reverse. STRATEGY dated checks are `Trigger:` lines (machine-read by plancheck; a past-due `open` trigger is blocking), including at least one early continue/kill check before heavy build-out. Plan revisions get a tribunal only when the pack is `Stakes: HIGH` and money or irreversibility is involved (see `rules/stakes-dial.md`, rule 7).

Writing the pack is the approval; ending at "shall I execute?" is a defect. **T1 is tracker-only:** overwrite Resume `Next:` with the next obvious action and its observable check, execute it serially using the harness's ordinary authorized process, verify the result and update the tracker. Do not invoke exec-pack or invent SPEC IDs, gates, or a wave graph for T1. If work needs an implementable build description or delegated acceptance criteria, escalate to T2 first. **T2+:** set each SPEC item's `Gate:` (absent = `auto`; `human` only for the closed strategic reason list in the SPEC template), overwrite Resume `Next:` with the first ready `Gate: auto` item (ID plus verb), then invoke the companion exec-pack skill in the same turn when its dependencies and harness capabilities are available. T0 means just do the todos. Stop gated or question-blocked work and name it; keep unblocked work moving. A merge is not intrinsically a human gate (exec-pack merges behind a reviewer and verifies before completion).

## Consumers - why the docs pay for themselves

A doc nobody dispatches against is ceremony. Wire each file on creation:

| File | Consumer |
|---|---|
| SPEC (or split spec) | implementer brief - dispatch with the path plus a charge, never a conversation dump |
| SPEC acceptance criteria | verifier diff target (it reads `Deviation:` lines first) |
| SPEC `Blocked-by:` | implementer dispatch filter - blocked items do not build |
| SPEC `Gate:` | exec-pack start filter - `auto` or absent builds unasked, `human` waits |
| SPEC `Needs:` / `Touches:` | exec-pack wave scheduler - waves from `Needs:`, collision-freedom from disjoint `Touches:` |
| STRATEGY | tribune seat brief (mandatory pass before the first T3 SPEC dispatch) |
| TRACKER | what a fresh session reads instead of the transcript |
| TRACKER `Goal:` line | the completion condition stated at execution kickoff |
| QUESTIONS | the owner, asynchronously |
| TRACKER `Outcome:` / `First-result:` / `Delivered:` | plancheck (an overdue first result is blocking) |
| STRATEGY `Trigger:` lines | plancheck (a past-due open trigger is blocking and overrides Resume `Next:`) |
| SPEC `Lane:` / `Stakes:` / `Box:` / `Lineage:` | exec-pack scheduler (use lane first) and ceremony scaling (`rules/stakes-dial.md`) |

## Location and lifecycle

`<repo>/plans/<topic>/`; a programme repo may promote the trio to the repo root. Cross-link the files in each header; bump the tracker's `Last verified:` footer whenever status is re-checked against reality. Drift detection is plancheck's job and is report-only. A pack whose last non-`none` delivery (`Last delivered:`) is more than 21 days old gets a continue/descope/park/abandon prompt from plancheck; the next session answers it with one decision row. `parked` is reversible and non-active and needs `Parked-until: <date|condition>`. Closing a pack: flip `Status:` to `done`, `abandoned`, `promoted` or `parked`; a promoted pack leaves a short pointer file to its new home. Non-active packs are skipped by plancheck.

## Steps

1. Name the 60% version and classify the tier; announce "Tier N - reason".
2. Copy only the templates for that tier from `templates/planning/` and fill them in; set `Outcome:`, `First-result:` and `Goal:`. T1 names its next action and check in Resume; T2+ writes a skeleton wave with a `Lane: use` item.
3. At T2+, give every SPEC item acceptance, a verify command, `Needs:`, `Touches:` and (when it matters) `Stakes:` and `Gate:`. T1 needs no SPEC.
4. Run `tools/plancheck/plancheck --dir plans/<topic>`; fix every blocking finding.
5. Overwrite Resume `Next:`; execute T1's next action serially, or hand the T2+ ready wave to exec-pack after checking its dependencies.

## Output and done check

Done planning when the files for the tier exist under `plans/<topic>/`, `plancheck --dir plans/<topic>` exits 0, Resume `Next:` names T1's concrete action and check or a T2+ ready item (or states what blocks further work), and the tier line was announced. Evidence to report: the plancheck summary line and the file list. Planning completion is not a claim that execution or integration is done.

## Failure handling

- plancheck blocking finding (dangling `Needs:` or `Blocked-by:`, missing Resume block, overdue first result, past-due trigger): fix the artifact, re-run; do not hand off a pack that exits 1.
- Overlapping `Touches:` with no `Needs:` edge: add the edge or merge the items before dispatch.
- Unsure about the tier: take the lower one and escalate on signal.
- A question only the owner can answer: write a Q, mark the item `Blocked-by:`, keep unblocked work moving.

## Worked example

A shipped sample lives in `tools/plancheck/example/` (a fictional "Recipe Box" pack: strategy, spec, tracker; `plancheck --dir tools/plancheck/example` exits 0) and a deliberately broken one in `tools/plancheck/example-broken/` (exits non-zero). Request: "plan a recipe finder". Announce "Tier 3 - contested approach and a measured baseline"; write the 60% version (grep over one file) and why it falls short; write S1 as the walking skeleton (`Lane: use`, feeds the first result) and S2 as hardening with `Needs: S1`; run plancheck; set Resume `Next:` to "build S1" and invoke exec-pack.
