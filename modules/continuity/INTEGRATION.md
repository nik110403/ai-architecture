# Integration, status, removal

Install: copy these paths, nothing else is needed:

- `modules/continuity/`
- `modules/orchestration/`, `modules/contracts/`, `adapters/runtime/` and `agents/` (the continuity module
  starts workers through [dispatch.py](../orchestration/dispatch.py); see [its integration notes](../orchestration/INTEGRATION.md))

Write your own dispatch config modelled on [example.json](../orchestration/example.json), then:

```
python3 -B modules/continuity/continuity.py init --project /path/to/project
echo '.ai-continuity/' >> /path/to/project/.gitignore
```

`example-config.json` shows the settings `init` writes to `<project>/.ai-continuity/ledger/config.json`; edit that file, not
this one. Unknown keys are refused.

Claude Code hooks (optional, wired by hand in your Claude Code project or user settings; the scripts are stdlib only
and print nothing on any error). Use absolute paths to this checkout:

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "python3 -B /path/to/modules/continuity/hooks/session_start.py"}]}],
  "Stop": [{"hooks": [{"type": "command", "command": "python3 -B /path/to/modules/continuity/hooks/claude_usage.py"}]}]
}}
```

`hooks/session_start.py` injects the latest checkpoint's Next once per checkpoint and coexists with the gates plugin's tracker
injection; `hooks/claude_usage.py` records the session's context usage so `ready` and the handoff gate have a real number.
Both do nothing for a dispatched worker (the `AI_ARCH_JOB_MARKER` environment variable) and for subagents. Checkpoint with
`checkpoint` or `handoff` before ending a session; see the README.

Status: `descriptor.json` claims `planned` with no evidence. Native evidence (a real worker run per harness) is
collected separately; do not raise the claim without a matching receipt.

Check: `python3 -B -m unittest discover -s modules/continuity/tests`. The tests start real [dispatch.py](../orchestration/dispatch.py)
subprocesses with fake `local-command` workers on a temporary `PATH`, use temporary project directories, and never
launch a real model or touch the network. They need Linux (`fcntl`, `/proc`); on another platform they fail with a
message instead of skipping.

Typical loop from a coordinator: `start` (or `run`), keep working, `status` or `collect` to see arrivals,
`cancel` what is no longer wanted, `drain start` before restarting the harness and `drain wait` until
`DRAINED`, `reconcile` after a crash. Exit codes 6 and 7 mean the action happened: never retry the mutation, run
`status` (it banks any finished job) and read the ledger.

Session freeze (Claude Code): wire the guard as a PreToolUse hook in the settings of the session to be frozen, then
run `continuity.py freeze set --root DIR` from that session (it reads `CLAUDE_CODE_SESSION_ID`):

```
{"hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
  "hooks": [{"type": "command", "command": "python3 -B /path/to/modules/continuity/hooks/freeze_guard.py"}]}]}}
```

The hook prints nothing for a session without a record. Remove the freeze with `freeze clear --session ID`.
OpenCode enforcement comes from the `opencode/runtime` plugin (a later item); `freeze status` reports what is
and is not enforced. Context packets: `start --context PACKET.json --intent TEXT` (see the README).

Removal: delete `modules/continuity/` and the `.ai-continuity/ledger/` directory of each project. The ledger holds
job state, prompts and outputs only; the the journal file beside it belongs to `skills/continuity`.
