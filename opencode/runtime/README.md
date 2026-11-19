# opencode/runtime

State: **runnable** (offline tests ship here), opt-in. Nothing else in this repository loads it.

The OpenCode side of the [continuity module](../../modules/continuity/README.md): the plugin records context usage,
injects the checkpoint's Next once, enforces a per-session freeze, blocks new workers at HANDOFF, stops a hub child at
its token budget, and adds a native background `hub` tool for **read-only** child sessions. All ledger state lives in
`modules/continuity` (Python); this directory holds caches and the OpenCode glue.

| Path | What it is |
| --- | --- |
| `bridge.ts` | `runContinuity(args, input?)`: runs the continuity CLI with `execFile` (no shell, 5 s, 64 KiB); any failure is `{code: -1}`. |
| `continuity-core.ts` | The hooks: `event` (usage), `experimental.chat.system.transform` (resume), `tool.execute.before` (freeze, handoff gate, child budget). |
| `hub-core.ts` | The hub: `start`, `status`, `wait`, `cancel` over child sessions. |
| `plugin.ts` | Plugin entry composing both. |
| `hub-tool.ts` | The `hub` tool wrapper for `@opencode-ai/plugin`. Not unit tested; the behaviour is in `hub-core.ts`. |
| `tests/run.sh` | Runs `tests/runtime.test.ts` under [bun](https://bun.sh) (prints `NOT RUN` and exits 1 without it). |

## Install

1. In the project, run `python3 modules/continuity/continuity.py init --project .` (and add `.ai-continuity/` to `.gitignore`). Without a ledger file under `<project>/.ai-continuity` every ledger feature is inactive for that directory (logged once) and the hub refuses to start jobs.
2. Add `opencode/runtime/plugin.ts` to your OpenCode plugin list; it loads `hub-tool.ts`. Keep the directory inside this repository (or copy it next to the same `modules/`): the bridge finds the CLI in the sibling `modules` directory relative to its own location.
3. Freeze a session with `python3 modules/continuity/continuity.py freeze set --harness opencode --session <id> --root <dir> [--bash deny]`.

## What is enforced

- **Usage**: a completed assistant message of a root session runs `usage record` (sum of input, output, cache read and write; `unknown` if any count is missing, so readiness reports `usage UNKNOWN`; the context limit comes from the provider list when it has one). Child sessions keep usage in memory for the child budget.
- **Resume**: the first system-prompt build per root session runs `resume consume`; the CLI's fenced, byte-bounded injection is cached for the session so the prefix stays byte-stable. A restart is a new plugin instance; the Next is delivered to one session only. `session.compacted` rebuilds the cached text with source `compact`, which consumes nothing.
- **Freeze**: `write`, `edit`, `patch`, `apply_patch` outside the frozen root, and `bash` (mode `deny`), are denied for that session id only. The check is a port of the freeze module and agrees with `freeze check` on the tested path table. An unreadable record denies. Child or task sessions have their own ids and are **not** frozen by their parent's record. Same-user records are not a security boundary.
- **Handoff gate**: `task` and `hub` `start` throw at phase HANDOFF. If the ledger cannot be read, `task` goes through with a logged warning and a hub start is refused (the hub must register in the ledger).
- **Child budget**: a hub child whose observed usage reached its `budget_tokens` gets an error telling it to stop and return a partial result. Unknown usage is not treated as over budget.
- **Hub**: read roles only. An agent qualifies only if the orchestration role file gives it `access: "read"` and its OpenCode agent config has `permission.edit` exactly `deny` and `permission.bash` either `deny`/`ask` or an object whose `"*"` entry is `deny`/`ask` (a missing setting, `{}`, or an object without a deny/ask `"*"` default is refused because OpenCode allows an unmatched command). Writers are refused with "use dispatch.py: write roles run in an isolated worktree". Only a root session starts jobs; at most 15 open jobs per coordinator session (4 and above are flagged `over_soft_cap`). A job is registered before its prompt is sent, banked once when it ends (text capped at 8000 characters), banked `cancelled` when cancelled, and banked `orphaned_external` when found open after a restart (the hub keeps a hub-jobs file in `.ai-continuity` for that; an entry written by another process is swept only when that process is dead, meaning `kill(pid, 0)` fails with ESRCH or its recorded start time no longer matches, every unbanked entry of another process is kept in the file by each instance's atomic read-merge-write until a sweep banks it; an orphan whose ledger row is already banked or absent is dropped without an abort or a bank, and one found while the ledger is unreadable is deferred: not aborted, not banked, kept for a later hub). While a child runs, the hub also honours a ledger cancel request (`continuity.py cancel`): it aborts the child and banks `failed` with cause `cancelled`; a request that arrives after the child completed changes nothing. A child that ends with an error is banked `failed` with cause `cancelled`, because the ledger has no failed-and-completed pair.
- **Worker processes**: everything above is off when `AI_ARCH_JOB_MARKER` is set.

## What is not enforced

The plugin cannot sandbox a hub child: its tools are whatever its agent config allows. A session that has no freeze record, or a bash call under `--bash unscoped`, is not restricted. The hub's job file is not locked across processes: a single hub instance per state directory is assumed. `continuity.py reconcile` recovers the lock window and a crash between register and persist. Tools of other plugins are not seen. Not run against a live OpenCode session.

## Remove

Remove the plugin entry from your OpenCode config, delete the copied directory, and optionally `freeze clear` the records. The ledger stays in `.ai-continuity/` until you delete it.

## Tests

```bash
bash opencode/runtime/tests/run.sh
```

Needs `python3` and `bun`. Uses a fake client and the real CLI in a temporary project; no model and no OpenCode server is started.
