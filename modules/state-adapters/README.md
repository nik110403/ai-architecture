# state-adapters

`state_export.py` writes one versioned JSON export of registered projects. `viewer.py` is a static-HTML
viewer authored and maintained in this tree as a separate program: it reads only the export file and
never imports the exporter. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json`.

```
python3 -B modules/state-adapters/state_export.py export --registry <registry.json> --out <out>/state.json
python3 -B modules/state-adapters/viewer.py render --state <out>/state.json --out <out>/index.html
python3 -B modules/state-adapters/viewer.py list [--companion <manifest.json>] [--format json]
```

- The export holds, per project: id, root, resurvey status, lint finding counts by code, and per plan the
  topic, status and first `Next:` line under `## Resume here`. It records no hostname or username.
- `render` refuses an unknown `schema_version` (exit 2, writes nothing) and escapes every value.
- `list` shows the viewers. `static-html` is `available`. The `companion` slot is only ever reported
  (`unavailable` or `declared-unexercised`) and never executed. A manifest is
  `{"name": "...", "command": "..."}` with a bare command name.
- Capability 64 is `partial` in `../../capabilities/registry.json` (built-in static viewer only) and handshake 64 stays `proposed`, `blocked_until` live evidence: this
  in-tree viewer does not replace an externally supported viewer.
