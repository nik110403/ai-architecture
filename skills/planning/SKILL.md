---
name: planning
description: Turn a requested outcome into a bounded plan with acceptance criteria, dependencies, and an executable first action. Use before multi-step implementation or when scope is unclear.
---
# Planning
## Objective
Create a decision record that a separate worker can execute without inventing architecture.
## Steps
1. State the outcome, non-goals, and one assumption that would invalidate the approach.
2. List observable acceptance criteria before tasks.
3. Split work into ordered, independently checkable items; name inputs, owner boundary, and verification for each.
4. Record unanswered strategic questions as blockers rather than resolving them implicitly.
5. Write the first executable action using [plan-template.md](plan-template.md).
## Output and done check
Output a filled plan. It is done when each item has a concrete result and check, and the first action can be performed without a design decision.
## Failure handling
If a requirement contradicts the code or a key question is unanswered, stop at the affected item and mark it blocked; do not broaden the plan.
## Worked example
A “validate uploads” outcome becomes: define accepted file cases, implement one validator, then test accepted and rejected fixtures—not “improve uploads.”
