# research-adapter

A stdlib module that drives the public companion [provenote](https://github.com/nik110403/provenote)
(successor of research-kit, plugin `plugins/provenote`, tested version `0.2.x`) through its own
entrypoints: `<companion>/bin/lit-search` for discovery and `<companion>/bin/check-bib` for citation
checks. It makes no model call, copies no companion code, fixtures or bibliography, and touches no
network unless you ask it to.

It covers two registry capabilities as a handshake only: 34 research discovery and 35 citation
verification. The current state of rows 33-35 is in `../../capabilities/registry.json` (they need the companion, which is not part of this collection); the descriptor claims `planned` and this module has no recorded live evidence.

## Commands

```bash
python3 research_adapter.py handshake [--companion DIR] [--format text|json]
python3 research_adapter.py search --companion DIR --brief brief.json --out OUT (--fixture F | --allow-network) [--mailto ADDR]
python3 research_adapter.py cite --companion DIR --dois DOI... --bib refs.bib --paper paper.md --out OUT (--fixture F | --allow-network)
```

- `handshake` reads `<companion>/.claude-plugin/plugin.json` (a regular file, duplicate keys rejected) and
  reports `{companion, integrated, capabilities: {"34", "35"}}`. A capability is `available`,
  `unavailable: <exact requirement>` or `unsupported: version <v> (tested 0.2.x)`. `integrated` is true
  only when the name is `provenote`, the version matches `0.2.x` and both `bin/lit-search` and
  `bin/check-bib` are regular files (symlinks are refused). It never imports the companion.
- `search` takes a typed brief `{"question", "since", "n", "snowball"}` (`since` int or null, `n` int >= 1,
  `snowball` false, true or K >= 1). It runs the companion's `main` with `--out <OUT>/companion`
  (refuses a non-empty directory) and writes `<OUT>/notes/<n>-<slug>.md` plus `<OUT>/notes/index.json`
  with the companion's receipt path and sha256, `status` and `provider_status` passed through verbatim.
  A provider that is down shows as `UNAVAILABLE`, a failed snowball as `DEGRADED`; the exit code is the
  companion's (0 ok or partial, 1 nothing reachable and no notes are written).
- `cite` exports BibTeX through the companion's `--export-bib`, then runs `check-bib`. `<OUT>/citations.json`
  lists each DOI as `resolved`, `NOT RESOLVED` or `identity-mismatch` (the exported entry carries a
  different DOI than requested) with the citekey actually written to the bib. `verified` is true only when
  every DOI resolved with a matching identity and `check-bib` exited exactly 0 with a parsable summary.
  Exit 0 means verified; 1 means not verified (the artifact is still written); 3 means the companion
  cannot do `cite` (for example `bin/check-bib` missing).
- Fetched titles are untrusted data: notes carry `trust: untrusted fetched metadata`, file names use only
  `[a-z0-9-]` from the DOI or title, and nothing from the metadata is executed.

## Network permission

Offline by default. `search` and `cite` need exactly one of `--fixture FILE` (canned responses, no socket)
or `--allow-network` (the companion's own fetch). The network permission you grant with `--allow-network`
is the companion's three hosts: `api.openalex.org` (openalex), `api.semanticscholar.org`
(semanticscholar) and `doi.org` (BibTeX export). A fixture is `{"routes": [{"match": "<url substring>",
"body_file": "<file next to the fixture>"}]}` (or an inline `"body"`); the first matching route answers
and any other URL raises `no fixture route`, so an unmapped provider shows as UNAVAILABLE.

`--mailto ADDR` is forwarded to the companion only when given and is never written to any adapter output.

## Zotero

"Zotero" here means BibTeX interchange: what Zotero imports and exports, through `--export-bib` and
`check-bib`. No Zotero API or local Zotero database is touched.

## Tests and removal

`python3 -B -m unittest discover -s modules/research-adapter/tests` uses a contract stub companion. To run
the same flows against a real companion checkout, set `RESEARCH_COMPANION_DIR` to its
`plugins/provenote` directory. Removal: delete `modules/research-adapter/` and `examples/research-companion/`.
