# opencode/rotation

State: **runnable** (offline tests ship here), opt-in. Nothing else in this repository loads it.

The OpenCode counterpart of the Claude Code rotation hook (see the pool format and brief lines in
[the hook's README](../../plugins/gates/hooks/rotation/README.md); the same
[pool file](../../plugins/gates/hooks/rotation/model-pool.example.json) works for both).

OpenCode's `task` tool picks an agent, not a model. So rotation switches to a **variant agent**: when the drawn model differs
from the role's configured model, the call's `subagent_type` becomes `<role>--<model with "/" replaced by "-">`
(for `provider/model-b` and role `builder`: `builder--provider-model-b`). If the drawn model is the role's own model,
or the variant is not defined in your config or its `model` is not the drawn one, the call is left unchanged.

| Path | What it is |
| --- | --- |
| `model-rotate.ts` | The module (`createModelRotate`), no dependencies beyond `node:` built-ins. |
| `plugin.ts` | Plugin entry that exposes the module's `tool.execute.before` hook. |
| `tests/run.sh` | Runs the Claude Code hook tests and the module tests (the latter need [bun](https://bun.sh)). |

## Turn it on

1. Write the pool file at `~/.config/ai-architecture/model-pool.json` (or set `ROTATION_POOL`).
2. For each role and each non-default model in the pool, add a variant agent to your OpenCode config: a copy of the role's
   agent with a different `model`, named as above. Example for role `builder` whose own model is `provider/model-a`:

   ```json
   {"agent": {"builder": {"model": "provider/model-a"},
              "builder--provider-model-b": {"model": "provider/model-b"}}}
   ```

   (give the variant the same `description`, `prompt` and `tools` as the role.) A missing variant means that draw leaves the call unchanged.
3. Copy `model-rotate.ts` and `plugin.ts` into one directory outside OpenCode's plugin folder and add a one-line shim
   in the plugin folder, as the guard bridge does (see [the OpenCode adapter](../../adapters/opencode.md)):

   ```bash
   mkdir -p ~/.config/opencode/aiarch-rotation ~/.config/opencode/plugins
   cp model-rotate.ts plugin.ts ~/.config/opencode/aiarch-rotation/
   echo 'export { default } from "../aiarch-rotation/plugin"' > ~/.config/opencode/plugins/aiarch-rotation.ts
   ```

4. Turn it off with `MODEL_ROTATION=off` or by deleting the shim.

The module reads the role's own model from `~/.config/opencode/opencode.json` (`ROTATION_OC_CONFIG` for another file; plain JSON only,
a config it cannot parse means no rotation).

## Fail safe and seed

Every problem leaves the call unchanged and nothing is ever thrown, so a session cannot be blocked by rotation: missing,
symlinked, oversized, wrong-version or invalid pool, `mode` other than `"on"`, a malformed entry in the role's list, no enabled
model, role not in the pool, unreadable config, variant not defined. A model that is not in the pool is never selected.
Model names are validated against `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}` and never reach a shell.

`ROTATION_SEED=<text>` makes draws deterministic for tests and replays, and is honoured only together with `ROTATION_TEST=1`
(a seeded generator derived from the text; the pick for a given seed, pool and role is always the same, so a seeded run is a
constant model). Without both it is ignored and the draw uses `node:crypto` `randomInt`.
`ROTATION_LOG=<file>` appends one JSON row per decision and per fail-safe exit (with a `reason`); the file is opened 0600, its
directory 0700, symlinks are not followed.
`ROTATION_POOL`, `ROTATION_LOG` and `ROTATION_OC_CONFIG` must be absolute paths outside `CLAUDE_PROJECT_DIR` and the cwd; otherwise
the default pool/config is used and no log is written. The pool and the config are read with `O_NOFOLLOW`, must be regular
files, and are capped at 1 MB. `builder_arm` is read from the brief header like `pin_arm`; more than one `builder_arm:` line
anywhere in the prompt is ignored and skips rotation for independent roles.

```bash
bash opencode/rotation/tests/run.sh
```
