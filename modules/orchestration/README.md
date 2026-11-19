# Orchestration: intent to role to native worker

`dispatch.py` turns a request into one permitted worker launch on a native harness (Claude Code,
OpenCode, or a recipient-declared local command) and returns a typed result. It is an execution
adapter. It is not `scripts/route.py`, which only writes a report-only routing plan; this module
neither imports nor calls it.

```
python3 -B modules/orchestration/dispatch.py plan   --config modules/orchestration/example.json --role researcher
python3 -B modules/orchestration/dispatch.py status --config modules/orchestration/example.json
python3 -B modules/orchestration/dispatch.py run    --config modules/orchestration/example.json --role researcher \
    --intent "list the open questions" --run-dir ./runs --job-id first-run
```

With `example.json` as shipped (`allow_write: false`, `allow_spend: false`) the `researcher` role is a read
role and resolves to the free `local-echo` seat, so the commands above run without any flag. `plan --role
builder` shows the refusal: `builder` holds `Bash` and `Edit`, so it is a write role and `allow_write` is false.
The paid seats (`claude-main`, `opencode-main`) need real model names, `allow_spend: true` in the config AND
`--allow-spend` on the command line.

## Threat model: the worker is the same user

The worker runs as the user who ran the dispatcher. A role holding Bash (every write role, and every
`local-command` seat) can write, execute and use credentials anywhere that user can, on either harness. From
its worktree it can reach the main repository's `.git`, hooks included. It can read the dispatcher's
environment through `/proc/<ppid>/environ`. It can append to the dispatcher's captured stdout through
`/proc/<worker>/fd/1`, and it can stop or kill the dispatcher and then overwrite `<job-dir>/result.json` and
`<job-dir>/start.json`. So for a Bash-holding worker, `succeeded` and the job files are worker-attested, not verified.
OpenCode's `external_directory: deny` stops file tools and visible bash paths, not a script. Confinement needs a
sandbox or another uid; this module does not provide one. Write roles stay behind
`allow_write` (default off).

## Permission model (default denies everything)

- `--config FILE` is required. There is no default config search and no home scan. Unknown keys, wrong
  shapes, non-finite numbers and explicit `null`s for `max_budget_usd` and `worktree_root` are refused.
  `plan`, `run` and `status` refuse the same way: a typed `unsupported` record and exit 2. A refusal after
  the role is known (unavailable role file, empty prompt, unreadable prompt file) carries that role.
- A role launches only if it is in `permit`. `--role` wins; otherwise intent words must match exactly one
  role in `roles.json`. Zero or several matches are refused with the candidate list.
- **Access.** A `read` role holds none of Bash, Write, Edit. Any role holding one of them counts as
  `write`, which means gated: it needs `allow_write`, an explicit absolute `worktree_root` (owned by you and not
  group- or other-writable, like `--run-dir`), and a git repository at `--workdir`, and it runs in a fresh
  `git worktree add` on a new branch returned in the result (repository hooks are switched off for that
  command). A worktree keeps changes on their own branch; it is not a sandbox and confines nothing: the
  envelope says `dispatch.confinement: "none: same-uid worktree, not a security boundary"` for a write role and
  `"harness tool allowlist; reads anything the user can"` for a read role. Nothing is merged here.
- **What a read role does and does not restrict.** The restriction holds where the harness enforces the
  tool list (Claude Code, OpenCode). On a `local-command` seat the role is a gating label only: the command is
  whatever the recipient declared and can do anything. On Claude Code a read role can still read whatever the
  user can read, including credentials under `HOME` and the worker's own environment (`/proc/self/environ`,
  `env_allow` values included; `--allowedTools Read` is not scoped to a directory), and
  `researcher`'s web tools can send what it read out. A recipient who needs confinement runs the dispatcher
  inside a sandbox (container, separate user, no credentials in `HOME`); this module does not provide one.
- **Roles resolve by construction.** The role's prompt and tools come from `agents/<role>.md` in this tree;
  a missing file, or one whose tools disagree with `roles.json`, makes the role unavailable (refused).
  The dispatcher grants exactly the role's tool list; the role file itself grants nothing.
  Claude Code gets the role inline (`--agents FILE`, tools limited and pre-allowed, everything else denied) and
  runs with `--setting-sources user`, so your user settings stay active. OpenCode gets it inline as a primary
  agent with an allowlist permission set and project config disabled, and still loads your global config besides
  the inline one; `--auto` is never passed; the tool name `WebSearch` is mapped to OpenCode's `websearch`
  permission id. The inline config also sets `agent.title.disable: true` and `small_model` to the pinned model,
  so the harness's title call does not pick another model. No per-harness role install is needed. That inline
  config travels in the worker's environment and is never written to the envelope or `<job-dir>/start.json`.
