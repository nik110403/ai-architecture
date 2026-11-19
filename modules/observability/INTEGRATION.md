# Integration, status, removal

Install: copy `modules/observability/`. `session` also needs `modules/continuity/` (it is called as a subprocess);
`spend` also needs `skills/evaluation/scripts/receipt_report.py`. Nothing else is required and nothing is wired
automatically.

Quota config (a JSON file you pass to `--config`; relative `path` values resolve against the config's directory):

```json
{"version": 1, "providers": {"my-provider": {"adapter": "snapshot-file", "path": "my-provider.json",
  "gate": true, "threshold_percent": 90, "max_age_seconds": 900}}}
```

You keep the snapshot file current with your own script:
`{"provider": "my-provider", "fetched_at": 1760000000, "windows": [{"name": "5h", "used_percent": 42, "resets_at": null}]}`.
A hook that wants to react to `quota gate` exits 3 when it prints `blocked`; every other outcome exits 0.

Remove: delete the directory. There is no state to clean up unless you passed `--out`.
