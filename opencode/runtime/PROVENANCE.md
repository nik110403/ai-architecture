# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` = sha256 of the source file
as read when it was ported). Sources named `~/.config/...` or `~/.claude/...` are an author's installed OpenCode and
Claude Code files; they are not shipped. `scripts/check_collection.py` validates this file.

## continuity-core.ts

Kit file: runtime/continuity-core.ts
Source: ~/.config/opencode/lib/continuity.ts
Source-sha256: 7d889798932d05d252ef5de04558f87e42bf03d9494db1886f2cf275dae51837
State: re-authored

Deltas:
- the ledger moved to the Python module (`modules/continuity`): one writer implementation, reached through `bridge.ts`; this file keeps only caches
- no price tier and no fixed phase thresholds: limits, thresholds and budgets are recipient settings in the ledger's config file
- no root-session cap: nothing here counts root sessions
- freeze enforcement is the session-keyed port described under freeze-boundary below, folded into this file
- the resume injection is built by the CLI (`build_injection`), fenced and byte-bounded; the plugin only caches it per session
- no repair prompts: a bridge failure is logged and the call goes through (a hub start is refused instead)

## hub-core.ts

Kit file: runtime/hub-core.ts
Source: ~/.config/opencode/lib/hub-core.ts
Source-sha256: abe9af868ac848975d5a6efd14ef6bd96d9791d880f7a94703dec49e7bd71135
State: re-authored

Deltas:
- read roles only: the allowlist comes from the orchestration role file (access "read") plus the OpenCode agent config (`permission.edit` exactly `deny`, `permission.bash` set and not `allow`) instead of an orchestrator task map; an unreadable file or config gives an empty allowlist
- writers are refused with a pointer to the dispatcher (isolated worktrees)
- jobs are registered in the ledger before the prompt is sent and banked once when they end, are cancelled, or are found orphaned after a restart; the ledger is the Python module
- only a root session may start jobs; per-coordinator caps (`SOFT_CAP` 4 flags, `HARD_CAP` 15 refuses) bound child jobs, not root sessions
- no repair prompts and no fixed thresholds

## freeze-boundary (inside continuity-core.ts)

Kit file: runtime/continuity-core.ts
Source: ~/.claude/hooks/freeze-boundary.py
Source-sha256: cc746383335af6b6d9c787186611dd9d216712e7382d1b3b20934f8274d014a0
State: re-authored

Deltas:
- the freeze is keyed by session id (a record file per session in the freeze directory); there is no session-less fallback
- the record format, file name and `freeze_dir()` rule are those of the continuity freeze module; the decision is a TypeScript port of `decide_tool` and is checked against `freeze check` on a table of paths
- an unreadable or invalid record denies file edits and bash for that session

## plugin.ts, hub-tool.ts, bridge.ts

Kit file: runtime/plugin.ts
Source: ~/.config/opencode/plugins/claude-hooks.ts
Source-sha256: 44bad7fa79e39311459b1f6258b09417d377aebb1cd1f1bdabffb1536852c8d5
State: re-authored

Deltas:
- a short entry that composes the continuity hooks and the `hub` tool; `hub-tool.ts` is the `@opencode-ai/plugin` wrapper and `bridge.ts` runs the CLI with `execFile`, no shell; none of the source plugin's personal modules is included

## runtime tests

Kit file: runtime/tests/runtime.test.ts and runtime/tests/run.sh
Source: ~/.config/opencode/lib/continuity.ts
Source-sha256: 7d889798932d05d252ef5de04558f87e42bf03d9494db1886f2cf275dae51837
State: re-authored

Deltas:
- written for this module: a fake OpenCode client and the real Python CLI in a temporary project with a temporary `HOME` and freeze directory; the runner follows the rotation module's runner (prints `NOT RUN` without `bun`)
