# Design pipeline example

A small notes page is built, rendered in real headless Chrome, criticised, revised and rendered again, offline. The front
door, record, design-builder, render, design-critic, revision, render, critic and receipt run through
`../../modules/orchestration/dispatch.py run`; every seat is a `local-command` seat running an authored, deterministic
worker from `workers/`. Rendering uses the Chrome launcher from `../../modules/browser-operations/chrome.py`. No model, no
harness, no outside network connection.

```bash
python3 -B examples/design/run.py --offline [--out DIR]          # 0 PASS, 1 FAIL, 2 refused, 3 NOT RUN
python3 -B examples/design/verify_receipt.py [RECEIPT]           # no argument: run the example into a temp dir, then verify
```

What happens: the builder stand-in commits a seeded first pass (`fixtures/bad`, eight real defects) as C1. The pipeline
renders it (R1 fails every defect check), builds the critic's bundle and dispatches the critic stand-in, which turns each
failing check into a blocking finding that cites an evidence id. The builder stand-in then applies one authored patch per
finding as C2, a child of C1. R2 passes, the second critic has no blocking finding, and receipt.json records the whole
chain. `verify_receipt.py` re-renders both rounds and re-derives the verdict.

Failure cells (used by the tests): `--leave-unfixed CHECK`, `--delete-element`, `--revisions N` (a third revision is refused),
`--canary TEXT`, `--plant-symlink PATH`, `--record FILE`, `--lane`.

## Limits

- The workers are stand-ins; no model judgement is exercised. Whether a real design-builder or design-critic does good work is NOT RUN.
- The renderer runs the project's own page scripts, so use it on your own local source only.
- Independence means input construction, not a sandbox.
- A receipt is evidence against mistakes, not against a same-user forger.
- On this host Playwright is NOT RUN; interaction and trace diagnosis here come from Chrome over CDP.
- The fixtures are intentionally flawed pages; never ship them.
- The example tokens are generic and marked as such; they are not a house style.
