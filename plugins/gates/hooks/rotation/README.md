# Model rotation hook (Claude Code, opt-in)

State: **runnable** (offline tests ship here). **Not enabled by the plugin**: the plugin's own hook list does not include it, so
installing `plugins/gates` never rotates anything. You turn it on yourself.

For a sub-agent dispatch (`Task`/`Agent` tool) whose `subagent_type` is a role named in your pool file, the hook
replaces the dispatch's `model` with a weighted-random pick from that role's list. Nothing else in the call changes.
Use it to spread work across models or to compare them; it is not an optimising policy and keeps no state.

| File | What it is |
| --- | --- |
| `model-rotate.py` | The hook (stdlib `python3`). |
| `model-pool.example.json` | Pool example with placeholder models (`provider/model-a`). |
| `hooks.fragment.json` | The `hooks` entry to merge into your own settings. |
| `tests/test-model-rotate.sh` | Offline tests; also run by `opencode/rotation/tests/run.sh`. |

## Turn it on

1. Copy the example pool to `~/.config/ai-architecture/model-pool.json` and replace the placeholder names with models your
   harness accepts in a dispatch's `model` field (set `ROTATION_POOL` to use another path).
2. Merge the `PreToolUse` entry of `hooks.fragment.json` into your Claude Code settings file (user scope) and replace
   `/absolute/path/to` with the real location of this directory. It must stay a separate entry: do not add it to the
   plugin's own hook list, because the OpenCode bridge runs every hook listed there and a broken entry would block tool calls.
3. Remove it by deleting that entry, or set `MODEL_ROTATION=off`.

## Pool file

```json
{"version": 1, "mode": "on", "independent_roles": ["reviewer"],
 "roles": {"builder": [{"model": "provider/model-a", "weight": 3},
                       {"model": "provider/model-b", "weight": 1, "enabled": false}]}}
```

- `mode` must be exactly `"on"`; `version`, when present, must be `1`.
- `roles` maps a role name (lowercase letters, digits, `-`) to its models. Only roles listed here are rotated.
- A model is `{"model": name, "weight": n}` with an integer weight from 0 to 1000000; `"enabled": false` or weight 0 removes it.
  Names match `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}`. Models are passed as data and never reach a shell.
- A pool holds placeholders and weights only: no accounts, quotas or usage figures.

## Brief lines

- `pin_arm: <model>` in the brief header (the lines before the first blank line, at most 15) skips the draw when that model is
  an enabled entry of the role. An unlisted, disabled or late pin is ignored and the draw runs.
- `builder_arm: <model>` in the brief header (same rule as `pin_arm`), for roles in `independent_roles`: that model is left out
  of the draw, so a reviewer does not run on the model that built the work. It is skipped when it would leave nothing to draw.
  If a `builder_arm:` line appears more than once anywhere in the prompt (for example injected text), it is ignored and
  rotation is skipped for that dispatch, which then runs on its configured default.

## Fail safe

Any problem prints nothing and exits 0, so the dispatch runs on its configured default model: unparseable input, another
tool, a role not in the pool, a missing, unreadable, symlinked, oversized, wrong-version or `mode`-off pool, any malformed
entry in the role's list, no enabled model, or `MODEL_ROTATION=off`. When `ROTATION_LOG` is usable, every such exit appends a row
with `"model": null` and a `reason`. A model that is not in the pool is never picked,
and the hook cannot deny a tool call.

## Environment

| Variable | Effect |
| --- | --- |
| `MODEL_ROTATION=off` | Disable. |
| `ROTATION_POOL` | Pool file path. Must be absolute and outside `CLAUDE_PROJECT_DIR` and the cwd (a project must not choose its own pool); otherwise the default path is used. |
| `ROTATION_LOG` | Append one JSON row per decision and per fail-safe exit (role, model, pinned, reason); file 0600, directory 0700, symlinks not followed. Same path rule as `ROTATION_POOL`; otherwise no log. Unset: no log. |
| `ROTATION_TEST=1` | Required for `ROTATION_SEED` to take effect. |
| `ROTATION_SEED` | With `ROTATION_TEST=1` only. Deterministic draw for tests and replays: the pick is `random.Random("rotation:" + seed).randrange(total weight)` over the enabled models in file order. Unset: `secrets.randbelow`. A seeded run always gets the same model, so do not set it in normal use. Without `ROTATION_TEST=1` it is ignored. |

## Output and harness behaviour

The hook answers with `permissionDecision: "allow"` plus the rewritten input. That auto-approves the dispatch; a `deny` or `ask`
from another hook still wins. A pool model name that passes the strict name check but that your harness does not accept may make
the harness reject the dispatch outright: it is not silently replaced by the default. Keep the pool to names your harness knows.

The OpenCode counterpart is described in [opencode/rotation](../../../../opencode/rotation/README.md).
