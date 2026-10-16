# Dispatch router - when to use which skill or agent, and how strictly

A single place to decide auto-dispatch for the skills and agents shipped in this collection. The goal: ask by intent and have the right skill or agent picked without naming it. This file is guidance for an orchestrating model; nothing here is enforced by a hook unless you wire one yourself.

## Decision framework (governs every skill and agent, current and future)

**STRICT** (deterministic - a hook, or an explicit MUST rule) only when ALL hold:
- The trigger is mechanical and pattern-detectable (file glob, command run, diff or branch state)
- The cost of missing is high (security hole, broken pipeline, unreviewed interface shipped)
- The action is SAFE to over-trigger (read-only review agents, idempotent actions)
- Few false positives

**JUDGMENT** (description-driven) when ANY hold:
- The trigger depends on intent or semantics, not a pattern
- Many candidates compete and the right pick needs nuance
- Over-triggering is costly or annoying, or the action writes or loads heavy context
- The space is still growing

**60/30/10 (a separate axis - whether a model is used at all):** before adding a model step, ask whether the job is 60 (plain code), 30 (rules, a hook or a linter) or 10 (a model), and default to the lowest layer that solves it. Pair this with plan-pack's "60% version" line.

**Scaling rule:** STRICT stays a small, curated set of high-value mechanical triggers. JUDGMENT is the default and scales for free: a new skill needs only a clean, non-overlapping `description:` and one row below, never new enforcement logic. When in doubt, default to JUDGMENT and sharpen the description; promote to STRICT only after a miss actually hurts. As shipped, every row is JUDGMENT; this collection includes no enforcing hooks.

## Registry (skills and agents shipped in this collection)

| Trigger / intent | Route to | Kind |
|---|---|---|
| work needs a plan - "plan this", multi-session or multi-file effort about to start | plan-pack | skill (plan-pack) |
| execute a plan pack / implement the next phase of a pack | exec-pack | skill (exec-pack) |
| second opinion, adversarial review of a plan, analysis, diff or decision | tribunal, with the tribune agent per seat | skill + agent |
| turn an outcome into a bounded plan with acceptance criteria (small, no pack) | planning | skill |
| carry out one already-decided work item | execution | skill |
| hand a bounded task to a separate worker | delegation | skill |
| decided plan, multi-file or bulk edit, well-specified change | implementer (or builder for one small change) | agent |
| verify delegated work against its spec and verify loop | verifier | agent |
| kill false positives in a findings list, one finding per seat | refuter | agent |
| independent review of a frozen artifact | review | skill |
| stakes-scaled review, refutation and bounded repair | convergence | skill |
| review a frozen change for security and data-integrity risk | security-reviewer | agent |
| review a frozen interface artifact | design-critic | agent |
| single-source extraction against a written template | researcher | agent |
| source-bounded research note | research | skill |
| compact context card, stale context, handoff | context | skill |
| checkpoint and one next action for a fresh session | continuity | skill |
| pending results and safe fresh-process handoff | lifecycle | skill |
| measure a claim before optimizing | evaluation | skill |
| scoped working area, inspect changes before sharing | workspace | skill |
| interface intent, states and accessibility before frontend work | design | skill |
| routine upkeep with evidence | maintenance | skill |
| recurring loops, drift checks, retrospectives | operations | skill |
| instructions, memory and project inventory | discovery | skill |
| conservative route from explicit intent and explicit capabilities | routing | skill |
| state-changing or external action needs a check first | safety | skill |
| specialist work with explicit dependencies | specialist-journeys | skill |

Planning versus execution never overlap: plan-pack writes the files, exec-pack runs them. A plan that is not a pack (no tracker, no SPEC items) is executed through the execution and delegation skills instead.

## Add your own

1. Give the new skill or agent a clean, non-overlapping `description:` that says what it does and when to use it, in the same plain style as the shipped ones.
2. Add one row to the registry above: trigger or intent, route, kind. Default the row to JUDGMENT.
3. Check for overlap: search the other descriptions for the same trigger words. If two rows compete for one intent, narrow one description or add a one-line tie-break under the table.
4. Promote to STRICT only after a miss actually hurt, and only if all four STRICT conditions hold. A STRICT rule needs an enforcing mechanism you wire yourself (a hook or a gate); this collection does not provide one.
5. Skills and agents that write files or run commands keep their own scope rules; routing never widens permissions.
