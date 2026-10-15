---
name: workspace
description: Establish a bounded working area and inspect changes before sharing them. Use before multi-file work, reviews, or cleanup.
---
# Workspace
## Objective
Make ownership and changed files visible without claiming isolation the harness does not provide.
## Steps
1. Name the target root and allowed paths.
2. Inspect current changes before starting; preserve unrelated work.
3. Keep generated outputs separate from source when practical.
4. Inspect the final status and diff summary against the allowed paths.
5. Use [workspace-checklist.md](workspace-checklist.md) to report evidence.
## Output and done check
Done means every changed path is accounted for and no unrelated path is silently included.
## Failure handling
If unrelated changes block reliable inspection, stop or use a separately prepared workspace according to local project practice.
## Worked example
Illustrative recipient paths: permit `src/<parser>.py` and `tests/<test_parser>.py`; flag a changed lockfile before proceeding. These placeholders are not collection dependencies.
