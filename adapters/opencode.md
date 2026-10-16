# OpenCode adapter

State: skills **runnable** by copy; guard bridge and agents generator **runnable** (offline tests ship in
`opencode/`); continuity **described**; everything else listed under "Not included".

Both harnesses read the same three things from this collection: skills, role files and guard hooks.
Nothing is duplicated; OpenCode gets thin glue around the originals.

## Shared pieces

| Piece | Claude Code | OpenCode |
| --- | --- | --- |
| Skills | `skills/<name>` loaded from the plugin | copy `skills/<name>` into project-local `.opencode/skills/<name>` |
| Roles | `agents/` role files | `opencode/agents/opencode-agents-sync` turns the same files into an `agent` block |
| Guards | `plugins/gates` hooks via the plugin system | `opencode/bridge` runs the same hook scripts with Claude-Code-shaped payloads |
| Guard settings | user-scope file `~/.config/ai-architecture/gates.json` (or the file named by `AIARCH_CONFIG`) | the same file |

### Skills

The preparation review retrieved the official [OpenCode skills documentation](https://opencode.ai/docs/skills/) on 2026-10-14. It says OpenCode accepts project-local `.opencode/skills`, `.claude/skills`, and `.agents/skills`, walking from the current directory to the worktree. This is a supplied documentation observation, not a native-loading test or a claim that the page was re-fetched for this release.

For selective adoption, copy all of one `skills/<name>` directory into project-local `.opencode/skills/<name>`, including assets. Refuse an existing destination; inspect and merge manually. The other documented locations are alternatives, not instructions to install duplicate copies. See [INSTALL](../INSTALL.md) for the copy example and checks. Do not overwrite settings, rules or project instructions.

### Roles

```bash
python3 opencode/agents/opencode-agents-sync > /tmp/agents.json
```

The output is `{"agent": {...}}`: one subagent per role file, with its description, its body as the prompt and
the declared tools as switch-offs. Merge it into your OpenCode config by hand. Every model is the
placeholder `provider/model-name`; set a model you have configured (`--model` changes the default for all roles).
`--out FILE` writes a file; `--check FILE` exits 1 when a file differs from a fresh generation.
The generator only ever emits `false` for tools a role does not name (`read`, `edit`, `bash`, `grep`, `glob`,
`webfetch`; Edit and Write share `edit`, which is off only when neither is named). A role declaration therefore
grants nothing by itself: OpenCode turns every `true` into an allow rule that overrides your own ask and deny
rules, so named tools get no entry and stay under your permission settings.

### Guards

`opencode/bridge/plugin.ts` is an OpenCode plugin. It reads the hook list from
`plugins/gates/hooks/hooks.json`, so the Claude Code and OpenCode guard sets cannot drift apart, and runs each
matching hook script with the JSON payload Claude Code would send. A `deny`, an `ask` (no approver exists in a
headless run), a non-zero exit, a timeout or unparseable output blocks the tool call; every PreToolUse hook fails closed.
If the gates hook list cannot be read, is malformed or lists no PreToolUse hook, the bridge denies every tool call and
names `AIARCH_GATES_ROOT` in the message, rather than running with all gates off. A matcher that does not compile
runs its hook.

| Event | OpenCode hook used | Gates hooks served |
| --- | --- | --- |
| PreToolUse | `tool.execute.before` | `bash-destructive-guard`, `no-code-guard`, `edit-budget-gate` |
| SessionStart | first `chat.message` of a session, context spliced into that turn | `resume-inject` |
| Stop | `session.idle`, the reason is sent back as a new turn (at most 3 times in a row) | `review-stop-gate`, `clean-state-check` |

Before any hook runs, the bridge also refuses file mutations that go through a symlink, use `.` or `..`
components, touch the agent-config directories (`~/.claude`, `~/.config/opencode`, `~/.config/ai-architecture`), or
land outside the worktree when it is not a git repository. A second step runs `bash-structural`: it parses each Bash command
and denies one that OpenCode would run without any permission prompt, and can answer "once" for commands
that are provably read-only. The read-only set is deliberately small: `git status|log|branch|rev-parse|ls-files|merge-base`
and a few commands such as `ls`, `pwd`, `echo`, `date`. It contains no `git diff` or `git show` (a repo-local
`diff.external` or textconv setting runs code) and no file readers such as `cat`, `head`, `tail` or `grep` (a
relative path can be a symlink out of the worktree); those fall back to OpenCode's normal prompt. The step needs two npm
packages (see `opencode/bridge/package.json`); if they cannot be loaded it logs and skips, which means nothing is
auto-approved and you are asked as usual, and the gates hooks still run. File mutations under the worktree's `.git/`
are refused, and protected directories are compared after resolving the real path and ignoring case.

Known gaps compared with Claude Code: the model-based gates (`no_code_models`, `edit_budget_models`) find the
current model in a Claude Code transcript, which OpenCode does not write. When either list is set in the defaults or
your user-scope file, the bridge itself denies code Edit/Write (docs and config files stay allowed) without trying to
detect a model. Leave the lists empty unless you adapt the hook. OpenCode 1.18 passes no agent name to
`tool.execute.before`, so sub-agent calls are gated as the main session (stricter, never looser). The bridge does not
itself enforce read-only roles; use OpenCode's permission settings for that.

### Install

1. Install the gates plugin's requirements (`python3`, `git`) and, optionally, write your user-scope config file.
2. Copy the bridge and the gates plugin into one directory that OpenCode does not scan as plugins, and add a
   one-line shim to the plugin directory (copying the helpers into `plugins/` would load them as plugins):

   ```bash
   mkdir -p ~/.config/opencode/aiarch-bridge ~/.config/opencode/plugins
   cp -r opencode/bridge/. ~/.config/opencode/aiarch-bridge/
   cp -r plugins/gates ~/.config/opencode/aiarch-bridge/gates
   echo 'export { default } from "../aiarch-bridge/plugin"' > ~/.config/opencode/plugins/aiarch-gates.ts
   ```

   The bridge finds `aiarch-bridge/gates` on its own. Set `AIARCH_GATES_ROOT` to an absolute `plugins/gates` path only
   for another layout; a wrong path makes the bridge deny every tool call.
3. Optional: `cd ~/.config/opencode/aiarch-bridge` and run `bun install` (or `npm install`) for the Bash structural check.
4. Start OpenCode and try a command the destructive guard denies on a scratch directory.

Run `bash opencode/tests/run.sh` first. The bridge unit tests need `bun`; without it that part prints `NOT RUN`.

## Model rotation: runnable, opt-in

`opencode/rotation` rotates the model of a `task` call through variant agents you define, with a fail-safe default of no
rotation. Setup and tests: [opencode/rotation](../opencode/rotation/README.md). Not part of the guard bridge.

## Continuity: described

The author's OpenCode setup also carries a continuity module that tracks token use per session, banks tool results
before a context reset and warns before a limit. It is tied to personal quota and ledger files, so it is not
included. The same ideas ship as skills (`skills/continuity`, `skills/context`) and the Resume block read by
`plugins/gates`; implement budget tracking against your own provider's usage data if you want it.

## Not included

Rate-limit fallback, provider quota readers, session caps, worker and budget guards, delegated
task runners for other CLIs, and terminal-multiplexer integration. They depend on personal accounts and local services.
