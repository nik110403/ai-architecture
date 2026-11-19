# companion-handoff: the discovery roundtrip and the receipt table

Two offline scripts for the `discovery-inbox` module (see `../../modules/discovery-inbox/README.md`).

```bash
python3 -B examples/companion-handoff/run.py --offline --out DIR
python3 -B examples/companion-handoff/verify_receipts.py
```

- `run.py --offline --out DIR` builds a disposable project and a fixture companion (an authored stand-in kept with the
  module's tests) in a temp dir, puts one candidate through handoff into the project's plan pack, reads it from the inbox, records the
  disposition `deferred` and confirms it with the companion's own list command. DIR receives only the receipt. Without `--offline`
  it exits 2 and writes nothing.
- `verify_receipts.py` runs that roundtrip, then prints the five-row table: `21-graph`, `22-brain`, `64-static-viewer`,
  `64-external-viewer`, `70-discovery`. With only the fixture, `70-discovery` is MISSING. Set `AIARCH_DISCOVERY_MANIFEST` to a
  manifest for a companion you installed (template: `fixtures/host-manifest.example.json`) and it also runs a real roundtrip
  in pack and inbox mode; then `70-discovery` can be EXERCISED. `AIARCH_REQUIRE_COMPANION=1` turns a MISSING row into exit 3.

What to know:

- The adapter drives a companion you install yourself, through that companion's own commands. This collection ships no companion.
- The fixture companion proves the adapter's behaviour, not the lane. Only a real-companion receipt makes `70-discovery` EXERCISED.
- The companion keeps candidate status, your plan pack keeps the work, and the module keeps receipts only.
- The network denial is a Python-level prelude, not a kernel namespace.
- The brain and external-viewer rows are MISSING. Brain: a backend that speaks the brain JSON handshake plus a permission
  file. External viewer: a viewer that reads a disposable state export (schema_version 1) without writing to a live service,
  described in a state-adapters companion manifest.
- Install, status, removal: there is nothing to install; `companions` gives the status; removal means deleting the out dir.
  The companion's own placement is undone with the companion's tools.
