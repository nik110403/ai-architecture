# Integration

## Pieces it uses

- The design pipeline's renderer (`../design-pipeline/render.py`), loaded by path and used read-only: headless Chrome through the
  browser-operations launcher, a same-origin Fetch gate, a sink proxy. `AIARCH_CHROME` chooses the binary and
  `AIARCH_RENDER_TIMEOUT` (seconds) bounds launch and navigation.
- The documents module's process runner (`../documents/documents.py`), for every FFmpeg, poppler, fontconfig, git and provider
  spawn (own session, group kill, allow-listed environment). `AIARCH_MEDIA_TOOL_TIMEOUT` (seconds) is the bound.
- The contracts module validates `descriptor.json` and `PROVENANCE.md` is checked by the collection script.

## Environment read

`PATH`, `HOME` (default out dir), `XDG_STATE_HOME`, `AIARCH_CHROME`, `AIARCH_MEDIA_TOOL_TIMEOUT`, `AIARCH_RENDER_TIMEOUT` and
`AIARCH_REQUIRE_MEDIA`. Child processes get `PATH`, `LANG`, `HOME` (a throwaway dir) and `TMPDIR` only; no key, token or other
variable is passed on.

## Using it

1. Check what is available: `media.py renderers --json`.
2. Build one kind: `media.py build --kind infographic --spec my-spec.json`; the result lists artifacts with sha256 and the named
   checks that passed. A failed inspection is exit 1 with the first failing reason.
3. Re-inspect later: `media.py inspect --kind K --spec S --artifact DIR`. It never reads the builder's notes.
4. Get the whole table: `media.py receipts --out DIR`, then `../../examples/media/verify_receipts.py DIR`, which checks the table
   structure before any render and then re-inspects every EXERCISED artifact.

## Adding a provider

Write a command that answers `--describe` with the seven fields in `interface.json`-style JSON (`interface`, `kind`, `network`,
`cost`, `licence`, `notice_required`, `output`) and, when run with `--prompt-file F --out OUT.png`, writes a PNG. List it in a
providers file (`name`, `argv`, `describe_argv`, absolute executable first). Run `illustrate ... --allow-provider`; add
`--allow-network` or `--allow-spend` only when you mean it.

## Registry

Registry rows 47-49 are `partial` (see `../../capabilities/registry.json`); `descriptor.json` claims `planned` with no evidence, because the contract accepts `partial` only with a native harness receipt. `receipts` sets a `conflict` flag on a row when its outcome disagrees with the registry state (EXERCISED while the registry says `planned`, or BLOCKED while it says `partial`); the registry is not edited here.
