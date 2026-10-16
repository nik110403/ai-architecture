# Gates: <Topic>

**Optional artifact.** Add a GATES.md next to a T2+ pack's SPEC.md when the item-level
`Acceptance:`/`Verify:` lines in SPEC.md aren't enough on their own to settle "is this leaf
actually done" — see plan-pack SKILL.md for when. Read by `gate-check` (deterministic) and
by the `verifier` agent, which runs it first and cites the output.

Format lifted from `Leonxlnx/unlazy` (references/gates.md, MIT), pinned SHA
`ed9e8d2b5919698cf2c54bda270d507e10b69617`, adapted: gates cross-reference SPEC.md item IDs via
an optional `SPEC:` attribute, and `gate-check` is a stateless reporter (it never edits this
file — the verifier re-runs it fresh each time, so there is nothing to keep in sync).

## Format

```markdown
# Gates: <scope name>

Scope: <one line — what this pack or SPEC item delivers>

- [ ] G1: <observable outcome, stated so a stranger could judge it>
  SPEC: S1
  CHECK: <shell command>
  EXPECT: <substring or /regex/>
  EVIDENCE: pending

- [ ] G2: <manual outcome, when no command can prove it>
  SPEC: S2
  EVIDENCE: pending

ABANDON: G2 <reason, only if a gate had to be surrendered — visible surrender, not deletion>
```

## Parsing rules

- A gate starts at a line matching `- [ ] <id>: <title>` or `- [x] <id>: <title>` (case-
  insensitive `x`). `<id>` is a single token with no spaces (`G1`, `G2`, ...).
- Indented `SPEC:`, `CHECK:`, `EXPECT:`, `EVIDENCE:` lines up to the next gate belong to the
  gate above them. All four are optional; a gate with no `CHECK:` is a manual gate.
- `SPEC:` names the SPEC.md item ID (`S1`, `S2`, ...) this gate proves, when there is one —
  informational only, not parsed for logic.
- `EXPECT:` is a plain substring match against the command's combined stdout+stderr, unless
  wrapped in slashes, then it is a Python regex (e.g. `/8\/8 passed/`).
- `ABANDON: G<n> <reason>` anywhere in the file marks that gate as honestly surrendered.
  `gate-check` reports it separately and does not count it against the exit code.

## What `gate-check` reports as unmet (⛔)

1. A `CHECK:` gate whose command output does not match `EXPECT:` (or, with no `EXPECT:`, a
   non-zero exit) — run fresh every time, the checkbox in the file is not trusted.
2. A manual gate (no `CHECK:`) whose box is unchecked, or checked with `EVIDENCE:` still
   reading `pending` — checked-without-evidence is a claim, not proof, and counts as unmet.

A gate named on an `ABANDON:` line is reported separately, never as ⛔.

## Writing good gates

- **State outcomes, not activities.** "All 8 planets clickable" is checkable. "Work on planet
  interaction" is not.
- **Prefer runnable gates.** Every `CHECK:` converts self-assessment into a free shell command.
  If you cannot think of one, ask whether the outcome is observable at all.
- **Make EXPECT decisive.** Match the line that can only appear on success (`8/8 passed`), not
  one that appears either way (`done`).
- **Five to twelve gates per file** is the useful range. Two means the leaf is under-specified;
  twenty means it should have been two leaves.
- **A number that will appear in a final report deserves its own gate** with a `CHECK:` that
  measures it — a number stated from memory is exactly the failure class this file exists to
  catch.
