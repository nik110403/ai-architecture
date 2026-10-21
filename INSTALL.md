# Install

This release is `0.4.2-preview`. There are three separate things you can install, and only the first
is on by default in the sense that it adds instructions rather than behaviour. What each capability does and
does not cover is in [CAPABILITIES.md](CAPABILITIES.md); what has been tested is in [COVERAGE.md](COVERAGE.md).

| Path | What you get | Changes how tools behave? |
| --- | --- | --- |
| A. Collection plugin | skills, roles, commands, templates | no |
| B. Gates plugin (opt-in) | hooks that can block tool calls | yes |
| C. OpenCode setup | the same guards inside OpenCode, optional model rotation | yes |

## A. The collection plugin (Claude Code)

The repository root is the marketplace root:

```text
/plugin marketplace add nik110403/ai-architecture
/plugin install ai-architecture@ai-architecture
```

To load the collection locally, replace `PATH` below with the absolute collection directory
(not the recipient project). Local loading is not a global install:

```sh
claude --plugin-dir PATH
claude plugin validate --strict PATH
claude plugin validate --strict PATH/plugins/gates
```

Use `/ai-architecture:plan`, `/ai-architecture:execute`, `/ai-architecture:review`,
`/ai-architecture:checkpoint`, `/ai-architecture:research`, or `/ai-architecture:health`.
Plugin-local command references use `${CLAUDE_PLUGIN_ROOT}` so health checks target
the collection rather than the user's project. Commands grant no permissions.
Plugin-root skills, roles and commands use Claude metadata; native loading remains
unverified here. Manually merge chosen rules; do not overwrite settings or rules.

## B. The gates plugin (opt-in hooks)

Install it only if you want hooks that can block your tool calls. The main plugin does not depend on it.

```text
/plugin install ai-architecture-gates@ai-architecture
```

or, to try it for one session from a checkout: `claude --plugin-dir PATH/plugins/gates`.
It needs `python3` and `git`. Full reference: [plugins/gates/README.md](plugins/gates/README.md).

| Hook | Blocks |
| --- | --- |
| destructive-command guard (Bash; best-effort tripwire) | wiping root or home, formatting a disk, fork bombs, recursive deletes of protected paths, reads of secret files (ssh and gpg keys, `.env*`, `*.pem`); asks first for force-push, `git reset --hard`, `DROP TABLE` and similar |
| no-code guard (Edit/Write) | code-file writes from the models you list in `no_code_models` (inert until configured) |
| edit-budget gate (Edit/Write) | the main session's code edits past a per-model limit (inert until configured) |
| review-stop gate (Stop) | ending a session once if UI or security-sensitive code was edited and no review agent ran |
| clean-state check (Stop) | ending a session once while a plan tracker has no current Resume block |
| resume-inject (SessionStart) | blocks nothing; loads the tracker's Resume block as context |

Configuration lives in **user scope only**: `~/.config/ai-architecture/gates.json`, or the file named by
`AIARCH_CONFIG`. A config file inside a project is never read, and a config path inside the project cannot loosen
a gate. That rule covers this plugin's own config files only: a project's Claude Code settings file (`<project>/.claude/settings.json`) can still set `disableAllHooks`, `enabledPlugins: false` or `env` values (for example `CLAUDE_HOOK_STATE_DIR`), and Claude Code settings are outside the plugin's control. Environment variables (`AIARCH_CONFIG`, `AIARCH_GATES_ROOT`) are trusted input from your shell. Lists you add extend the defaults; you cannot remove a
default entry from your file.

Turning gates off:

- The model gates (no-code, edit budget) do nothing until you fill `no_code_models` or `edit_budget_models`;
  empty them to switch them off again.
- There is no per-hook off switch for the others. To run only some hooks, load a copy of `plugins/gates` whose
  `plugins/gates/hooks/hooks.json` lists just those, or disable the plugin with `/plugin`.
- Model rotation is not part of this plugin's hook list. It stays off until you add its entry yourself
  ([plugins/gates/hooks/rotation/README.md](plugins/gates/hooks/rotation/README.md)); `MODEL_ROTATION=off` stops it.

The destructive guard fails closed: it denies every Bash call if `python3` is missing, if its own checks
crash, or if your config file is malformed (the message names the file). The model gates (no-code, edit budget) and the Stop and SessionStart hooks cannot run without `python3`, so there they fail open (Claude Code treats the hook error as non-blocking). Every blocking hook denies on
unparseable input, and an uncaught exception in a Python hook is answered with deny (pre-tool) or block (Stop) instead of exit 1. The Stop hooks block on an unparseable payload; `review-stop-gate` blocks once per cycle, and `clean-state-check` nudges once. SessionStart can only warn. The destructive guard is a best-effort tripwire against accidental destructive commands, NOT a security boundary against an adversarial agent: it pattern-matches bash text and cannot parse every bash form.
Known limits: secret reads through shell variables are not caught; constructs it cannot parse are denied or may be missed (e.g. unusual heredoc or line-continuation forms); the wall-clock limit is about 8.2 s, so under very heavy CPU load long commands may be denied and Stop hooks may block; a project's Claude Code settings can disable hooks.

## C. OpenCode

The bridge runs the same guard hooks inside OpenCode and adds a structural check for Bash commands. Rotation
of worker models by weighted pool is a separate, opt-in module. Both are optional and need no Claude Code.

1. Run `bash opencode/tests/run.sh` (the bridge tests need [bun](https://bun.sh); without it they print `NOT RUN`).
2. Copy the bridge and the gates plugin into one directory outside OpenCode's plugin folder and add a one-line
   shim. The exact commands are in [adapters/opencode.md](adapters/opencode.md); the overview is in
   [opencode/README.md](opencode/README.md).
3. Optional rotation: [opencode/rotation/README.md](opencode/rotation/README.md).

To print an OpenCode `agent` block generated from this collection's role files:
`python3 opencode/agents/opencode-agents-sync`.

## Uninstall

- Claude Code: `/plugin uninstall ai-architecture@ai-architecture` and
  `/plugin uninstall ai-architecture-gates@ai-architecture`, then `/plugin marketplace remove ai-architecture`.
- Gates configuration, if you wrote one: delete `~/.config/ai-architecture/gates.json`. Edit-counter state is
  under `~/.cache/ai-architecture/gates/`.
- OpenCode: delete the shim file you added to `~/.config/opencode/plugins/` and the directory it points to
  (`aiarch-bridge`, `aiarch-rotation`), then remove any rotation variant agents you added to your config.
- Selectively copied skills: delete the directories you copied.

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

For `plan-pack`, follow the selective-adoption dependency list in
[`skills/plan-pack/SKILL.md`](skills/plan-pack/SKILL.md): copy the planning templates for
your tier, `tools/plancheck/plancheck` and `rules/stakes-dial.md`, preserving root-relative
paths or adapting the references. T1 runs serially from its tracker; T2+ also needs
`exec-pack` and configured builder/checker roles, T3 needs the tribunal skill and its
dependencies, and GATES users need `gate-check`. No automatic bundle installation or
cross-harness role import is provided.

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
python3 -B scripts/privacy_scan.py
bash plugins/gates/tests/run.sh
bash opencode/tests/run.sh
python3 -B examples/system-journey/run.py
python3 -B examples/continuity/run.py
python3 -B -m unittest discover -s examples/worked-example/tests -v
```

Python 3.11+ and the standard library suffice. The separate Claude strict validator
requires the Claude CLI. The continuity [run guide](examples/continuity/README.md)
explains the temporary-directory fixture; it never executes arbitrary Next text.
