# Continuity: durable job ledger over the dispatcher

`continuity.py` keeps a project-local record of dispatched jobs so a result is banked exactly once, survives a
restart of the coordinator, and is never silently lost. It starts workers through
[dispatch.py](../orchestration/dispatch.py) (see the [orchestration README](../orchestration/README.md)) (always as a separate process in its own
session), watches for their results, cancels them, drains work before a restart, and reconciles jobs whose
dispatcher died. Stdlib only, Python 3.9 or later, Linux first.

```
python3 -B modules/continuity/continuity.py init   --project .
python3 -B modules/continuity/continuity.py start  --project . --config my-dispatch.json --role researcher --intent "list the open questions"
python3 -B modules/continuity/continuity.py wait   --project . --job <job-id> --timeout 600
python3 -B modules/continuity/continuity.py status --project .
python3 -B modules/continuity/continuity.py collect --project .
```

`run` is `start` plus a wait as the dispatcher's parent. Add `.ai-continuity/` to your `.gitignore`: it holds job
state, prompts and outputs. `init` prints that reminder and never edits `.gitignore`.

## Threat model

Everything runs as one user. The ledger, its lock, the job directories and the spool files are writable by every
same-user process, workers included. The ledger is the coordinator's record, not a security boundary: a worker
that holds Bash can forge it, delete it, or write a fake `<state>/result.json`. A result banked from a write role is
marked `trust: worker-attested`. This matches [the orchestration threat model](../orchestration/README.md).

## Platform

The ledger needs `fcntl.flock` and a local filesystem (no NFS). Where `fcntl` is missing every command refuses
with `unsupported platform: the ledger needs fcntl.flock` (exit 2). Reconciliation that inspects or signals
processes needs `/proc` and `os.pidfd_open` (Linux 5.3 or later; older kernels fall back to a start-ticks
re-check followed by `os.kill`, and the result note says `signal: no pidfd`). Without `/proc`, jobs whose
dispatcher is gone are reported `unresolved`: never killed, never guessed. A reconciler running in a different
time namespace (`unshare -T`) sees a shifted `CLOCK_MONOTONIC` under the same boot id; that is unsupported.

## Layout

```
<project>/.ai-continuity/ledger/
  ledger.json        state document, replaced atomically
  ledger.lock        flock target, created once, never removed
  reconcile.lock     flock target serialising reconcilers
  config.json        settings (written by init, unknown keys refused)
  runs/<job_id>/     the dispatcher's job directories (dispatch.py --run-dir runs)
  spool/<job_id>.prompt, .out, .err, .external.txt
```

All files 0600, directories 0700. `init --project DIR` creates it; every other command takes exactly one of
`--project DIR` or `--state DIR` (the `ledger/` directory itself). There is no home scan and no default search.
Every component from the project root down must be a real directory (no symlinks), owned by you and not group-
or other-writable. `skills/continuity/scripts/journal.py` shares `.ai-continuity/` but only touches
the journal file; the ledger never touches the journal.

