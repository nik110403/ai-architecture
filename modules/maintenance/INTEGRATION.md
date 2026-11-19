# Integration, status, removal

Install: copy `modules/maintenance/`. Nothing else is needed. For the Markdown table workflow keep
[`tools/loop-sync`](../../tools/loop-sync/README.md), which gained an additive `--format json`.

Pick the registry path and every `--unit-dir`, `--crontab-file` and `--out` yourself; the module never
discovers a scheduler location. Nothing here enables a unit unless you pass `--enable`.

Status: `claimed_support` is `planned` in `descriptor.json`; this module has no recorded live evidence.

Removal: delete `modules/maintenance/`. Unit files it wrote stay until `scheduler rollback` removes them.
