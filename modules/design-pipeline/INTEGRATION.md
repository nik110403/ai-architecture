# Integration

## Roles

The pipeline uses `design-builder` (write role, runs in a dispatch worktree) and `design-critic` (read role: Read, Grep, Glob; no shell). Neither role is changed. Configure seats in a dispatch config exactly as for any other role; the example uses `local-command` seats running authored workers (see `../../examples/design/README.md`).

## Browser

Rendering imports the browser-operations launcher by path (`../browser-operations/chrome.py`) and subclasses its backend with page scripts on, which that launcher keeps off. The sink proxy, resolver rules and target gating belong to the launcher (the launcher is subclassed to also send loopback to the sink; dedicated workers are resumed at once because Fetch cannot be enabled on a worker session). The page is fulfilled over CDP from the verified tree, not served. Set `AIARCH_CHROME` to choose the binary; `AIARCH_RENDER_TIMEOUT` (seconds) bounds launch and navigation. `AIARCH_DESIGN_FAULT=<check>` is a test seam that makes one check's data collection fail as a browser error.

## Using it on a project

1. Write a design record (`schemas/design-record.json`) with the project's own tokens and a product record when the lane is `product`.
2. Commit the surface to a repository and run `design.py inspect --tree DIR --record R` for a quick check, or drive the full sequence like `../../examples/design/run.py` does.
3. Keep receipt.json with its run directory; `design.py verify` needs the repo, renders and bundles beside it.

## Exit codes

0 PASS, 1 FAIL or invalid receipt, 2 refused (bad input, unknown lane or adapter, round budget, links in the tree), 3 NOT RUN.

## Registry

Registry rows 45, 46 and 50 are `partial` (see `../../capabilities/registry.json`); `descriptor.json` claims `planned` with no evidence, because the contract accepts `partial` only with a native harness receipt.
