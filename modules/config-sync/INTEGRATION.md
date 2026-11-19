# Integration, status, removal

Install: copy `modules/config-sync/`; for `--agents-dir` also keep `opencode/agents/opencode-agents-sync` at the
same relative path or pass `--opencode-sync`. Keep each manifest under version control, bump `manifest_version`
whenever it changes, and give every machine the same manifest.

Pass an explicit `--home` per run; the module never discovers a home or reads credential files. Export the
variables your MCP servers need in the shell that starts the harness; the manifest only names them.

Status: `claimed_support` is `planned` in `descriptor.json`; this module has no recorded live evidence.

Removal: delete `modules/config-sync/`; delete `$XDG_STATE_HOME/aiarch/config-sync/` to drop state and backups.
Managed keys and files stay in your configuration.
