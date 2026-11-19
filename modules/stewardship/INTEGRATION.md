# Integration, status, removal

Install: copy `modules/stewardship/` and `tools/plancheck/`. Nothing else is needed; the module is stdlib plus bash.

Pair it with [state-adapters](../state-adapters/README.md) to export the registered projects' state. Pick the
registry path yourself and keep it outside your source tree; `bootstrap` and `register` are the only writers of it.

Status: `claimed_support` is `planned` in `descriptor.json`; this module has no recorded live evidence.

Removal: delete `modules/stewardship/`. Files it created in your projects are ordinary plan files and stay.