`<state>/config.json`: `lock_wait_seconds` 10, `max_active_jobs` 8 (a runaway guard on one ledger, not a root-session
count), `launch_grace_seconds` 30, `stuck_grace_seconds` 180, `cancel_wait_seconds` 30. Handoff and budget settings
(all optional, see [Checkpoints](#checkpoints-usage-readiness-handoff-and-resume)): `handoff_reserve_tokens` 20000,
`handoff_cap_tokens` null, `prepare_fraction` 0.8, `context_limits` {}, `child_budget_tokens` null,
`child_budget_pool_tokens` null, `resume_max_bytes` 4096 (at most 16384).

## Storage and exactly-once banking

One JSON document. Every mutation is `with ledger.transaction() as doc:` under an exclusive `flock` that the
kernel releases when the holder dies, so there is no stale-lock reclaim. A write is: remove `ledger.json.tmp`,
create it exclusively, write, `fsync`, `os.replace`, `fsync` the directory. Any failure before the replace leaves
the previous revision untouched (exit 4). A failure of the directory `fsync` after the replace means the new
revision is visible but not proven durable (exit 7: do not retry). The lock is never held while waiting for,
signalling or sweeping a process. Transactions are not reentrant.

A job is `launching` (recorded, dispatcher not yet recorded), `launched`, or `banked` (terminal). Every banking
path calls one function, `bank()`, which checks "already banked?" and writes inside one exclusive transaction:
exactly one caller gets `banked_now: true`. A result that shows up after banking (a late or forged
`<state>/result.json`) is appended to `post_bank` (at most 8) and never re-banked. The ledger is strict: duplicate keys,
`NaN`, unknown keys and a missing or truncated file are `LedgerInvalid`; nothing repairs or resets it.

A banked job's `cause` is one of `completed`, `refused`, `cancelled`, `timed_out`, `orphaned`, `lost`,
`not_launched`, `lost_in_launch`, `invalid_result`, `result_missing`, `abandoned`, `orphaned_external`. A
`succeeded` record whose `dispatch.timed_out` is true or whose worker exit code is -9 is banked `failed`. When a
cancel was requested and the job banks `failed` or `unsupported` without a completed result, the cause is
`cancelled` and the original cause moves into `note`.

## Commands

All print one JSON object. Exit: 0 ok, 1 job failed, 2 refused or bad usage, 3 ledger busy, 4 ledger invalid or
storage failure (nothing changed), 5 still pending at timeout, or processes were killed or a worktree removed and the
bank then failed (run `reconcile` again), 6 dispatcher launched but its record or result is not banked (never
retry), 7 committed, durability unknown (never retry).

| Command | Does |
|---|---|
| `init --project DIR` | Create the state directory; refuses one that names another project root. |
| `start --config CFG --role R (--intent T \| --prompt-file F) [--model M] [--allow-spend] [--workdir D] [--session HARNESS:ID] [--budget-tokens N] [--job-id ID]` | Admit (refused while draining or at `max_active_jobs`), launch the dispatcher, record it, return without waiting. `--role` is required (intent words resolve in `dispatch.py plan`). |
| `run` (same options) | `start`, then wait as the dispatcher's parent and bank with the exit code. Once launched it never exits 3 or 4: a ledger failure after launch is exit 6 with the result path and `changed` (true when a signal was sent or a worktree removed), and after the bank it exits with the banked outcome. The outcome comes from `<state>/result.json`, then the dispatcher's stdout envelope (the order the reconciler uses). With neither, `run` synthesises nothing: it takes `reconcile.lock` and runs the dispatcher-gone rows (worker group kill, marker sweep, rule W) before banking; a busy lock leaves the job for the reconciler, exit 5. While waiting it retries recording the dispatcher and honours a `cancel`. |
| `wait --job ID [--timeout S]` | Poll for `<state>/result.json` and bank it; also settles a job whose dispatcher is gone. Exit 5 while pending. |
| `status [--job ID] [--session H:ID]` | Bank every finished job first (no signals), then summarise. An invalid ledger reports `storage: invalid`. With `--session` it also prints that session's `phase`, `usage_tokens` and `limit_tokens`. |
| `collect` | The same non-destructive bank, then list banked jobs not yet acknowledged (the arrivals), newest first. |
| `cancel --job ID [--session H:ID] [--force]` | SIGCONT, SIGTERM the verified dispatcher, bank what it writes. The kill and worktree rows run under `reconcile.lock` (waits up to `lock_wait_seconds`, then exit 5 with the request recorded). A job owned by a session needs that session or `--force`. `--force` also SIGKILLs a dispatcher still alive after `cancel_wait_seconds`. |
| `drain start --reason T \| status \| end \| wait [--timeout S]` | An admission barrier kept in the ledger until `drain end`. |
| `reconcile [--dry-run] [--job ID]`, `reconcile --force-bank ID --reason T` | Apply the recovery table. `--force-bank` records an `unresolved` job as `failed`/`abandoned` without touching any process; it refuses unless the dispatcher is provably gone (an alive verdict; an unknown one unless `--allow-unknown-liveness`, which is itself refused without `--force-bank`, says the operator vouches for it and is recorded as "(unknown liveness overridden)" in the banked note; or a `launching` job while a process carries its dispatcher argv or `/proc` cannot be searched, exits 2). Known gap: a `launching` job whose dispatcher has not yet exec'd carries no dispatcher argv to find, so `--force-bank` can close it just before the dispatcher starts; only force-bank a `launching` job you know is stuck, never one that is seconds old. |
| `external register ...` / `external bank ...` | For read-only jobs run by something other than [dispatch.py](../orchestration/dispatch.py) (a native background hub). |
| `usage record --session H:ID --tokens N\|unknown [--limit N] [--model M] [--source S]` | Record a session's context usage. |
| `checkpoint --project DIR --state T --next T --evidence REF ... [--tracker P] [--accept-failed ID ...] [--session H:ID]` | One transaction: acknowledge every banked job not yet acknowledged and write a hash receipt. (`--state` is the checkpoint text here, so `checkpoint` and `handoff` take `--project`, not the ledger-directory `--state`.) |
| `ready --session H:ID` | Exit 0 only when READY; otherwise exit 1 listing every failing condition. |
| `handoff --session H:ID --state T --next T --evidence ...` | `checkpoint`, then `ready`; exits as `ready`. |
| `freeze set\|clear\|status\|check ...` | A per-session file-edit freeze; see [Session freeze](#session-freeze). |
| `start`/`run ... --context PACKET.json --intent T` | Launch with a validated context packet; see [Context packets](#context-packets-start---context). |
| `resume show [--session H:ID]` / `resume consume --session H:ID --cwd DIR --source startup\|resume\|clear\|compact` | Read, or deliver once, the latest checkpoint's Next. |

## Checkpoints, usage, readiness, handoff and resume

**Receipt.** `checkpoint` appends a receipt to the ledger (at most 20 kept, the newest always): revision, state and
Next text, evidence (at least one reference; a reference that names a regular file inside the project, relative, no
`..`, no symlink component, is hashed; a reference that looks like a path (an extension or a slash, no whitespace) but is missing, a directory, a symlink or outside the project is refused, so a mistyped path cannot give READY. That test is a typo guard, not a boundary: it also refuses literal references such as `origin/main`, `owner/repo#12`, `node.js` or `CI/CD`, and passes `Makefile`, `README` or a name with a space as plain text; any other reference is kept as literal text with no hash. `--state` and `--next` are validated first, then the evidence is hashed, and both happen before the ledger lock is taken), an optional hashed
tracker file, the jobs it acknowledged (`covered_jobs`), the failed jobs the caller accepted (`accepted_failed`; only
a banked `failed` or `unsupported` job can be named), the jobs still pending, a digest of the job table, and
`receipt_sha256` over canonical JSON of all of it except the `resume` marker.

**READY** is a conjunction and `ready` lists every failing condition: the ledger validates; a checkpoint exists and its
`receipt_sha256` recomputes; no job changed after the checkpoint; no job is unbanked (an `unresolved` job is unbanked);
every banked job is acknowledged and has no later `post_bank` entry; every failed or unsupported banked job is in some
kept checkpoint's `accepted_failed`; every hashed evidence file and the tracker still hash the same; and the session's
`usage_tokens` is an integer. A condition that cannot be evaluated counts as failing. There is no path to READY with
unknown usage: no record, `--tokens unknown`, a malformed transcript tail, a `<synthetic>` placeholder entry and a failed usage-hook write all give `usage UNKNOWN`. `ready` also reports `usage_age_seconds`, the age of the session's last usage record. Readiness is
project-wide on purpose (a successor session must bank its predecessor's jobs); with several root sessions in one
project it is conservative, never falsely ready. A failed job accepted in a checkpoint that later falls out of the 20
kept is no longer accepted: accept it again in a newer checkpoint.

**Phases and the handoff gate.** `handoff = context limit - handoff_reserve_tokens`, capped by `handoff_cap_tokens` when
set; `prepare = floor(prepare_fraction * handoff)`. The limit is the session record's `--limit`, else
`context_limits[model]`. Phase is `UNKNOWN` when usage or limit is unknown, else `HANDOFF` at or above the handoff
line, `PREPARE` at or above the prepare line, else `NORMAL`. `start --session H:ID` refuses at `HANDOFF` ("bank
results and checkpoint before new workers"); at `UNKNOWN` it admits and prints a `note`. Without `--session` there is
no phase gate. These are the recipient's settings, not a fixed policy, and nothing counts root sessions.

**Child budgets.** A job stores `budget.tokens` (`--budget-tokens`, else `child_budget_tokens`). At banking the result
gets `usage_total` (the sum of the known `input`, `output`, `cache_read`, `cache_write`; null when the dispatcher
reported no usage) and `budget_state`: `within`, `exceeded`, `unknown` or `unbounded`. This is a judgement after the
fact; the ledger does not stop a running worker. Enforcement inside a worker belongs to the harness (the OpenCode
plugin for hub children; a Claude Code seat's `max_budget_usd` stays in the dispatch config). When
`child_budget_pool_tokens` is set, `start` also refuses a job with no budget (give `--budget-tokens` or set `child_budget_tokens`), and refuses if the reserved budgets of unbanked jobs, plus the new job's own
budget, plus the known totals of jobs banked since the last checkpoint exceed the pool, and refuses with `child usage
unknown since the last checkpoint` when any of those banked jobs has no `usage_total`. A checkpoint opens a new window.

**Resume, exactly once.** The latest checkpoint's Next is delivered once, marked under the ledger lock. Precedence,
first match wins: (1) consumed by another session: the caller gets only `delivered: false`, who consumed it and when,
never the Next as an instruction, the writer included; (2) consumed by this session: the text again, labelled
`already delivered to this session`; (3) unconsumed and `--source compact`: the text labelled `pending for the next
session` and nothing marked, for every session (compaction never consumes); (4) unconsumed and source `startup`,
`resume` or `clear`: marked consumed, the writer included, so a crashed coordinator restarted under its own id takes
its Next and no later session reruns it. A Next that cannot be shown whole within `resume_max_bytes` (in the longest label) is refused by `checkpoint`, so a Next written under the current setting always fits (one that no longer fits, because `resume_max_bytes` was lowered afterwards, stays pending). Delivery is at-least-once: the SessionStart hook marks the Next consumed only after the injection was written (a crash or closed pipe in between loses nothing), so a failure of that mark after the print, such as a busy ledger, means the same Next can be delivered again by a later session, and two sessions starting at the same moment may both print it. A new checkpoint carries a new, unconsumed Next. `--cwd` must resolve inside
the project root (`wrong root` otherwise). `resume show` and `consume` also return `injection`, built by one function
(`resume.build_injection`) so every harness injects identical text: the Next, state, readiness, at most 8 pending job
ids, at most 8 unacknowledged arrivals and the drain state inside `<untrusted-file-content>` fences, at most
`resume_max_bytes`. Each line is made printable first and then stripped of the fence delimiters (repeatedly, so a
nested delimiter cannot re-form), so stored text cannot close the fence.

**Claude Code hooks** (`hooks/session_start.py` for SessionStart, `hooks/claude_usage.py` for Stop; wiring in
[INTEGRATION.md](INTEGRATION.md)). Both find the ledger from the payload `cwd` by walking up at most 64 parents to the
first directory holding a ledger (ledger.json under .ai-continuity/ledger, no symlinked component) whose recorded `project_root` is that directory;
nothing else is searched. Both exit 0 with no output when `AI_ARCH_JOB_MARKER` is set (a dispatched worker's own
session must not consume the coordinator's Next or record its usage), when the payload has `agent_id`, when no ledger
is found, and on any error. The usage hook reads at most the last 256 KiB of the transcript and records the last
assistant entry's four token counts and model, or `unknown` when any is missing: it never guesses.

## Reconciliation

`reconcile.py` implements the recovery table (see also the
[orchestration README](../orchestration/README.md#reconciliation-for-a-later-recovery-step)). Per job it first
decides whether the dispatcher is alive, and only then reads `<state>/result.json` and `<state>/start.json`.

- **Identity (R1).** Alive: `/proc/<pid>` exists and the recorded start ticks match, or the ticks are unrecorded
  and the command line holds the job's exact `--job-id` and `--run-dir` tokens. Gone: no such pid, differing
  ticks, a different boot id, or state `Z`. Anything else is `unknown`: no signal, no bank, reported `unresolved`.
  The ledger's own dispatcher record is used; `<state>/start.json` is worker-writable and only cross-checked.
- **Stuck (R2).** State `T`, or the same boot id and `CLOCK_MONOTONIC` minus the reading taken right after launch
  exceeds `timeout + stuck_grace_seconds` (`timeout = deadline - started_at` from `<state>/start.json`). With missing
  inputs a live dispatcher younger than `launch_grace_seconds` is `running`, an older one `unresolved`: never
  killed on missing inputs. Wall-clock time is never compared with `deadline`.
- **Worker (R3).** The worker is killed only if its `environ` holds the job marker and its pid and start ticks equal
  `<state>/start.json`. Unreadable `environ` or null ticks: unknown, not killed.
- **Signals** go through `pidfd_open`, then a re-check of the start ticks, then `pidfd_send_signal`. Only ESRCH or a
  ticks mismatch means gone; any other pidfd error is `unresolved`. A pidfd does not pin a process group, so the
  group kill lists the group's members by `/proc` (same user, start ticks at least the worker's) and signals each
  through its own pidfd. A process that joins the group after the listing is missed; the marker sweep that follows
  is the backstop, and it cannot see an `env -i` or non-dumpable descendant.
- **Adoption** of a `launching` job's dispatcher needs the argv `<dispatch.py> run ... --job-id ID --run-dir RUNS` on a
  process that leads its own session (pid = pgid = sid), with equal start ticks before and after the argv read and
  again inside the ledger transaction. The stored (pid, ticks) pair is what any later signal uses. The group kill
  needs `pgid` = the worker pid = the worker's `/proc` group; `<state>/start.json` alone is never trusted. A start-ticks
  value that is not an integer is unknown, not gone.
- **Rule W.** A half-created worktree (dispatcher died in `starting`) is removed together with its branch only
  when every git check agrees it is empty (planned path and branch, `HEAD` on the branch, no commit reachable only
  from it, clean status including ignored files). Anything else keeps it and says why. Every other row keeps a
  worktree: a finished or orphaned write job's branch is the work product; integration is not done here. Rule W does
  not run after a marker sweep that timed out or failed (the job stays unresolved), every git call runs with
  `core.fsmonitor=false`, and a removal is written to `<state>/rule_w.json` so a retry after a failed bank still
  reports it. That file is worker-writable, so a record counts only when git agrees the branch and the worktree are
  gone. A repository's `filter.*.clean` driver can still run during `git status`; it runs as the same user, so no
  boundary is crossed.
- Files a worker can write (the result, start, removal and stdout files) are opened `O_NOFOLLOW|O_NONBLOCK`
  and must be regular files and size-capped: a FIFO is invalid, never a hang. The acted rule is per command and
  enforced at the one entry point (`continuity.main`): once a signal was sent or a worktree removed anywhere in the
  command, any ledger failure (busy, invalid, storage) is exit 5 with `changed: true`, never exit 3 or 4 (`run` after
  launch is the one exception: it keeps exit 6, with `changed`). Exit 5 with `changed: true` means the ledger is behind the world: this command acted (sent a signal or removed
  a worktree) AND a row of a job it acted on is left not banked (for example `cancel` whose own SIGTERM was followed
  by an error or left the job `running`, or a `reconcile` where one job was killed and its bank then failed). Every
  non-banked row of a job that acted carries `changed: true`, including rows that did not themselves signal; one
  predicate decides it for every command. A command that acted and banked everything exits 0. A live `running` job
  this command did not touch is not behind, and neither is an `unresolved` job it did not touch (that one needs
  `reconcile` or `--force-bank`, not "run again"): `reconcile` lists it and exits 0, and `wait` and `drain wait` keep
  waiting after an earlier kill was banked. A marker sweep that timed out counts as an act, and in the `starting`
  branch and in both `running` branches (worker already gone, or killed by this command) its row is `unresolved`
  with `changed: true` (never banked as "swept 0"), so a later `reconcile` sweeps again. A refusal (exit 2) after an
  act is exit 5 `changed` too. `wait --job ID` on a job whose result the ledger refuses stops with exit 2 and names
  `--force-bank` instead of waiting forever, but only when the job's row is `unresolved` (its dispatcher is gone); a live job
  keeps waiting, and `--force-bank` refuses a job whose dispatcher is alive. Without `--timeout`, an `unresolved` row this
  command did not touch that stays the same across two polls also exits 2, naming `reconcile` and `--force-bank`.
  If the command itself had acted on the waited job and it is still open, `wait` goes through the "run reconcile
  again" path (`SideEffectsDone`): exit 5 `changed`, remedy `reconcile`. `start` with a pending cancel that it
  signalled and that left the job unbanked is exit 5 `changed` too.
  `run` maps a result that `check_result` refuses to exit 6 (with `changed`), not exit 2.
- No worker-supplied text reaches the ledger uncapped. Notes and reasons are capped (500 characters, with the
  sha256 of the whole). A dispatcher result record is refused above 8 KiB counted in bytes as stored (ASCII-escaped,
  indented, so it is the size the ledger really grows by); `check_result` enforces the same 8 KiB on `record`. The
  other text fields (note, evidence kind and sha256, worktree branch and reason) are bounded by `check_result` at
  1024 characters, and worktree and evidence paths at 4096 characters: those two limits count characters, not bytes
  (a non-ASCII field can store as several bytes per character; the notes are capped at 500 characters first).
  Worktree paths and branches come only from the ledger's own plan; `start` and `run` refuse a `worktree_root`
  whose planned path exceeds 4096 characters. A new result that `check_result` refuses at bank time (a healthy
  ledger) leaves that job `unresolved` (the others continue, in `reconcile` and in `status`, `collect`, `wait`, `cancel`
  and `drain wait`), is exit 6 in `run`, and never exit 4. `reconcile --force-bank ID --reason T` closes such a job
  as `failed`/`invalid_result` with a capped reason.
- One job that raises while being reconciled is `unresolved` for that job (no traceback); the others continue and
  `--force-bank` still works. If it raised after that job sent a signal or removed a worktree, that job's row is
  `changed: true`; `reconcile_all` still continues with the other jobs and exits 5 with `changed: true` at the end
  (as it does whenever any row is `changed`). More than one process carrying a job's dispatcher argv (after dropping a match whose
  parent is also a match) is ambiguous: nothing is adopted or signalled.
- Only one reconciler runs at a time (`reconcile.lock`); a busy lock is `skipped` with exit 5, which counts as
  pending, never as progress.
- **External jobs are never signalled.** The owner pid is a shared, long-lived process. `cancel` only records the
  request; the owner sees it and runs `external bank --cause cancelled`. Reconcile banks `orphaned_external` only
  when the owner process is gone.

## Session freeze

`freeze set --root DIR [--session ID] [--harness claude-code|opencode] [--bash deny|unscoped]` writes a
per-session record that denies file edits outside `DIR` (and, by default, Bash) for that one session. `freeze clear
--session ID`, `freeze status [--session ID]` and `freeze check --session ID --tool T [--path P]` (the guard's
decision as JSON) complete the set.

- The record is `<freeze dir>/<harness>-<sha256(session id)[:32]>.json`, mode 0600, in a 0700 directory: the
  value of `AI_ARCH_FREEZE_DIR` (absolute), else `$XDG_STATE_HOME/ai-architecture/freeze`, else
  `~/.local/state/ai-architecture/freeze`. A freeze belongs to a session, not a project.
- The session id is `--session`, else `CLAUDE_CODE_SESSION_ID` (harness `claude-code`). With neither, `set` is
  refused: there is no session-less or global freeze. A harness other than `claude-code` and `opencode` is refused
  (`unsupported: no enforcement adapter for this harness`).
- **Enforcement is by an adapter and says so.** Claude Code: install [hooks/freeze_guard.py](hooks/freeze_guard.py)
  as a PreToolUse hook (matcher `Edit|Write|MultiEdit|NotebookEdit|Bash`, see INTEGRATION.md); then Edit, Write,
  MultiEdit and NotebookEdit outside the root and Bash are denied for the session named in the hook payload.
  Without the hook nothing is enforced. A symlink inside the root that points outside is outside. An invalid record
  denies. An unparseable payload is denied only while a record exists in the freeze directory. Subagent calls carry
  the parent's session id and are frozen with it; a dispatched worker has its own session id and is not.
  OpenCode: the `status` text says write, edit, patch, apply_patch and bash are denied "when the opencode/runtime
  plugin is loaded" (a later item). OpenCode exposes no session id to commands, so the id comes from the user, `status`
  labels it `unverified id`, and OpenCode child or `task` sessions have their own ids and are NOT frozen by the
  parent's record.
- `freeze status` and `freeze check` without `--harness` look the session up under both harnesses (a deny from
  either record wins); `clear` and `set` still default to `claude-code`.
- The guard reads the target from the tool's own key (`file_path`; `notebook_path` for NotebookEdit) and denies any
  other path-like key that points outside, a target starting with `~` or containing `$`, and always denies writes to the
  freeze directory, the guard file and any `settings*.json` (case-folded) that is a DIRECT child of a `.claude`
  directory under the root, so a symlinked `.claude` (existing, dangling, relative or chained) or its target directory
  cannot be used to create one, even when the file does not exist yet. Every rule is evaluated on BOTH readings of the
  path, `realpath(path)` and `realpath(normpath(path))`, and a deny on either reading wins; so `link/../x` cannot
  differ between the lexical and the resolved meaning. `settings*.json` deeper under `.claude` (`.claude/skills/x/`)
  is allowed. The `.claude` search skips `.git`, `node_modules`, `__pycache__` and `.venv`. `set` refuses `/` and any
  root that equals or contains the freeze directory or the guard file.
  Known limits (not closed): a hardlink to a settings file, a directory the guard cannot list, a `.claude` link that
  lives outside the root and points into it, and a `.claude` inside a skipped directory (other than a plain
  `<dir>/.claude/settings*.json`, which the name rule still denies).
  **Case-sensitive filesystems only (Linux).** Resolved paths are compared as exact strings; only the `.claude` and
  `settings` names are case-folded. On a case-insensitive filesystem (the macOS default, exFAT/vfat, ext4 with
  casefold) or with Unicode-normalization variants, a write that spells a `.claude` symlink target or a settings-link
  target in a different case (upper-case FOO for a link to foo) is allowed although it reaches the protected
  file. The `set` root-containment check has the same limit. Do not rely on the freeze guard there.
- `--bash unscoped` leaves Bash allowed; `status` reports "Bash unscoped: not enforced". Bash cannot be confined to
  a root, so a frozen session with Bash allowed can still write anywhere.
- Same threat model as the ledger: the record is a same-user file, so this stops a well-behaved session, not a
  worker with Bash.

## Context packets: `start --context`

`start` and `run` accept `--context PACKET.json --intent TEXT`. The packet is the JSON produced by
[context_packet.py](../../skills/context/scripts/context_packet.py) (schema_version 1). Before anything is launched
the consumer validates it: the exact documented key set (`note` optional), `project` absolute and inside this
ledger's project root, empty `missing_paths`, no path in both `read` and `exclude`, no repeated `read` path, and
every `read` path safe (relative, no traversal, no symlink at any step, a regular file). The budget is a hard
limit: `sum(bytes) // 4 > budget_tokens` refuses, where the producer only warns. A refusal leaves no job, no spool
file and no run directory.

Exactly the `read` files are read, in order, as UTF-8 without following symlinks. The prompt is the intent, then
each file as `### <path>` and its content inside `<untrusted-file-content>` fences (any fence text inside the
content is removed), then `Do not load:` and the `exclude` list. The job record gains
`context: {packet_sha256, files: [{path, sha256, bytes}], estimated_tokens, budget_tokens}`; it is `null` for a job
without a packet. The check and the read are not raceproof against a concurrent filesystem change (the producer states
the same limit). `--context` goes with `--intent`, not `--prompt-file`.

## What is not here

The OpenCode plugin is not implemented. A child's token budget is judged at
banking, not enforced inside a worker (see above); a Claude Code seat's `max_budget_usd` stays in the dispatch config.
`descriptor.json` claims `planned` with no evidence: native evidence is collected separately.
