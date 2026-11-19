# Integration, status, removal

Install: copy `modules/discovery-inbox/` together with `modules/knowledge-adapters/`, `modules/memory-backends/`,
`modules/state-adapters/`, `modules/stewardship/` and `modules/contracts/` (it calls them by path as subprocesses or by import).
The `tools/plancheck` tool is used by stewardship. Install your own discovery companion; this module never installs software and
ships no companion. The companion needs its own interpreter dependencies; the adapter runs it with `/usr/bin/python3`.

1. Write a manifest like `../../examples/companion-handoff/fixtures/host-manifest.example.json` (a template; fill in your own
   names and paths and keep the file outside this tree).
2. Create a disposable project with an instructions file, then run `roundtrip` with `--backend companion`. Use a throwaway
   `--registry` and `--project`; the companion's own handoff command writes into the project's plan pack.
3. Run `companions --companion MANIFEST` for the table. `70-discovery` is EXERCISED only from a `backend: companion`
   receipt with ack confirmed, `isolation: net-denied` and an identity equal to the current companion files.

The adapter never runs the companion's other commands (sync, interactive UI, scan, triage, review). It never writes into the
companion's own checkout: it works on a sandbox exported with `git archive`.

Descriptor claim: `planned`, not tested in a live session. Registry rows 21, 22 and 70 are `planned` and row 64 is `partial`; see `../../capabilities/registry.json` for the current state and what is missing.
Removal: delete `modules/discovery-inbox/`, the out dir and any sandbox directory you chose.