- **The two harnesses do not report the same act the same way, so `permission_denials` is not comparable
  across them.** In the recorded runs (Claude Code 2.1.314, OpenCode 1.18.31, fake model endpoint), every
  Claude Code case below ended `succeeded` with 0 denials: a read role asked to write (nothing written), a
  write role asked to change an existing file outside its worktree (file unchanged), and a write role asked to
  create a new file outside its worktree (file created). On OpenCode the comparable acts (a tool outside the
  list, a write outside the worktree, refused by `external_directory: deny`) were each counted as 1 denial, so
  the job `failed`. A `succeeded` Claude Code job says nothing about whether the worker tried an act it was not
  given, or whether that act took effect.
- **Spend.** `claude-code` and `opencode` seats are paid. `run` needs `allow_spend: true` in the config AND
  `--allow-spend`. A miss on either gate refuses the whole request; it never falls through to a free seat.
  A `local-command` seat whose `command[0]` basename, or the basename of its resolved real path (symlinks
  followed), is `claude` or `opencode` also counts as paid. Wrapper scripts under another name that call a paid
  harness stay outside the check: they are the recipient's responsibility. Credentials are recipient-owned:
  only `PATH HOME LANG TMPDIR`, the seat's `env_allow` names, `AI_ARCH_JOB_MARKER` (see below) and the adapter's
  own variables are handed to a worker. That is what the dispatcher passes on, not a secrecy guarantee: a
  same-user worker can still read the dispatcher's own environment (threat model above). Nothing probes an
  account; `status` only looks up executables on `PATH`.
- **Paths.** Executables, including `git`, are launched by absolute path found on `PATH`. Relative `PATH`
  entries (including empty ones) are ignored, and the `PATH` a worker or a child receives contains absolute
  entries only, also when `PATH` is in a seat's `env_allow`. A `PATH` with no absolute entry becomes `/usr/bin:/bin`,
  never an empty one. This is deliberately not `shutil.which`, which honours relative entries.
- **Budget.** `max_budget_usd` is a Claude Code feature. On any other seat it is refused by name, because no
  adapter there can honour it.
- `--model X` is honoured exactly or refused; it never falls back to another model. A pin tries every seat with
  that model in order and lists every skip, pin mismatches included. Without a pin, the role's ordered seat list
  is walked and only seats unavailable before launch (no model, no executable) are skipped. A launched worker
  that fails is reported failed and never retried.
- The prompt goes to the worker on stdin, never in argv; no shell is used; stdout and stderr are captured through
  pipes, at most 10 MiB each. A worker that exceeds that is killed and fails, even with exit 0.
