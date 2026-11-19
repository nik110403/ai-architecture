# Integration

- Contracts: `descriptor.json` declares `core-contracts` 1.0.0 as `declared-untested`; the descriptor claim
  is `planned`. Registry rows 29 (`browser-modes`) and 30 (`state-bound-actions`) are `partial` in `../../capabilities/registry.json`.
- Roles: `agents/browser-worker.md` stays read-only (survey and report). Acting requires the approval chain here.
- Continuity: no ledger integration. The reviewer is a plain subprocess, not a dispatched job, and the receipt is a
  JSON file in the output directory.
- Backends: implement `Backend` (`name`, `probe`, `survey`, `can_submit`, `submit`) in `backends.py` and register it
  in `BACKENDS`. The `cdp` entry is an availability probe only and never launches or drives a browser.
- CI: the repo verify loop does not run `modules/browser/tests`. Proposed step:
  `python3 -B -m unittest discover -s modules/browser/tests`.
