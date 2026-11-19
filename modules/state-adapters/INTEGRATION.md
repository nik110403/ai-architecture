# Integration, status, removal

Install: copy `modules/state-adapters/`, `modules/stewardship/`, `modules/contracts/` and `tools/plancheck/`.

Write exports and pages to a path you choose, outside your source tree. Nothing is scheduled; run the export
when you want a fresh view.

Status: `claimed_support` is `planned` in `descriptor.json`. A companion viewer is declared by manifest and is
reported, never run, until live evidence exists.

Removal: delete `modules/state-adapters/`. Exports you wrote are plain files you can delete.