- **Containment: the dispatcher kills descendants still attached and reports survivors.** When the worker exits,
  times out, or the dispatcher is signalled, it kills the worker's process group, then the descendants it can
  still find. After a normal exit the group is killed while the worker is still an unreaped zombie
  (`waitid` with `WNOWAIT`, polled every 0.05 s), so its pid and process group id cannot have been reused; the
  worker is reaped afterwards. On timeout, signal or output overflow the group is killed while the worker is alive.
  Where `os.waitid` or `WNOWAIT` is missing, the group kill after a normal exit is skipped (the marker sweep and
  child list still run), and so is it when the dispatcher inherited
  `SIGCHLD` as ignored (the kernel reaps the worker itself, `waitid` raises `ECHILD`; the exit code is then not available and reads as 0). Under `run` it makes itself a child subreaper
  (Linux), so orphaned descendants, `setsid` and double forks included, become its children: it kills and reaps
  each by pid until none is left (bounded), then sweeps `/proc/*/environ` for the job marker. `dispatch.containment`
  is `{method, killed, survivors, unknown}`. `method` is `children+marker` (subreaper armed, `run` in the main
  thread), `marker` (library `launch()`, a non-main thread, the subreaper unavailable, or the caller already had children) or `group` (no `/proc`).
  `survivors` is a count only for `children+marker`; for `marker` and `group` it is `null`, because a sweep cannot
  see a process that cleared its environment (`env -i`, including a double fork plus `env -i`) or one that is
  non-dumpable. `unknown` counts every same-user process on the host whose `environ` could not be read, not
  only this job's, so it is usually above 0 and proves nothing about this job. The sweep has no time bound of
  its own: a process hung in uninterruptible I/O (a dead NFS or FUSE mount) can stall the read of its `environ`. In every mode a process that leaves the process
  tree (`systemd-run`, `tmux`) is outside the dispatcher's reach.
  Descendants killed are counted in `killed` and do not lower a success. `survivors > 0` lowers the outcome to
  `failed` with a note. A worker that keeps a pipe open past all this fails the job ("worker left a process holding
  the output pipe") after a bounded wait. One dispatcher process per job is a constraint on the caller: the
  child list is process-wide. Calling `main(["run", ...])` in-process arms the subreaper only when the caller has
  no children at that moment; with any child present the run uses the marker sweep (`method: "marker"`) and the
  caller's children are never killed or reaped. The caller's own subreaper flag is read before the run and
  restored afterwards (where it cannot be read, it is left off). A SIGTERM aimed at a process that calls
  `launch()` from a non-main thread is not contained, because a signal handler cannot be installed there; the
  lost and orphan rows of the reconciliation table are the recovery for that case. `modules/continuity` never does
  that: it starts every dispatcher as its own process, in its own session, in its main thread.
- **The marker** `AI_ARCH_JOB_MARKER` is the sha256 of the job directory's real path. It is not stored in
  `<job-dir>/start.json` or the envelope; a recovery step recomputes it from its own run dir and the job id. It is a
  discovery hint, not a credential: a same-user process can compute it too.
- An existing `--run-dir` must be owned by the effective user and not group- or other-writable.

## Output and job directory

`{"result": {...}, "dispatch": {...}}`. `result` is a `result` record from `modules/contracts`
(`event_id` equals the job id). Exit codes: 0 succeeded, 1 failed, 2 refused.

**The dispatcher judges only what it captured.** Each pump copies its pipe to the job directory's stdout and stderr files
(unbuffered, so a dispatcher crash loses nothing already read) and keeps the same bytes and a streaming sha256
in memory. The outcome, the harness checks, usage and the evidence hash all come from that in-memory capture;
the files in the job directory are copies. The verdict and the hash are computed from memory, never read back from
the files, so rewriting a copy afterwards changes neither. A same-user worker can still write into the stream while
it runs (threat model above), so for a Bash-holding worker the verdict is worker-attested. Worker stdout stays
untrusted data.

**Success needs a positive signal; a check can only lower an outcome to `failed`.** Parsing never raises (a
deeply nested or oversized value is just unreadable, which fails closed).

- `claude-code` succeeds only on exit 0 with stdout a JSON object whose top level has `type == "result"`,
  `subtype == "success"`, `is_error == false` and `permission_denials` absent or empty. (`subtype: "success"`
  with `is_error: true` is a real API-error shape, so neither field alone is enough.)
- `opencode` (`run --format json`, one event per line) succeeds only on exit 0, no top-level `error` event, no
  denial, and the last parsed event a `step_finish` with `part.reason == "stop"`. A denial is a `tool_use`
  event whose `part.state.status` is `error` and whose `part.state.error` starts with `The user has specified a
  rule` or `Model tried to call unavailable tool`; denials are counted in `permission_denials`, inputs are never
  recorded. Other tool errors (a missing file) are ordinary. Empty output, `step_start` only and
  `reason: "length"` all fail. The two fallback markers (`Falling back to default agent`, `auto-rejecting`) are
  looked for on stderr only; model text on stdout never changes the outcome.
- These prefixes and reasons match OpenCode 1.18.31 and Claude Code 2.1.314. A harness that changes them fails
  closed: the run is reported `failed` until the adapter is updated.
- `local-command` is judged by exit code alone.
- `dispatch.usage` is rebuilt from an allowlist, never passed through: `input output cache_read cache_write cost_usd`,
  `null` when none is present, never guessed. Both harnesses map into these keys; only finite numbers `>= 0` count,
  anything else is ignored. It is parsed whenever output was captured, including after a timeout. For OpenCode it counts the steps the harness reported; compaction and other auxiliary calls are not
  proven absent.
- Worker-controlled shapes cannot turn a launched job into `unsupported`: envelopes and job files are written as
  strict JSON (no `NaN`, no `Infinity`), and if the full envelope cannot be serialised a minimal one
  (`result` as a `failed` record, `job_id`, `outcome: "failed"`, `exit: 1`, `note`; no `dispatch`) is written and
  printed instead.

Each job lives in `--run-dir/<job-id>/` (`--job-id` optional, generated if absent, reuse refused), directory
mode 0700, files 0600.

Order: signal handlers (SIGTERM, SIGINT, SIGHUP) are installed first, then the harness version probe
(`<exe> --version`, `run` only; a signal ends it at once), then `<job-dir>/start.json` is written in state `starting`
(with the planned worktree path and branch), then `git worktree add`, then the worker is launched and
`<job-dir>/start.json` is rewritten as `running`. A failure before the launch removes any worktree it created and the job
directory and is reported as a refusal that carries the role. Once the worker exists the record says launched:
any later failure kills the worker and yields `failed` (exit 1), never `unsupported`; a `Popen` failure on a write
role removes the worktree and branch it created. If `<job-dir>/result.json` cannot be written the envelope still
goes to stdout, with a note. SIGTERM, SIGINT or SIGHUP kills the worker and writes a `failed` result; the handlers
are restored only after the result is written. A refusal names the caller's `--job-id` when it is a valid id
(config, role, seat or spend gate, empty or unreadable prompt, a job directory that cannot be created), except a
reuse refusal (that id belongs to another job) and an invalid id, which carry `job_id: null`. A caller that must
find its job still knows its own id; do not rely on the refusal to tell it. A timeout is
reported as "job timed out after Ns", not as a harness problem.

`<job-dir>/start.json` carries: `job_id role seat model harness harness_version argv workdir worktree started_at deadline
pid pgid state dispatcher_pid dispatcher_start_ticks worker_start_ticks`. `deadline` is `started_at` plus the
timeout. The two `*_start_ticks` are field 22 of `/proc/<pid>/stat` (`null` where unavailable) and guard
against pid reuse. The worker's environment carries `AI_ARCH_JOB_MARKER`; it is never recorded.

### Reconciliation (for a later recovery step)

`modules/continuity` (`continuity.py reconcile`) implements this table and is authoritative where the two differ
(identity rules, the stuck rule R2, the worktree rule W, the bounded marker sweep). The table below is kept for
readers of this module on its own.

One dispatcher process per job. Everything in `<job-dir>/result.json` and `<job-dir>/start.json` is the dispatcher's record; for a
Bash-holding role it is worker-attested (threat model above).

| Observed | Meaning | Action |
|---|---|---|
| `<job-dir>/result.json` present | finished, per the dispatcher | read it, but for a Bash-holding role it is worker-attested. A file present without a clean exit seen by your own `wait()` of a live dispatcher is unverified. `exit_code` -9 with `succeeded` is contradictory: treat as failed |
| dispatcher alive (pid and `dispatcher_start_ticks` match), not stuck by the rule below, not state `T` | running | leave it. The dispatcher legitimately runs about 25 s of cleanup after a timeout and the version probe (up to 10 s) precedes `started_at`, so a live dispatcher a little past the timeout is not stuck |
| dispatcher alive and stuck: state `T`, or, with the same boot id, `CLOCK_MONOTONIC` now minus the monotonic reading taken right after launch is greater than `timeout + 180 s` (`timeout` is `deadline - started_at` from `<job-dir>/start.json`, both written from one clock reading) | stuck, not running | wall-clock time is never compared with `deadline`, and process start ticks are not used here (they include all suspend since boot). A different boot id means the dispatcher is gone. With the monotonic reading, the boot id or `deadline` missing, a live dispatcher younger than 30 s is running and an older one is unresolved: never killed on missing inputs. No process enforces `deadline` once the dispatcher is stopped: send `SIGCONT`, then `SIGTERM`, and wait up to 30 s; the dispatcher then contains the worker's descendants and writes `<job-dir>/result.json` itself. Only if it is still alive, `SIGKILL` it; that skips its containment, so continue with the next rows |
| dispatcher gone, worker alive | orphan | verify the worker: the marker `AI_ARCH_JOB_MARKER=<sha256 of the real job dir>` is in its `/proc/<pid>/environ` AND its pid and start ticks match `<job-dir>/start.json`. Then `SIGKILL` its process group (`pgid`) while the worker is still alive, then the worker, then run the marker sweep below. Unreadable `environ` or null start ticks: identity unknown, do not kill, keep the worktree, record `failed` |
| dispatcher gone, worker gone, no `<job-dir>/result.json` | lost | descendants may still be alive: run the marker sweep below (never signal the old `pgid` once the worker is gone). Exit code unknown; output up to the crash is on disk; record `failed` |
| state `starting`, `pid` null, dispatcher alive | launching (`git worktree add` can take up to 120 s) | leave it; apply the `deadline` rows above |
| state `starting`, `pid` null, dispatcher gone | a worker may exist from the launch window | run the marker sweep below; if it found any process, record `failed`, else treat the job as not launched |
| job dir exists, no `<job-dir>/start.json` | not launched (the window during the version probe) | do not reuse the job id |

**Marker sweep.** `SIGKILL` every process of your own uid whose `/proc/<pid>/environ` holds the exact entry
`AI_ARCH_JOB_MARKER=<marker>`, reading field 22 of `/proc/<pid>/stat` before and after the `environ` read and
skipping the process if the two differ (pid reuse). Repeat until a pass finds none; only then remove the
worktree. A descendant that cleared its environment, is non-dumpable or left the process tree cannot be found
this way, so a sweep that finds nothing does not prove nothing is left.

Recovery of a lost job means a recorded failure and a re-dispatch, not a resumed worker.

## Status

`status` lists seats (executable found, spend permitted, availability) and, per role, whether the role file
resolves (`role_available`, `reason`), so it agrees with `plan` and `run`.

`browser-worker` only declares a role; browser control is supplied by the recipient and owned elsewhere.
`roles.json` must agree with the frontmatter in `agents/` (a test checks it).
