# Integration, status, removal

Install: copy `modules/autopilot/`; it needs [continuity](../continuity/README.md), the [dispatcher](../orchestration/README.md),
the [sandbox](../isolation/README.md) with bubblewrap, and git. Nothing runs until you run `kickoff`.

To run it on a schedule, you create the entry yourself. `modules/maintenance` prints the unit or cron text for you to review
(`maintenance.py scheduler plan`) and installs nothing without your named directory; point it at `autopilot.py resume
--plan-id ID`. This module never creates, enables or starts a scheduler entry.

Detached runs: `kickoff --detach` (and `resume --detach`) re-execute the controller in a new session. With
`--resource-limits FILE` the controller runs inside the optional resource adapter's scope, see
[control-plane-adapters](../control-plane-adapters/README.md). `--resource-limits` needs `--detach`: a foreground `kickoff` with it,
or a foreground `resume` of a run started with it, is refused (`resource-limits-need-detach`) rather than run unbounded.

Status: `claimed_support` is `planned` in [descriptor.json](descriptor.json); the live pilot is NOT RUN.

Removal: delete `modules/autopilot/`, the state directory for the plan id (`status` prints the id), the worktrees listed by
`git worktree list` under it (`git worktree remove`) and the branch `autopilot/<plan-id>`.
