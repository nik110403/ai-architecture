# AI Architecture Collection

Reusable, project-local instruction assets for teams that need AI-assisted work to remain scoped, checkable, and resumable. It is a collection, not an autonomous agent framework: adopt the whole repository or copy one component directory into a project.

## Five-minute start

1. Copy `skills/planning/` intact into a selected project-local skill directory using [INSTALL.md](INSTALL.md), or locally load the candidate as a Claude plugin. **0.2.0 candidate NOT PUBLISHED**; the current remote is the old preview.
2. Copy `templates/plan.md` and write a small plan with acceptance criteria.
3. Give a worker `templates/worker-brief.md`; retain its result beside the work.
4. Have a fresh checker use `skills/review/` and `templates/review.md`.
5. Record the next executable action using `templates/resume.md`.

Start with [INSTALL.md](INSTALL.md), then choose a component in [CATALOG.md](CATALOG.md). The worked example is in [examples/worked-example/](examples/worked-example/).

## Layout and adoption

`skills/` supplies portable workflows; `agents/` supplies Claude-native role declarations; `commands/` are Claude-native entrypoints; `rules/` are guidance to merge manually with existing project rules. `templates/`, `adapters/`, `examples/`, and `scripts/` work without a hosted service. Each skill carries its own local asset when one is useful, so copying that directory does not require sibling skills.

Claude can discover plugin-root `skills/`, `agents/`, and `commands/`. Other harnesses should use [adapters/](adapters/) and manually adapt role contracts and routing. Native Claude commands and roles are not automatically imported by Codex or OpenCode.

## Scope and limits

There are no hooks, rotation systems, enforced gates, background agents, model policy, or permission grants here. Workflows describe a process; your repository and harness remain responsible for permissions and enforcement. The optional continuity journal is a stdlib command-line utility, not a service. Run the [temporary-directory continuity fixture](examples/continuity/README.md) or the [parser worked example](examples/worked-example/README.md) without a hosted model.

## License and dependencies

Instruction assets and tools are MIT; explanatory documentation is CC BY 4.0 as mapped in [LICENSES.md](LICENSES.md). Core validation and the optional journal use Python 3 standard library only. No third-party assets are bundled.
