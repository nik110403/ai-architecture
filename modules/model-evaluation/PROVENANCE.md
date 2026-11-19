# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` = sha256 of the source file
as read on 2026-11-06). Sources named `~/...` or `private operator repository, ...` are an author's installed files and
private operator-repository scripts; they are not shipped. `scripts/check_collection.py` validates this file, and
the module's own test suite runs that check on it because the collection walk covers only `plugins/` and `opencode/`.

## pool-reweight

Kit file: modules/model-evaluation/reweight.py
Source: private operator repository, bin/pool-reweight
Source-sha256: 7ed584c68d6d819af91ae83b8acbb23ebe1b291f6a16e28341d58a1f64f06ce7
State: re-authored

Deltas:
- the source counts a checker "win" for any parseable verdict and a researcher "win" for "completed with usage", which treats completion as quality; this port moves weights only on matched per-task score evidence (same tasks, same grader, enough pairs), recomputed from the rows, and outcome evidence moves nothing
- the source has a dry-run but no rollback; this port writes a plan with the pool, evidence and parameter hashes, `--apply` recomputes the plan and refuses (exit 4) on any difference, writes a 0600 backup first and replaces the pool atomically with the same mode, and `--rollback` restores the exact bytes only while the pool is still the applied one
- integer weights only (what both rotation hooks accept): held arms (pinned, disabled, weight 0, under `min_samples`) keep their weight, movable arms share the movable mass by largest remainder, shares are clamped to `[floor, cap]` by water-filling, infeasible parameters are refused
- the source's quota cut, which read provider cache files directly, is not ported and no quota cut is ever written into a pool; the usage-efficiency bonus is not ported

## model-scoreboard

Kit file: modules/model-evaluation/scoreboard.py
Source: private operator repository, bin/model-scoreboard
Source-sha256: 078b9a7da8a8cb63147d6055f9f53aa985bf6fb23a14b2d0bc6670101668306c
State: re-authored

Deltas:
- counts only (runs, verify outcomes, usage classes), arms in key order, `ranking: null` with a reason; no rate or score field
- the receipt reading and `usage_class` come from `skills/evaluation/scripts/receipt_report.py` and the checker-verdict join from `skills/evaluation/scripts/evaluation_report.py`, imported, not copied

## model-bakeoff

Kit file: modules/model-evaluation/bakeoff.py
Source: private operator repository, bin/model-bakeoff
Source-sha256: fd4bc3361f431fcffccc224cd51d6619f5b128ce7f8aec8cb67be2bba4fa4eaf
State: re-authored

Deltas:
- no model, harness or provider is called: the operator supplies an absolute-path runner; nothing starts without `--allow-spend`
- every run has a timeout; a non-zero exit, unusable output or a timeout leaves the run unscored and the evidence insufficient
- no usage or cost term enters a score or a ranking; a ranking is labelled `descriptive (no significance claim)`

## goldset-arms

Kit file: modules/model-evaluation/goldset.py
Source: private operator repository, bin/goldset-arms
Source-sha256: 550303bb84448b8a870073d357c05bc7f0268d6d61dd8da417fabeca7c40f0df
State: re-authored

Deltas:
- arms are listed from an answers file with their task counts; no arm definitions or model names ship

## goldset-score

Kit file: modules/model-evaluation/goldset.py
Source: private operator repository, bin/goldset-score
Source-sha256: ac2c8f448c2bb7c10580092eef7c2efddf487da4259ac511882e7bc3f4372510
State: re-authored

Deltas:
- deterministic exact-match scoring against operator-supplied gold answers; the grader id `goldset-exact-v1` is stamped on every score row so matching can require one grader

## Not ported

- effort-trial, style-parity, human-time, fixture-run: not ported (personal tuning experiments, outside this module's scope)
- the usage-efficiency bonus and the quota cut of the reweighter: not ported (usage and quota are not quality evidence)
- the private bake-off arm definitions and harness launchers: not ported (they start real models)
