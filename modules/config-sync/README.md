# config-sync

A versioned manifest of MCP servers, skills and agents with drift detection, backup-first apply, rollback and
reviewed reconciliation. Stdlib only; no network, no model. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json` (no live evidence yet).

```
python3 -B modules/config-sync/config_sync.py validate  --manifest M
python3 -B modules/config-sync/config_sync.py status    --manifest M --home <home> [--agents-dir D]
python3 -B modules/config-sync/config_sync.py apply     --manifest M --home <home> [--apply]
python3 -B modules/config-sync/config_sync.py rollback  --home <home> [--apply]
python3 -B modules/config-sync/config_sync.py reconcile --a M1 --b M2 --out P
```

Manifest: `{"schema_version":1,"manifest_version":N,"entries":[...]}`. Entries are
`{"kind":"mcp","id","harness":"claude|opencode","name","command"|"url","args":[...],"env":["NAME",...],"headers":{"Header":"NAME"}}`
(rendered into `<home>/.mcp.json` `mcpServers` or `<home>/opencode.json` `mcp`) and
`{"kind":"tree","id","dest","files":{"relative/path":"text"}}` (skills and agents under `<home>/<dest>`).

- `status` and `apply` without `--apply` write nothing. Per key or file the verdict is `ok`, `missing`, `outdated`
  (matches the last applied state, manifest is newer), `conflict` (differs and was not last applied by this tool,
  including a differing value with no record) or `broken` (malformed target JSON). Any verdict but `ok` is exit 1;
  `apply` refuses `broken` (exit 3) and `conflict` (exit 1) and writes nothing in either case. Keys and files the
  manifest does not name are never touched.
- Output prints key paths and 12-character hashes, never values.
- `apply --apply` copies every touched file into `<state>/backups/NNNN/` first, then writes each target atomically.
  If a write fails the already written targets are restored and the backup stays. `rollback --apply` restores the
  newest snapshot, but only if the files still match what apply wrote.
- State lives at `$XDG_STATE_HOME/aiarch/config-sync/<sha256(realpath home)>/` (`<state>/applied.json`, `<state>/history.json`,
  `<state>/backups/`), mode 0600 in 0700 directories. Backups are full copies of the touched files.
- Every target must resolve (symlinks followed) under `--home`, else exit 2.
- Env entries are variable names. Apply writes `${NAME}` into `<home>/.mcp.json` and `{env:NAME}` into `<home>/opencode.json`.
  A literal value in `env`, `headers`, URL userinfo or query, or an argument like `--api-key=...` exits 2. Files named
  like credentials (for example auth, credentials and dotenv files) are refused in tree entries and never read.
- `--agents-dir D` also compares the `agent` block of `<home>/opencode.json` with the output of
  `opencode/agents/opencode-agents-sync --agents-dir D` (structurally, ignoring each entry's `model`) and prints
  `drift: opencode agents` on a difference. It never edits that file.
- `reconcile` compares two manifests: the same `manifest_version` with different entries is a `version-conflict`
  (exit 1). It writes a proposed manifest plus the diff to `--out` and applies nothing; apply it yourself with
  `apply --manifest P` after review.
