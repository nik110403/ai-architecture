# Integration, status, removal

Install: copy `modules/model-evaluation/`. It needs `skills/evaluation/scripts/` (receipt and verdict readers, imported) and,
for `--quota-config`, `modules/observability/`. Nothing is wired automatically and nothing runs on a timer.

Evidence file (`scores` rows; `role` names the pool role to reweight):

```json
{"version": 1, "role": "builder", "scores": [{"task_id": "t1", "arm": "provider/model-a", "grader": "goldset-exact-v1", "score": 1}]}
```

`bakeoff run` writes this file as evidence.json in `--out`. A runner receives `BAKEOFF_TASK` and `BAKEOFF_ARM` and prints
`{"answer": "..."}`. Outcome evidence (`{"outcomes": [...]}`) is accepted by the reader and moves nothing.

Typical use: dry-run, read the plan, apply, and keep the printed id for `--rollback`. Both rotation hooks validate the
result the same way the reweighter does (integer weights 0..1000000, valid names).

Remove: delete the directory and, if you used it, the `--out` directory (plans, backups, apply records).
