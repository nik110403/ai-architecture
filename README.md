# AI Architecture Collection

A generic translation of a whole personal AI-agent operating system: routing,
bounded delegation, review, continuity, memory, maintenance loops, evaluation and
specialist workflows, packaged as reusable skills, roles, commands and offline tools
that keep AI-assisted work scoped, checkable, resumable, and honest about what the
harness actually does. This is a collection, not an autonomous-agent framework: adopt the whole
repository or copy one component directory into a project.

The collection's system map is [CAPABILITIES.md](CAPABILITIES.md); its source-grounded
journeys and boundaries are [WORKFLOWS.md](WORKFLOWS.md). They describe a broad
operating model—routing, bounded delegation, evidence, continuity, review, learning,
and specialist work—not a claim that this candidate implements all of it.

## Five-minute start

1. Run `python3 -B scripts/route.py --intent 'fix code test' --capabilities '{"capabilities":["code.builder","code.verifier"],"models":["builder","verifier"]}'` to get a report-only route.
2. Copy `skills/planning/` or a self-contained specialist skill intact using [INSTALL.md](INSTALL.md). This is release `0.3.0-preview`.
3. Give a worker `templates/worker-brief.md`; retain its result beside the work.
4. Have a fresh checker use `skills/convergence/` or `skills/review/`.
5. Record a data-only Next using `templates/lifecycle-record.md`.
6. For an explicit local Markdown bundle, run `python3 -B skills/discovery/scripts/memory_check.py --root PATH`; it only reports drift and scan gaps.

Start with [INSTALL.md](INSTALL.md), then choose a component in [CATALOG.md](CATALOG.md). Read [WORKFLOWS.md](WORKFLOWS.md) before whole-system adoption. The worked example is in [examples/worked-example/](examples/worked-example/).

## Layout and adoption

`skills/` supplies portable workflows; `agents/` supplies Claude-native role declarations; `commands/` are Claude-native entrypoints; `rules/` are guidance to merge manually with existing project rules. `templates/`, `adapters/`, `examples/`, and `scripts/` work without a hosted service. Each skill carries its own local asset when one is useful, so copying that directory does not require sibling skills.

Claude can discover plugin-root `skills/`, `agents/`, and `commands/`. Other harnesses should use [adapters/](adapters/) and manually adapt role contracts and routing. Native Claude commands and roles are not automatically imported by Codex or OpenCode.

## Scope and limits

There are no hooks, rotation systems, enforced gates, background agents, provider discovery, model-account policy, or permission grants here. `scripts/route.py` is stdlib-only, accepts explicit availability, and emits reporting states; manual mode is never native or enforced. Workflows describe a process; your repository and harness remain responsible for permissions and enforcement. Run the [system journey](examples/system-journey/README.md), [temporary-directory continuity fixture](examples/continuity/README.md), or [parser worked example](examples/worked-example/README.md) without a hosted model.

The system map is diagnostic, not whole-system runtime acceptance. It distinguishes
documented workflows from tools, adapters still required, missing ports, experimental
or source-stated capabilities, and intentionally excluded private/personal material.
The local preview adds report-only routing, modular lifecycle/review/operations
skills, specialist playbooks, and deterministic journeys. Whole-runtime parity remains
open: provider adapters, native enforcement, and unread families are still explicit
gaps. See [CAPABILITIES.md](CAPABILITIES.md#reading-status) and [WORKFLOWS.md](WORKFLOWS.md#what-this-does-not-prove).

## License and dependencies

Instruction assets and tools are MIT; explanatory documentation is CC BY 4.0 as mapped in [LICENSES.md](LICENSES.md). Core validation, the optional journal, and the report-only memory check use Python 3 standard library only. No third-party assets are bundled.
