---
name: maintenance
description: Perform routine project upkeep through a small checklist, evidence capture, and deferred follow-up. Use for dependency, documentation, test, or housekeeping cycles.
---
# Maintenance
## Objective
Make recurring upkeep observable and bounded instead of mixing it into feature work.
## Steps
1. State the maintenance target and allowed change class.
2. Collect current evidence: status, check output, or documented drift. For a declared loop snapshot, run `python3 -B scripts/loop_check.py --registry REGISTRY.json --adapters ADAPTERS.json`; both files are required and explicit.
3. Apply only reversible or explicitly approved routine changes.
4. Run the relevant health check and record deferred concerns with a revive trigger. `scripts/loop_check.py` sorts opaque IDs, validates both complete inventories, and reports `checked`, `info`, `drift`, `unavailable`, and `skipped`. `--strict` exits 1 only for drift; unavailable-only output remains exit 0 with `complete: false`.
5. Use [maintenance.md](maintenance.md) for the result.
## Output and done check
Done means the completed check, changed paths, and next review trigger are recorded.
## Failure handling
If upkeep reveals an architectural or security decision, stop the routine and open a separate scoped item.
## Worked example
Run the documentation link check, fix one broken local link, and defer a site redesign until a named release milestone. Or run the loop snapshot reporter on the authored fixtures; it does not probe scheduling or prove a live loop.
