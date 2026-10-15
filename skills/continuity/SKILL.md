---
name: continuity
description: Record a verifiable checkpoint and one executable next action for resuming work in a fresh context. Use at handoff, interruption, or completed milestones.
---
# Continuity
## Objective
Leave a small, evidence-backed handoff rather than relying on conversational memory.
## Steps
1. State current state and cite the artifact or check output that proves it.
2. List active work separately from completed arrivals.
3. Write one exact next action a fresh worker can execute.
4. Save the record with [resume.md](resume.md), or optionally use `scripts/journal.py` for a local JSON journal.
5. Re-read the record in a fresh process or context before trusting it.
## Output and done check
Done means evidence is nonempty, active work is explicit, and Next is actionable without interpretation.
## Failure handling
If evidence is missing, checkpoint as unresolved; never call a handoff ready just because no work is visible.
## Worked example
Illustrative checkpoint format (not an execution receipt): “State: parser checks completed. Evidence: record the exact command and observed output. Next: inspect the empty-input fixture.” A selectively copied skill needs only its local assets; the full collection also provides a deterministic continuity example.
