---
name: operations
description: Define report-only recurring loops, drift checks, scouting, retrospectives, and promotion candidates. Use for maintenance planning.
---
# Operations
## Objective
Turn recurring maintenance into bounded, evidence-backed proposals rather than autonomous changes.
## Steps
1. Declare cadence, scope, owner, evidence, stop condition, and rollback before scheduling a loop.
2. Compare observed state to an explicit baseline; report drift rather than correcting it without authorization.
3. For scouting, record provenance, license, maintenance, and security evidence before use/fork/create/reject.
4. For retrospectives, require repeated evidence, an outcome delta, and deduplication before a promotion proposal.
5. Persist recommendations and a Next; schedule/execute only with recipient-provided adapters and authorization.
## Output and done check
Done is a checked report or approved, scoped action record. Promotion is never automatic.
## Failure handling
Missing scheduler, provenance, approval, or evidence is a GAP, not a silent no-op.
## Worked example
A weekly link check reports two broken references and proposes owners; it does not edit instructions or register a timer.
