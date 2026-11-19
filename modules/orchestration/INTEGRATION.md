# Integration, status, removal

Install: copy exactly these four paths, nothing else is needed (the role prompts and tools are read from
`agents/` in the same tree, so no per-harness role install is required):

- `modules/orchestration/`
- `modules/contracts/` (the typed `result` record and its validator)
- `adapters/runtime/`
- `agents/` (every role file, including the five generic ones: `surveyor`, `pipeline-runner`, `steward`,
  `design-builder`, `browser-worker`)

 Write your own config modelled on
`example.json`; keep it outside the repo if it names environment variables for credentials.

Status: `descriptor.json` claims `planned` with no evidence. Native evidence (a real worker run per harness) is
collected separately; do not raise the claim without a matching receipt.

Check: `python3 -B -m unittest discover -s modules/orchestration/tests -p 'test_dispatch*.py'`. The tests use fake
executables on a temporary `PATH` and never launch a real model.

Removal: delete `modules/orchestration/` and `adapters/runtime/`, drop the five role files, and remove their lines
from the catalog and the `ROLES` set in the collection checker in the same change.
