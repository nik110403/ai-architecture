# Integration

- Contracts: `descriptor.json` declares `browser` 1.0.0 as `declared-untested`; the descriptor claim is `planned`; registry rows 31 (`partial`) and 32 (`planned`) are in `../../capabilities/registry.json`.
- Reuse: protocol, exit table, single-use claim and receipts are `../browser`'s, unchanged. `ops.py` registers the
  `chrome` backend in `backends.BACKENDS` for the duration of one command only (restored in `finally`); nothing loads a
  backend from the environment, the command line or a plugin path. `extension` and `desktop` are never backends.
- Roles: `agents/browser-worker.md` stays read-only. Acting still requires the approval chain from `../browser`.
- CI: the repo verify loop does not run these tests. Proposed step:
  `python3 -B -m unittest discover -s modules/browser-operations/tests && python3 -B examples/browser-local/verify_receipt.py`.
- Follow-up (not done here): `../browser/README.md` still says the real backend is "(U10)"; registry rows and COVERAGE
  are unchanged.
