# model-evaluation

Evidence joins, scoreboard, bake-off, goldset and a guarded pool reweighter. Stdlib only, Python 3, Linux. No network;
nothing starts a model, harness or provider except the runner you pass to `bakeoff run --allow-spend`.
Claimed support: `planned` (see `descriptor.json`).

```
python3 -B modules/model-evaluation/evaluate.py scoreboard --receipts receipts.jsonl [--scores evidence.json]
python3 -B modules/model-evaluation/evaluate.py goldset score --gold gold.json --answers answers.json
python3 -B modules/model-evaluation/evaluate.py bakeoff run --role builder --arm A --arm B --task t1 --task t2 \
    --gold gold.json --out DIR --allow-spend --runner-cmd /abs/path/runner
python3 -B modules/model-evaluation/evaluate.py reweight --pool pool.json --evidence evidence.json --out DIR --dry-run
python3 -B modules/model-evaluation/evaluate.py reweight --pool pool.json --evidence evidence.json --out DIR --apply DIR/plans/ID.json
python3 -B modules/model-evaluation/evaluate.py reweight --pool pool.json --out DIR --rollback ID
```

Exit: 0 ok, 2 refused or bad usage, 4 pool, evidence or plan no longer agree (nothing changed).

## What it claims

- Task completion is not model quality. Completion and verdict counts are `outcome` evidence: the scoreboard shows counts
  per harness, role and model, in key order, with `ranking: null`. A ranking exists only on **matched** evidence: every arm
  scored on the same task ids by the same single grader, at least `min_pairs` (default 5) tasks, no unscored run. It is labelled
  `descriptive (no significance claim)` and carries per-arm n, mean and paired win counts. Matching is recomputed from the
  per-task score rows; a status flag in a file is ignored, but a declared `evidence_class` other than `matched` vetoes
  the file (no ranking, every arm held). Score rows and grader ids are trusted as given: the tool cannot tell a goldset
  score from a completion flag.
- The reweighter reads a rotation pool (`plugins/gates/hooks/rotation`, `opencode/rotation`) and changes integer `weight`
  values of one role, only on matched evidence. Arms with `pinned: true` (a key only this tool reads; the hooks ignore it),
  `enabled: false`, weight 0, or fewer than `--min-samples` paired tasks keep their weight. The others share their combined
  weight in proportion to their mean score, within `[--floor, --cap]` (defaults 0.05 and 0.6), rounded by largest remainder.
  Infeasible limits are refused. A brief's `pin_arm` keeps working after any reweight.
- `--dry-run` writes a plan (`pool_sha256`, `evidence_sha256`, `params`, `before`, `after`, `shares`, `held`, `reasons`,
  `plan_sha256`) into `--out` and leaves the pool untouched. `--apply` recomputes the plan from the pool, evidence and
  parameters and refuses (exit 4) on any difference, including a pool edited since the dry-run, writes a 0600 backup, then
  replaces the pool atomically with the same mode. `--rollback ID` restores the exact bytes and refuses if the pool changed
  after the apply.
- Quota is gate-time state: the reweighter never writes a quota cut into a pool. `--quota-config` only reports provider
  states next to the dry-run plan.

## Not claimed

No result here says one model is better in general: the ranking is descriptive over the tasks you scored. Weights are not
tuned for cost. `--pool` and `--out` are always required: there is no default pool and no scheduler (a timer is yours to add).

Tests: `python3 -B -m unittest discover -s modules/model-evaluation/tests` (needs `bun` for the OpenCode hook check).
