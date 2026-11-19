# development

Artifact contracts and a checker for five development journeys: **debug** (reproduce, failing test, verified fix, with a reuse and dependency decision), **domain** (a glossary-versus-code contradiction plus one scoped improvement), **secure-build** (new code written against named threats), **security-audit** (existing code, with confirmed exploit, deployment-testing and hardening statuses) and **incident** (one reversible change per measurement). It is not a test framework and adds no role: it uses the existing `builder`, `verifier`, `surveyor`, `refuter`, `tribune` and `security-reviewer` roles through `../orchestration/dispatch.py`.

```bash
python3 -B modules/development/development.py check --kind debug ARTIFACT --run RUN_DIR   # 0 pass, 1 fail (reason), 2 refused
python3 -B modules/development/development.py deps [--skills-dir DIR] [--json]
python3 -B modules/development/development.py verify-receipt RECEIPT [--run RUN_DIR]
python3 -B -m unittest discover -s modules/development/tests
```

## What a pass means

`check` never trusts what an artifact says. It re-runs the evidence itself: it extracts the recorded commit with `git archive`, runs the fixture script as `<this python> -B -I <script>` in a fresh process, and compares the result with the claim. Strings from an artifact are data; argv comes from `journeys.json` and from 40-hex commit ids. Paths in artifacts are run-relative and may not leave the run directory.

| Journey | Re-executed by `check` |
|---|---|
| debug | the repro at the base commit; the test at the red commit (fails with an `AssertionError` for the recorded test id) and the green commit (passes); the test file is byte-identical; diff within the declared scope; no import outside the standard library unless declared; named `reuse` helpers exist at base and are used; the verifier ran in its own worktree whose HEAD is the green commit and which is clean |
| domain | the scenario (glossary and code disagree); the cited file:line contain the term; the refuter is a different job from the maker and echoes the sha of this finding; one improvement file with a named acceptance check |
| secure-build | every attack (traversal, absolute path, runtime symlink escape) is refused at the build commit and succeeds at the base commit; the benign request is served; the security-reviewer verdict is bound to the build commit |
| security-audit | every finding is replayed: `confirmed` needs the canary observed in this replay, a decoy with a blocked path stays `hardening`; an empty attack scenario is no finding; each finding has its own refuter verdict |
| incident | log lines by sha; at least two candidates; a tribune ranking; before and after metrics re-measured; exactly one change per measurement; the prerequisite re-checked; the rollback restores the base tree byte-for-byte; no third change after two failures |

Job records (`jobs` in each artifact) are compared with the dispatch run files (the result, start and stdout files under `dispatch/<job_id>/` in the run). A label alone (a route report, a dispatch plan, a roles.json intent match, trigger text, or a `succeeded` envelope without an artifact) fails with `no-artifact-evidence`; it cannot close the secure-build, audit or incident journeys.

`verify-receipt` loads each artifact by its sha, requires exactly one artifact per journey, requires the secure-build, security-audit and incident artifacts to share no job id and no input file, and re-runs `check` for each in a fresh process.

## Upstream skills degrade explicitly

Only `superpowers` 7.0.0 (MIT, not vendored) is a licensed source (`../contracts/sources.json`). Every other upstream is a generic slot with an unresolved license. `deps` reports `unavailable` or `present-untested` and the effect `generic contract only`; it checks that a skill file exists and never reads it. Nothing probes a default skills location. An artifact claiming `used` or `available` fails, and receipts say `upstream: not exercised`.

## Limits

- The workers in `examples/development` are authored stand-ins; no model judgement is exercised. A live-model journey is NOT RUN.
- The `local-command` seat is not a sandbox (see the threat model in `../orchestration/README.md`).
- The fixtures are intentionally vulnerable; never import them.
- A receipt is evidence against mistakes, not against a same-user forger: anyone who can write the run directory can rewrite the artifacts, the repos and the receipt together.
- Registry rows 41-44 are `partial` (authored stand-in workers, see `../../capabilities/registry.json`); the descriptor still claims `planned` with no evidence, because the contract accepts `partial` only with a native harness receipt.
