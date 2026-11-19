# Media example

Builds and inspects every media obligation offline from the synthetic specs in `specs/`, then prints one line each.
Each adapter is optional and lists its exact dependencies (see `../../modules/media/README.md`); a missing one blocks only
that adapter's rows. Renders use the Chrome and FFmpeg you install yourself; this collection ships no renderer, no font and no
provider.

```bash
python3 -B examples/media/run.py --offline [--out DIR] [--format text|json]
python3 -B examples/media/verify_receipts.py [OUT_DIR] [--keep DIR] [--only KIND,...]
```

`run.py` refuses to run without `--offline` (exit 2) and writes nothing then. It runs `modules/media/media.py receipts` with the
stub provider declared in `providers.json`: slides, infographic, visual-plan, visual-recap (a disposable repository of
synthetic files), system-atlas and video are each rendered by Chrome (and FFmpeg for the video), inspected, and listed as
EXERCISED, or BLOCKED with a reason when a tool is missing. Turning a kind off never hides it. The illustration row stays
BLOCKED with class `provider`; an extra `interface: fixture` line shows the stub's result.

The stub provider proves the interface, not a generation. A real illustration needs a provider you configure, and network or
cost use needs explicit flags (`--allow-network`, `--allow-spend`) that this example never passes.

`verify_receipts.py` with no argument runs the example into a temp dir, then reads the table back without trusting it: it
checks the seven rows, the reason vocabulary, fixtures and artifact hashes first (no browser), and then inspects every
EXERCISED artifact again from its bytes. It prints the seven-row table and the renderer rows. Exit 0 ok, 1 tampered or failed,
2 invalid, 3 unavailable. With `AIARCH_REQUIRE_MEDIA=1` a missing Chrome, FFmpeg or poppler fails instead of blocking.

## Limits

- The video has no audio, narration or loudness, and no aesthetic review (the design pipeline owns critique).
- The builders are minimal and generic; the upstream design languages are not used.
- The upstream engines (a deck runtime, a code-video engine, a browser automation driver, a hosted plan service) are listed as
  blocked renderers, not run.
- Nothing to install; removal means deleting the out directory.
