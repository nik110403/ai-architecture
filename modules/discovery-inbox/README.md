# discovery-inbox: candidate, handoff and disposition against a discovery companion

An adapter that drives a discovery companion through that companion's own commands: a handoff command
(`--apply --targets-root`), a set command and a list command. One CLI: `discovery.py`. Stdlib only; no network code, no
model call. Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`; declared dependencies core-contracts, stewardship, state-adapters
and knowledge-adapters, all `declared-untested`.

- **You install the companion yourself.** The adapter drives a companion you install, through that companion's own
  commands. This collection ships no companion.
- **A fixture proves the adapter, not the lane.** The fixture companion in the tests proves the adapter's behaviour, not the
  lane. Only a real-companion receipt (`--backend companion`) makes `70-discovery` EXERCISED.
- **Who owns what.** The companion keeps candidate status, your plan pack keeps the work, and this module keeps receipts
  only (`discovery.jsonl`, derived evidence). `inbox list` always reads the disposition back from the companion. The module
  never writes a tracker, spec or questions line itself; only the companion's own `--apply` writes into the pack.
- **The network denial is a Python-level prelude, not a kernel namespace.** Every companion command runs as
  `python3 -I -c <denial> <script>` through the knowledge-adapters spawner (no shell, scrubbed environment, deadline, output
  cap). The receipt says `isolation: net-denied` only after a real connect attempt in the same run was refused.
- **Brain and external viewer are MISSING.** Row `22-brain` needs a backend that speaks the brain JSON handshake
  (`handshake` and `status` operations) and a permission file. Row `64-external-viewer` needs a viewer that reads a
  disposable state export (schema_version 1) without writing to a live service, described in a state-adapters companion
  manifest and passed with `--viewer-companion`. Neither ships here; both rows report MISSING with an owner and that setup.
- **Install, status, removal.** There is nothing to install. `discovery.py companions` gives the status. Removal means
  deleting the out dir; the companion's own placement is undone with the companion's tools.

## Manifest (`handoff-brief/1`)

Given explicitly with `--companion FILE`; a default location is never probed. Closed shape (unknown or duplicate keys, a
different interface, a partial disposition map, an absolute or `..` path: exit 2):

```json
{"schema_version": 1, "interface": "handoff-brief/1", "name": "token", "root": "/abs/path/to/checkout", "rev": "HEAD",
 "commands": {"handoff": "bin/handoff", "set": "bin/set", "list": "bin/list"}, "support": ["bin/shared_lib.py"],
 "dispositions": {"adopted": "integrated", "harvested": "harvested", "deferred": "deferred", "rejected": "skip"},
 "eligible_status": "next"}
```

`root` must be a git work tree. Only the declared files are exported with `git archive` into a disposable sandbox
(plus an empty `data/repos`); no ledger, data directory or report of the companion is copied. The identity in every receipt is
the git tree of each command dir plus the sha256 of each declared file. The contract (record keys, body labels, dispositions,
roles, the 64 KiB brief cap) is in `interface.json`.

## Commands

Common flags: `--out DIR` (0700; default under `$XDG_STATE_HOME/ai-architecture/discovery/runs/`), `--companion FILE`,
`--sandbox DIR`, `--registry FILE --project DIR [--targets-root DIR]`.

```bash
python3 -B modules/discovery-inbox/discovery.py roundtrip --companion M --registry R --project P --topic T --mode pack --out O
python3 -B modules/discovery-inbox/discovery.py inbox list --companion M --sandbox S --registry R --project P --out O
python3 -B modules/discovery-inbox/discovery.py decide --companion M --sandbox S --registry R --project P --id owner/name --disposition deferred --out O
python3 -B modules/discovery-inbox/discovery.py companions --out O [--companion M] [--graphify PATH] [--json]
```

`roundtrip` bootstraps the disposable project's pack with stewardship, writes one disposable candidate in the sandbox,
runs handoff twice (the second must report the companion's own `unchanged`), intakes the placed brief, sets the disposition
and reads it back with the companion's list command (otherwise exit 1 `disposition not confirmed`). Modes: `pack` (the
companion appends a handoff section to the pack's questions file), `inbox` (a copy under the project's inbox folder) and
`pseudo` (the companion's staged-only target: reported `not placed`, nothing to decide). The lower-level steps are
`sandbox`, `candidate`, `handoff`, `inbox list|intake` and `decide`.

- A repeated `decide` with the same disposition is a no-op with 0 spawns; a different one is exit 1 `already decided`.
- A changed candidate body makes the companion write a second file with a "new" suffix beside the original: reported as a `conflict` with
  both shas, never a second candidate, and `decide` refuses until it is resolved.
- A placed brief is data: capped at 64 KiB, only the `interface.json` labels are accepted (anything else is
  `unsupported brief`), control characters are escaped, nothing is executed or followed, and file names must match the
  owner__name pattern (with an md ending).

## Receipt table

`companions` prints one row per sub-capability: `21-graph`, `22-brain`, `64-static-viewer`, `64-external-viewer`,
`70-discovery`. Each is EXERCISED or MISSING with an owner, a reason, the registry claim and a `conflict` when the registry
claim and the observation disagree (it only reports; it never edits the registry). Rows are derived by running the owning
module's own commands (`../knowledge-adapters/knowledge.py` exercised, `../state-adapters/viewer.py` list, then `../state-adapters/state_export.py` and the viewer render), never copied.
Exit codes: 0 complete table, 1 invalid (a row without an owner), 2 refused, 3 when `AIARCH_REQUIRE_COMPANION=1` and
`70-discovery` is MISSING.

Run the tests with `python3 -B -m unittest discover -s modules/discovery-inbox/tests`. The real-companion cell runs only
when `AIARCH_DISCOVERY_MANIFEST` names a manifest outside the tree; otherwise it prints `NOT RUN`. See `INTEGRATION.md` and
`PROVENANCE.md`.
