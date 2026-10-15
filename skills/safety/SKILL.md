---
name: safety
description: Check whether a proposed action has a clear authority, reversible scope, and evidence before it is performed. Use before state-changing or external actions.
---
# Safety
## Objective
Make action boundaries explicit without inventing permissions or enforcement.
## Steps
1. Name the action, target, expected effect, and reversibility.
2. Identify the authority that permits it and the evidence supporting this exact action.
3. Prefer a dry run, fixture, backup, or narrower scope where available.
4. Record what must be confirmed after the action.
5. Use [action-boundary.md](action-boundary.md) before proceeding.
## Output and done check
Done means the action owner can see what will change, why, and how success or rollback is checked.
## Failure handling
If authority, target, or consequence is unclear, stop and seek clarification; workflow text itself grants no permission.
## Worked example
Before deleting generated files, confirm they are regenerable and run the generator in a temporary directory first.
