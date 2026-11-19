# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` is the sha256 of the upstream
file as read when this module was written). One unit per hashed upstream file. Every unit is `re-authored`: no upstream text,
list, template, palette, type scale or prompt was copied, and a 12-word-shingle comparison of both directories against every
file below found 0 shared shingles. The `Source:` values are logical names of files in an author's installed skill libraries;
they are not shipped. `scripts/check_collection.py` validates this file. The illustration skill's licence unit is read back at
run time by `illustration.py` to produce the licence record shown by `media.py renderers`.

## slides skill

Source: operator skill library, slides SKILL.md (deck build and PDF workflow)
Source-sha256: 46fad5d196abe666058915ea82b1809a4c8497f7d58831c53bd413b674be4102
State: re-authored

License seen at the source: unresolved (operator-authored).

Deltas:
- interface only: the idea of a deck directory that is built, rendered to a PDF and checked; the build is a static HTML page printed by headless Chrome, not a JavaScript deck runtime
- the checks (page count, page size, per-page text, fonts) are authored here and run with poppler tools

## slide-authoring reference

Source: operator skill library, slides slide-authoring reference (canvas and page contract)
Source-sha256: 81e1550cdb61dd3856a03dc7528655fc07d258fd05bdf7e6d38e42b0f829f2cb
State: re-authored

License seen at the source: unresolved.

Deltas:
- interface only: a fixed 16:9 canvas with one page per slide; the canvas size here is 1920 by 1080 CSS pixels and the type scale, palette and transitions are not carried over
- the declared font is the single family DejaVu Sans, checked against the browser's and the PDF's real fonts

## infographic skill

