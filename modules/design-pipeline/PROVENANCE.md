# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` is the sha256 of the upstream
file as read when this module was written). Every unit below is `re-authored`: no upstream text, list, palette or rule was
copied, and the 12-word-shingle comparison against all eight upstream files found 0 shared shingles. The `Source:` values
are logical names of files in an author's installed skill and agent libraries; they are not shipped. Upstream assets stay
attributed dependency assets (see `adapters.json` for the slots and `design.py deps` for their state).
`scripts/check_collection.py` validates this file.

## front-door sequence

Source: operator skill library, ui-ux-design SKILL.md (front door sequence)
Source-sha256: eebb5819a5ca3c8bd264354727479dc20f1dfab7b22432ada7bfbb498b33cdfc
State: re-authored

License seen at the source: MIT (operator-authored).

Deltas:
- the order route, record, build, render, critique, revise, render, critique, receipt is enforced by `design.py` from `pipeline.json`, with at most two revision rounds; the upstream sequence is prose guidance
- the record fields come from the repository's own design brief; an undecided token set becomes a decision request and nothing is built
- no register defaults, no house style and no operator rules are carried over

## design-builder

Source: operator agent library, design-builder agent
Source-sha256: 7c67e63ba4d4ca2652f88cca747461f4440a3500da63322a5e35abaa551f0a19
State: re-authored

License seen at the source: unresolved.

Deltas:
- the builder seat is a role of this repository (agents/design-builder.md); the stand-in worker in the example is authored here and applies fixed text edits
- the builder receives the critic's findings and their sha only, never the critic's tooling or bundle path

## design-critic

Source: operator agent library, design-critic agent
Source-sha256: 5ae0f1fd0f41367b4be80b80ffeeffd7e6c36bc816263b3e2fb0c865489d10e1
State: re-authored

License seen at the source: unresolved.

Deltas:
- the critic seat is read-only (no shell); it reads a bundle of rendered artifacts built by the pipeline and every finding must cite an evidence id in that bundle
- the stand-in critic converts check results into findings; model judgement is NOT RUN

## accessibility adapter

Source: web-design-guidelines SKILL.md (third-party, vercel)
Source-sha256: 820d83c75fc773dc4927ba41fcbdf92abd876900a1e1577adefc0479e219c322
State: re-authored

License seen at the source: unresolved.

Deltas:
- four narrow local checks re-authored from the accessibility idea: accessible names from the browser AX tree, WCAG contrast, real Tab reach, visible focus by pixel difference
- no guideline text is read at run time and nothing is fetched

## motion adapter

Source: emil-design-eng SKILL.md (third-party)
Source-sha256: 3b658bbf1c7ad8bf43f8a8f9a14f69505d560cf0bb11b2cd8195efa7437b0bb3
State: re-authored

License seen at the source: unresolved.

Deltas:
- one narrow check: under reduced-motion emulation the declared motion targets run animations and transitions of at most 0.01 s
- applies only when the record sets motion; otherwise it is out of scope and refused

## style adapter

Source: impeccable 4.2.2 SKILL.md (third-party)
Source-sha256: 4f42c67f5f201895085165472366305aab77ce7676fe8f69e24bd26ef6e00312
State: re-authored

License seen at the source: Apache-2.0 (frontmatter).

Deltas:
- one narrow check: computed colors and the first font of every visible element must be members of the record's own tokens
- it requires tokens and never falls back to a default set

## taste reference

Source: taste-skill SKILL.md (third-party)
Source-sha256: bfbe565766f9cad87c1a64427e077abce20fc67d6ecd879593a4c6fdbd96a7ec
State: re-authored

License seen at the source: unresolved.

Deltas:
- nothing is adopted; the slot exists so a user who installs the skill can see it as present-untested
- the pipeline has no taste defaults of its own

## interaction reference

Source: frontend-reference-kit SKILL.md (operator wrapper; public upstream emilkowalski/skills commit e8a175de…)
Source-sha256: d01dfe9557feaeeb630b47c94dcb4eb153bb1c060cf1b552bb8db5d1f6bda30a
State: re-authored

License seen at the upstream: MIT.

Deltas:
- only the idea that interaction states are checked in a rendered page; trusted keyboard events, focus and Escape handling are re-authored as the keyboard-reach, focus-visible and interaction checks
- none of the wrapper's own provenance lines are copied

## Not ported

- the upstream ban lists, font choices, palettes and the ui-ux-pro-max grounding (taste defaults)
- the live fetch of a guidelines URL by the web guidelines skill (network)
- the platform detector binary of the style skill (third-party, not vendored)
- the upstream agents' operator rules: paths, memory, owner approvals
- the hook and Stop-gate nudge of the operator setup
- any puppeteer tool use; rendering here goes through the repository's own headless Chrome launcher
