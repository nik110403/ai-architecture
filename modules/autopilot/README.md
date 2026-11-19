# autopilot

An optional, opt-in runner that works through a plan pack on its own, within a budget, and stops at every human gate. Stdlib
only, Python 3.9 or later, Linux. Descriptor claim: `planned` (see [descriptor.json](descriptor.json)); per-capability state is in `../../capabilities/registry.json`: fixture-tested with a fake
harness; the live pilot is **NOT RUN**.

```
python3 -B modules/autopilot/autopilot.py kickoff --repo R --plan plans/topic --provider fake --fake-bin DIR --model NAME --budget-tokens N
python3 -B modules/autopilot/autopilot.py status --json --plan-id ID
python3 -B modules/autopilot/autopilot.py pause|stop|resume|refreeze|clear-corrupt --plan-id ID
```

## Off by default

Installing or importing this module starts nothing: no scheduler entry, unit, timer or cron line is ever created, and no
command other than `kickoff`, `resume` and `--detach` starts a worker. `kickoff` runs in the foreground; `--detach` only
re-executes the controller in a new session and records its pid. The hidden `_controller` subcommand that `--detach` runs
refuses to start unless it holds the single-use launch nonce the launcher put in its environment (only the hash is kept in the
state, and it is cleared on use), so it cannot be used to restart a run around the checks of `kickoff` and `resume`. A person who wants a timer reads
[INTEGRATION.md](INTEGRATION.md). The runner never pushes: it integrates onto its own branch `autopilot/<plan-id>`, never onto
yours, and merging that branch is your act.

## What it does

A human starts it on one pack. At kickoff it freezes, per SPEC item, a hash of the Acceptance, Verify, Touches, Needs, Gate and
Blocked-by lines, the ordered item-id list and the QUESTIONS file. Each round it:

1. checks, in this order, that your branch did not move (`branch-moved`), that the runner worktree is clean, and that nothing
   frozen changed outside an accepted round (`verify-drift` for Acceptance, Verify, Touches or the item list; `gate-changed` for
   Needs, Gate, Blocked-by or QUESTIONS);
2. picks the first item in SPEC order that is not done, has `Gate: auto` (absent counts as auto; `human` and anything
   unrecognised do not), has every Need done and no open `Blocked-by`. With nothing left it stops `waiting-human` (exit 0) or `done`;
3. dispatches one worker through [continuity](../continuity/README.md), which calls the
   [dispatcher](../orchestration/README.md). With `--max-workers K` it launches up to K items with pairwise disjoint Touches
   and integrates them one after another;
4. rejects the round when the worker's commits touch a path outside the item's Touches (`touches-violation`) or edit a frozen
   line (`gate-changed`, `verify-drift`); nothing is integrated;
5. builds the candidate tree from the object store (no checkout, so no hook or filter runs) and runs the item's frozen Verify
   command inside [the sandbox](../isolation/README.md). A sandbox that is unavailable stops the run `unverifiable`; Verify never
   runs unsandboxed;
6. on a pass, fast-forwards the runner branch and marks the item done in the tracker. On a failure it retries: fix rounds are
   counted per lineage (the `Lineage:` root, else the item), capped by the root's Stakes (HIGH 2, otherwise 1), and survive
   resume and refreeze. The next failure stops `verify-failed`.

Stop reasons: `done`, `waiting-human`, `paused`, `stopped` (exit 0); `budget`, `usage-unknown`, `verify-failed`,
`verify-drift`, `gate-changed`, `touches-violation`, `unverifiable`, `branch-moved`, `dirty-tree`, `state-corrupt`, `error` (exit 1);
`fake-binary-changed` (exit 2). Exit 2 is also a refused input, exit 3 is busy or not authorised.

## Safety statements (read these)

- **The live pilot is NOT RUN.** `--provider live` is built and refusal-tested only. A human starts a live pilot, on a TTY,
  with `--i-accept-spend`, a finite budget and a typed confirmation equal to the plan id, outside any agent session. This is a
  tripwire against an agent starting it by accident, not a boundary against a same-user process that fakes a TTY. It also
  refuses when an agent environment variable is set or an agent process is an ancestor. A pilot needs separate human
  authorisation; the build never started one.
- **Workers are not contained.** Workers run through the dispatcher with the harness in `dontAsk` mode and the role's tools
  pre-allowed, Bash included: no prompt ever appears, the network is on, and the worker runs as your user. A worktree is not a
  boundary. Only the runner's Verify step runs inside bwrap. The gates plugin is a tripwire and is not relied on. This is a
  recorded gap, `worker-containment`, disposition `incomplete`, in the
  [assessment](../control-plane-adapters/ASSESSMENT.md): the harness needs the network and the sandbox has no network mode.
- **Unknown usage or cost stops the run.** A round with no usage, partial usage or an unpriced model stops `usage-unknown` and the
  round is not accepted. So does a cancelled, interrupted, orphaned or lost round, or one that never started, in any budgeted run
  (a round you cancel with `stop` is the exception: the run is already stopped). A pessimistic charge goes against a token budget:
  the whole per-round cap, which is what was left when the round began, so after such a stop `resume` needs a larger
  `--budget-tokens` (a person's decision). A USD budget is not charged, the run just stops. No price table ships: a USD budget needs
  `--prices FILE` from you. An unbudgeted run is allowed only with `--provider offline`; a budgeted offline run stops
  `usage-unknown` at round 1 because a local command reports no usage. `fake` and `live` need a finite budget.
- **Fake provider.** `--provider fake` needs `--fake-bin DIR` whose `claude` carries the marker `AI_ARCH_FAKE_HARNESS`, is the
  first `claude` on PATH and is pinned by sha256; it is re-checked before every round and on resume (`fake-binary-changed`).
  A fake binary is refused as a live one.
- **A receipt is evidence against mistakes, not against a same-user forger.** The state is strict JSON with a hash chain,
  written atomically; anyone running as your user can still rewrite all of it.

## Control and state

`status` is read-only: it takes no lock and writes nothing, and works while the controller runs. `pause` finishes the current
round, then stops `paused`. `stop` verifies the controller (pid, start ticks, boot id) before signalling, then the
controller drains continuity, cancels in-flight workers, banks them `cancelled` and records unlaunched items as queued; a
queued item is launched exactly once on `resume`. A second `kickoff` or `resume` while a controller runs exits 3 `busy` and
spawns nothing. A controller that died is adopted by `resume` only after `continuity reconcile` has settled its in-flight
job, which is banked once and never rerun; the round's checkpoint Next is consumed once. `refreeze` and `clear-corrupt` need a
TTY and the typed plan id. Corrupt state parks the run (`state-corrupt`) and every command except `status` and `clear-corrupt`
refuses.

State lives in `${XDG_STATE_HOME:-~/.local/state}/ai-architecture/autopilot/<plan-id>` (0700, never a symlink, never a
directory this tool did not create). Printed output never contains that path, your repository path, your home directory, your
user name or the host name. Schemas: [state](schemas/state.json), [status](schemas/status.json), [receipt](schemas/receipt.json).

## Tests

`python3 -B -m unittest discover -s modules/autopilot/tests`. Offline: a fake harness stands in for every model, PATH starts
with sentinels that fail loudly, and no test needs network or money. Sandbox cells skip with a printed reason only when the
sandbox probe fails (`AIARCH_REQUIRE_SANDBOX=1` makes that a failure).