Source: epic-infographics SKILL.md (third-party; the collection's source licence record marks it excluded, package-only licence)
Source-sha256: b1c9ed579c82e462ab8361ed413a3ca0e883515f2edc68dc954824bf067f003f
State: re-authored

License seen at the source: MIT, package only; redistribution blocked by the collection's source record.

Deltas:
- interface only: a spec of labelled values becomes a PNG of an exact size; no design language, template, palette, copy rule or animation is reused
- the builder is a minimal generic bar chart whose geometry is checked from the rendered page

## infographic render script

Source: epic-infographics scripts/render.mjs (interface only: HTML to PNG)
Source-sha256: 693f1207d7855fc59c632913c1df703a80455fa5b4b1def7decae447a662a2ac
State: re-authored

License seen at the source: MIT, package only; redistribution blocked by the collection's source record.

Deltas:
- interface only: render a page at a fixed viewport and save a PNG; authored here on top of the design pipeline's Chrome renderer, with no Playwright dependency

## infographic check script

Source: epic-infographics scripts/check.mjs (interface only: size check)
Source-sha256: 1d046563aea28941f206c577d0f41fde9fe60e4cd6b81791bb0e3f851f74a5fc
State: re-authored

License seen at the source: MIT, package only; redistribution blocked by the collection's source record.

Deltas:
- interface only: the PNG must be valid, of the exact size and not one flat colour; the check is the design pipeline's PNG reader, plus DOM and pixel checks authored here

## visual plan skill

Source: visual-plan SKILL.md (third-party, local-files mode)
Source-sha256: 2cd574b11bd830cf6b3529d6efb2376ca01fc0f1b941315878ef9e5c996ae4f2
State: re-authored

License seen at the source: unresolved.

Deltas:
- interface only: a plan of steps with dependencies and a file map is drawn as a picture; the hosted publishing path is not used and not ported
- nodes, edges and the file-map rows are recomputed from the rendered page and compared with the spec

## visual recap skill

Source: visual-recap SKILL.md (third-party, local-files mode)
Source-sha256: 0ee0d8e401d005c0584f25ef1a17e4a941f205165528133601c2ba457aa9535f
State: re-authored

License seen at the source: unresolved.

Deltas:
- interface only: a commit range becomes a picture of changed files and commit subjects; only committed, tracked paths of the range appear, never authors, emails, absolute paths or untracked files
- the per-file counts are recomputed from the repository by the inspector, not read from the builder

## system atlas skill

Source: operator skill library, system-atlas SKILL.md
Source-sha256: 3c1da5ce45fca069a31ff26f2f29371356099476dd023474e1d8aab54fbb7e7c
State: re-authored

License seen at the source: unresolved.

Deltas:
- interface only: nodes, edges and loops are drawn as a diagram with a plain-text twin that lists the same facts, one line each, sorted
- no isometric look, chapters or animation are carried over

## system atlas template

Source: operator skill library, system-atlas assets template
Source-sha256: 503f1697649046820bc47e9e820a67c78a275adcfc257d7dce75919b1e9a789f
State: re-authored

License seen at the source: unresolved.

Deltas:
- not used: the diagram is a generic SVG grid authored here; nothing of the template's markup, geometry or styling is reused

## system atlas build script

Source: operator skill library, system-atlas assets build script
Source-sha256: a8d9ea427f41cdc1a5dc4afb6306b9c3e533baf5228635bd862e5a67f4e3c08b
State: re-authored

License seen at the source: unresolved.

Deltas:
- not used: the upstream build runs under a JavaScript runtime; this module builds the page in Python and renders it in Chrome

## video skill

Source: operator skill library, video SKILL.md (bounded render and technical QA)
Source-sha256: 3985a8eb3e770bf8bcbf4feb6134665f94e1442b1bc04ea97d8cd58f46347a1f
State: re-authored

License seen at the source: unresolved.

Deltas:
- interface only: a bounded render followed by a technical inspection; the upstream engine, its project scaffold, profiles, voice and review tooling are not used
- the video here is CSS keyframes stepped frame by frame in Chrome and encoded with FFmpeg; audio, narration and loudness are out of scope

## video toolkit contract

Source: operator skill library, video toolkit reference
Source-sha256: 4de300ca57e0738ebaa5df096ae66a2f57a731c84067ecdb075017d9af1b405e
State: re-authored

License seen at the source: unresolved.

Deltas:
- interface only: probe the encoded file (codec, size, pixel format, frame count) and decode frames back; authored here with ffprobe, a container walk and frame comparison

## illustration skill

Source: illo 0.23.5 SKILL.md (third-party, public upstream tmchow/illo-skill)
Source-sha256: a6bd904023d73c1f425093834cee58f4ceca0c492550b54e2fce67e3404c9cb3
State: re-authored

License seen at the source: MIT, with a NOTICE file.

Deltas:
- interface only: an illustration provider is a command with a describe step; the skill's prompts, mascot character, print looks and key handling are not ported
- nothing is installed or enabled by this collection; a provider is configured by the user

## illustration backends reference

Source: illo 0.23.5 references/backends.md (third-party, public upstream tmchow/illo-skill)
Source-sha256: a31463a7f42a652138a97d4a239092d7bd5702bc5f6a576adf4b92f81f2a6142
State: re-authored

License seen at the source: MIT, with a NOTICE file.

Deltas:
- interface only: the facts that matter are that a backend needs network and either quota or a paid key; the interface records network and cost and refuses both unless explicitly allowed
- the backend list, commands and configuration files are not ported

## illo LICENSE

Source: illo 0.23.5 LICENSE (third-party, public upstream tmchow/illo-skill)
Source-sha256: d5c6b57f6897b3595086e1483e3dfdc2ebc9e2c00071c8a622b570f2f2a8835c
State: re-authored

License seen at the source: MIT.

Deltas:
- recorded only: the licence is read back at run time as part of the illustration licence record; no upstream text is copied into this collection
- the collection's source licence record has no row for this skill yet (licence gate pending)

## illo NOTICE

Source: illo 0.23.5 NOTICE (third-party, public upstream tmchow/illo-skill)
Source-sha256: a741308693a22d518031ff0b1837fc8975fcf01887cd84b718b87c3aa7fc01fd
State: re-authored

License seen at the source: NOTICE file accompanying the MIT licence.

Deltas:
- recorded only: a NOTICE is required wherever the upstream is redistributed; nothing from it is redistributed here, so the record notes the requirement for a user who adds a provider

## Not ported

- the infographic design language, templates, palettes and animation script
- the slide-authoring type scale, palette and transitions
- the system atlas isometric template and its chapters and packet animation
- the hosted plan and recap publishing and its service address
- the code-video engine, its scaffold, profiles, voice and speech-recognition parts, and the review-packet tooling
- the React deck runtime, its build tool and its browser PDF driver
- the illustration skill's prompts, mascot character, print looks, backends and its configuration and key handling
- every upstream step that dispatches a critic (the design pipeline owns critique)
