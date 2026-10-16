# Provenance

One section per unit, in the collection's `Field: value` format (`Source-sha256` = sha256 of the
source file as read when it was ported). Sources named `~/.config/opencode/...` are an author's
installed OpenCode files; they are not shipped. `scripts/check_collection.py` validates this file.

## hooks-core.ts

Kit file: bridge/hooks-core.ts
Source: ~/.config/opencode/lib/claude-hooks-core.ts
Source-sha256: 1510d742041c77f8b3ce552f004e69886db4a77377f5faeb49dd96716689e1f1
State: adapted

Deltas:
- hook list is read from the gates plugin's own hooks file instead of a personal settings file; the spawned hook gets `CLAUDE_PLUGIN_ROOT` so the commands run unchanged
- spawn environment is PATH, HOME, the plugin root and, when set, the user-scope config pointer and state-dir variables; nothing else is passed
- fail-closed behaviour is unchanged: every PreToolUse hook fails closed on timeout, crash, unparseable output or a missing decision; "ask" becomes a deny because there is no approver
- protected write roots reduced to the agent-config directories (`~/.claude`, `~/.config/opencode`, `~/.config/ai-architecture`); the personal approved-file list and trusted-workspace exceptions are removed, so a write outside the worktree in a directory that is not a git repository is always refused
- removed: the plan-tracker child-edit refusal, the excluded-hook list, the host-client singleton, stop-hook handling of a personal marker set and all references to model rotation, quota and worker caps
- edit payload now also carries `content`, `old_string` and `new_string` so content-aware hooks see them
- event log moved to a neutral cache directory name
- `AIARCH_CONFIG` and `AIARCH_GATES_ROOT` are each ignored (default used, one-line warning on stderr) when they resolve inside the project (`CLAUDE_PROJECT_DIR`, the working directory or the plugin `directory`); paths are resolved through the nearest existing ancestor, so a not-yet-created path under the project is also rejected. Only the user-scope file can configure the gates
- the gates plugin directory is resolved once and cached (re-resolved only if `AIARCH_GATES_ROOT` changes)

## plugin.ts

Kit file: bridge/plugin.ts
Source: ~/.config/opencode/plugins/claude-hooks.ts
Source-sha256: 44bad7fa79e39311459b1f6258b09417d377aebb1cd1f1bdabffb1536852c8d5
State: re-authored

Deltas:
- wires only the gates bridge and the structural bash check; continuity, model rotation, rate-limit fallback and the other personal modules are not included

## bash-structural.ts

Kit file: bridge/bash-structural.ts
Source: ~/.config/opencode/lib/bash-structural.ts
Source-sha256: c5f9916e42288c28d61f1429c2062ef0fc3e72963c76777130e1b4c1a2085aa3
State: adapted

Deltas:
- comment wording (internal item ids and one personal file name removed)
- `ls` is auto-approved only without operands and only with flags from a letter allowlist (no `L`, `H` or `R`, which follow or recurse into symlinks); `stat`, `readlink`, `du`, `realpath` and `df` are no longer auto-approved at all, because a relative operand can be a symlink pointing outside the project and the classifier has no working directory to check it against. Those commands now go to the normal approval prompt. Nothing else in the classifier changed

## bash-structural-hook.ts

Kit file: bridge/bash-structural-hook.ts
Source: ~/.config/opencode/lib/bash-structural-hook.ts
Source-sha256: 9dba96c5fbd073fc76c80ccbcf04b8ac1047b62dad281a338c8bba04ad044d58
State: adapted

Deltas:
- comment wording only; behaviour unchanged (zero-pattern deny fails open only when the parser cannot load; any later error denies)
