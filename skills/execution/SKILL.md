---
name: execution
description: Execute one already-decided work item minimally, with evidence from affected checks. Use when the what is settled and the remaining task is implementation.
---
# Execution
## Objective
Make the smallest scoped change that satisfies stated acceptance criteria.
## Steps
1. Read the whole work item and named files; identify the stated boundary.
2. Check callers or consumers before changing shared behavior.
3. Reuse existing code or standard facilities before adding code or dependencies.
4. Change only the files needed for the item and retain an evidence note.
5. Run the named focused check and report its exact outcome.
## Output and done check
Return changed paths, a one-line purpose for each, and command output. Done means the specified check ran successfully, not merely that a diff exists.
## Failure handling
If verification fails twice after distinct fixes, stop, preserve the failure output, and request a revised diagnosis.
## Worked example
For “reject empty project names,” add the shared validation at the input boundary and test both an empty and nonempty name.
