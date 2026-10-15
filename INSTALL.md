# Install

This release identifies itself as `0.3.0-preview` in its manifest. The marketplace
commands below install it; local loading works the same way and is described below.
What each capability does and does not cover is in [CAPABILITIES.md](CAPABILITIES.md).

## Claude Code

The repository root is the marketplace root:

```text
/plugin marketplace add nik110403/ai-architecture
/plugin install ai-architecture@ai-architecture
```

For the local candidate, replace `PATH` below with the absolute collection directory
(not the recipient project). Local loading is not a global install:

```sh
claude --plugin-dir PATH
claude plugin validate --strict PATH
claude plugin validate --strict PATH/.claude-plugin/plugin.json
claude plugin validate --strict PATH/.claude-plugin/marketplace.json
```

Use `/ai-architecture:plan`, `/ai-architecture:execute`, `/ai-architecture:review`,
`/ai-architecture:checkpoint`, `/ai-architecture:research`, or `/ai-architecture:health`.
Plugin-local command references use `${CLAUDE_PLUGIN_ROOT}` so health checks target
the collection rather than the user's project. Commands grant no permissions.
Plugin-root skills, roles and commands use Claude metadata; native loading remains
unverified here. Manually merge chosen rules; do not overwrite settings or rules.

## Selective skill adoption

Choose one skill from [CATALOG.md](CATALOG.md). Copy the **entire**
`skills/<name>` directory, including assets and scripts, to one project-local destination.
Some skills deliberately declare a small root-level record/tool bundle in their local
dependency manifest; copy that exact bundle too. The routing bundle is
`scripts/route.py` plus `templates/route-report.md`; it is optional for manual use:

| Harness | Destination |
| --- | --- |
| Claude Code | `.claude/skills/<name>` |
| Codex | `.agents/skills/<name>` |
| OpenCode | `.opencode/skills/<name>` |

Example from a POSIX shell; run in the recipient project, substitute your absolute
collection path, and choose exactly one destination:

```sh
COLLECTION=/absolute/path/to/collection
DEST=.claude/skills/planning
if test -e "$DEST" || test -L "$DEST"; then
  printf '%s\n' 'Collision: refusing copy; inspect and merge manually.' >&2
else
  mkdir -p .claude/skills
  cp -R "$COLLECTION/skills/planning" "$DEST"
fi
```

For routing, after the collision check also copy `scripts/route.py` to `scripts/` and
`templates/route-report.md` to `templates/`, refusing each collision. Do not assume a
single copied skill directory contains collection-root utilities.

For Codex replace `DEST` with `.agents/skills/planning` and the parent passed to
`mkdir` with `.agents/skills`; for OpenCode use `.opencode/skills/planning` and
`.opencode/skills`. Never copy over an existing skill directory. Inspect collisions
and merge manually. Do not replace project instructions, settings, or rules.
Role and command files are Claude-native metadata plus instruction bodies; other
harnesses require deliberate native configuration/manual adaptation, not universal
agent import. See [Codex](adapters/codex.md) and [OpenCode](adapters/opencode.md).
For whole-system intent, read [WORKFLOWS.md](WORKFLOWS.md) first: it identifies
missing routers, adapters, and specialist ports rather than hiding them as dependencies.

## Verify offline

Run from the collection root, not the recipient project:

```sh
python3 -B scripts/check_collection.py
python3 -B -m unittest discover -s tests -v
python3 -B examples/system-journey/run.py
python3 -B examples/continuity/run.py
python3 -B -m unittest discover -s examples/worked-example/tests -v
```

Python 3.11+ and the standard library suffice. The separate Claude strict validator
requires the Claude CLI. The continuity [run guide](examples/continuity/README.md)
explains the temporary-directory fixture; it never executes arbitrary Next text.
