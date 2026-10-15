---
name: review
description: Independently check a frozen artifact against explicit criteria and return findings with evidence. Use before relying on a change or claim.
---
# Review
## Objective
Test a claim independently without editing the reviewed artifact.
## Steps
1. Freeze the artifact identity (path plus hash or revision) and acceptance criteria.
2. Inspect relevant implementation and tests independently of the builder’s narrative.
3. Run authorized deterministic checks or explain why none can run.
4. Classify findings by impact, evidence, and required correction.
5. Write the verdict with [review.md](review.md).
## Output and done check
Done means PASS, FIX, or BLOCKED is traceable to observed evidence; silence is not a pass.
## Failure handling
If the artifact changes during review, stop and request a new snapshot. Do not repair reviewed code.
## Worked example
Verify a migration against a copy of representative data and cite the exact failing row when rejecting it.
