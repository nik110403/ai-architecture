# media

Slides, four kinds of visual, a short code-driven video, and an illustration-provider interface, each as its own optional
adapter. Every artifact is built from a small JSON spec, rendered by a real tool, and then inspected again by code that reads
only the artifact's bytes, the spec and (for a recap) the repository. Stdlib only, offline.

```bash
python3 -B modules/media/media.py renderers [--providers P --allow-provider] --json
python3 -B modules/media/media.py build --kind slides|infographic|visual-plan|visual-recap|system-atlas|video --spec S.json [--out DIR]
python3 -B modules/media/media.py inspect --kind K --spec S.json --artifact DIR      # re-inspect an artifact from its bytes
python3 -B modules/media/media.py illustrate --provider NAME --providers P.json --prompt-file F --allow-provider
python3 -B modules/media/media.py receipts [--out DIR] [--enable K,...] --json       # the seven-row table + renderer rows
python3 -B -m unittest discover -s modules/media/tests && python3 -B examples/media/verify_receipts.py
```

Exit codes: 0 ok, 1 inspection failed or tampered, 2 refused or invalid, 3 renderer or tool unavailable, 4 provider refused.
`--format json` (or `--json`) always prints an object with `status` and `reason`.

## Optional adapters and their exact dependencies

Each adapter is optional and lists what it needs in `adapters/`. A missing dependency blocks only that adapter's rows; every
other adapter still runs. Nothing is installed for you.

| Adapter | Kinds | Needs |
|---|---|---|
| slides | slides (HTML deck, PDF, PNG of slide 1) | Chrome (headless, `Page.printToPDF`), `pdfinfo`, `pdftotext`, `pdffonts`, the font family DejaVu Sans |
| visuals | infographic, visual-plan, visual-recap, system-atlas | Chrome, the font family DejaVu Sans, and `git` for the recap only |
| video | video (H.264 mp4) | Chrome, FFmpeg with libx264 and yuv420p, `ffprobe`, the font family DejaVu Sans |
| illustration | illustration | a provider command you configure (this collection ships only a stub) |

Renders use the Chrome and FFmpeg you install yourself; this collection ships no renderer, no font and no provider. Chrome is
driven through the design pipeline's renderer (the browser-operations launcher, a Fetch gate that fails every request outside
the page's own origin, and a sink proxy that counts what still tries to leave). FFmpeg is called with
`-protocol_whitelist file`, so no frame can pull a URL. Every spawned tool runs in its own session with an allow-listed
environment and is killed as a group on timeout (`AIARCH_MEDIA_TOOL_TIMEOUT`, seconds).

## What the table says

`receipts` always prints exactly seven obligation rows in this order: slides, infographic, visual-plan, visual-recap,
system-atlas, video, illustration. Every row is EXERCISED from a real render that was inspected, or BLOCKED with a reason from
the closed vocabulary in `interface.json`; turning a kind off never hides it (it becomes BLOCKED with class `not-enabled`).
`renderers` lists every inventoried renderer: the observed ones (`chrome-html`, `chrome-pdf`, `pdf-inspect`,
`ffmpeg-encode`, `ffprobe-inspect`) and the upstream engines that are not shipped or not run (`remotion`, `deck-runtime`,
`playwright`, `hosted-plan-service`, `atlas-build`, `infographic-render`, `plan-local-files`, `illustration-backends`), each
with a fixed reason class and what you would have to set up. Each obligation row names those upstream renderers in
`upstream`, so a green row from this module's own generic builder is never read as an upstream engine having run.

A row is EXERCISED only when the backend is real, every check passed, each artifact's sha256 matches its file, and the
renderer and tools have a version and an executable hash. A renderer whose executable lies inside this collection is a
fixture, and a fixture row is never EXERCISED. `AIARCH_REQUIRE_MEDIA=1` turns a host or not-enabled blocker into exit 3 (the
illustration provider blocker alone stays exit 0).

## Illustration

The stub provider proves the interface, not a generation. A provider is a command with a `--describe` step; only that output
is trusted (`network`, `cost`, `kind`, licence). Without `--allow-provider` nothing is spawned; a provider that needs network
or money is refused unless `--allow-network` or `--allow-spend` is given, and every refusal comes before the generate command
runs. A real illustration needs a provider you configure and explicit flags; the example never passes them, so the
illustration obligation stays BLOCKED (class `provider`). Its licence record is read back from `PROVENANCE.md` (the upstream
licence and notice units) and says `licence_gate: pending U02 row` until the collection's source licence record has a row.

## Untrusted specs

A spec is data, at most 256 KiB, with no duplicate or unknown keys. Assets (`logo`) must be a PNG or SVG inside the spec's own
directory (no `..`, no absolute path, no link, no URL). Every string is HTML-escaped; colours are `#rrggbb` only. No artifact
holds a host path, a user name, a host name, an email address or a corpus file; a hit is `host path in artifact` (exit 1).

## What the video adapter does not do

No audio, narration, loudness or aesthetic review. The design pipeline owns critique. The video is CSS keyframes stepped frame
by frame, encoded by FFmpeg, and checked by decoding frames back and comparing them with a fresh render.

## Install, status, removal

There is nothing to install. `receipts` gives the status; removal means deleting the out directory
(`$XDG_STATE_HOME/ai-architecture/media/runs/<id>`, mode 0700, never a symlink or someone else's). See `INTEGRATION.md`.
