---
name: lifecycle
description: Persist pending results, readiness evidence, and a safe fresh-process Next action. Use at milestones and handoff.
---
# Lifecycle
## Objective
Make resumption reproducible without treating arbitrary saved text as executable.
## Steps
1. Bank completed results with frozen artifact references and evidence.
2. List pending jobs and unknown arrivals separately; either prevents READY.
3. Record one exact Next as data, plus its allowed scope and required check.
4. Persist the bundled `templates/lifecycle-record.md` record described in [DEPENDENCIES.md](DEPENDENCIES.md), or the optional local journal.
5. Fresh-process recovery re-reads and validates state before a human or harness chooses an action.
## Output and done check
READY requires evidence, no pending jobs, and no unknown arrivals. Otherwise write NOT_READY with the blocker.
## Failure handling
Failed persistence or uncertain receipt is NOT_READY. This skill supplies no lock, scheduler, or concurrency enforcement.
## Worked example
One verifier result is banked but a security check is pending: checkpoint is NOT_READY and Next is “collect security verdict,” not “release.”
