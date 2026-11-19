# design-pipeline

A rich-UI pipeline with rendered evidence: a front door that validates a design record, a design-builder, a real headless
Chrome render with inspection, an independent design-critic that reads only a bundle of rendered artifacts, a revision
round, and a receipt that a verifier can re-derive. Stdlib only. It adds no role and no test framework: it uses the
existing `design-builder` and `design-critic` roles through `../orchestration/dispatch.py`, and Chrome through the launcher
in `../browser-operations/chrome.py`.

```bash
python3 -B modules/design-pipeline/design.py route --lane product --record R --product P [--run DIR]
python3 -B modules/design-pipeline/design.py inspect --tree DIR --record R [--adapters accessibility,motion,style] [--allow-playwright]
python3 -B modules/design-pipeline/design.py verify RECEIPT           # 0 PASS, 1 FAIL or invalid, 2 refused, 3 NOT RUN
python3 -B modules/design-pipeline/design.py diagnose RUN             # names the failing trace steps of round one
python3 -B modules/design-pipeline/design.py deps [--skills-dir DIR] --json
python3 -B modules/design-pipeline/playwright.py status|run|trace
python3 -B -m unittest discover -s modules/design-pipeline/tests && python3 -B examples/design/verify_receipt.py
```

## What a pass means

`PASS` needs all of: the final render ran in real Chrome; every check passed; every screenshot is a valid PNG of its
viewport size with at least two distinct pixels; the final critic has no blocking finding; and `verify` re-rendered every
round and agrees. Otherwise `FAIL`, or `NOT RUN` when no browser rendered (exit 3, never 0). A visual PASS without rendered
evidence is not possible: a missing, crashing, hanging or failing browser, or a check whose data the browser could not collect, is NOT RUN. A check that errors for another reason (for example contrast on a background image) is never a pass and gives `FAIL`.

| Piece | File | What it does |
|---|---|---|
| render | `render.py` | the files of a commit read from the object store (every object re-hashed in Python; links, odd modes and unsafe names refused) into a temp dir and fulfilled over CDP to a fresh Chrome at a 127.0.0.1 origin where nothing listens, through the same-origin Fetch gate; trusted `Input.dispatchKeyEvent` keys; screenshots, AX tree, geometry, computed styles, trace.jsonl |
| checks | `checks.py` | pure functions: a11y-name, contrast, overflow, keyboard-reach, focus-visible, interaction, required-present, external-request, reduced-motion (motion adapter), tokens (style adapter); result is pass, fail, blocked or error and an error is never a pass |
| pipeline | `design.py`, `pipeline.json` | stage order, lanes (product, polish-only), at most two revision rounds, bundle, receipt, verifier |
| adapters | `adapters.json` | narrow local checks, each with an attributed upstream slot that never changes a result |
| playwright | `playwright.py` | contracts and a spawn-gated adapter; NOT RUN on a host without Playwright |
| records | `schemas/` | design record, product record and findings; unknown keys are refused |

## Checks

- `keyboard-reach` presses real Tab keys from a fresh load. Reaching the end of the page (focus leaves the document) ends the walk and is never counted as reaching a target. A key handler that stops Tab is a trap and fails.
- `focus-visible` compares a screenshot of the target's box focused and unfocused. An unreached target is `blocked (keyboard-reach)`, never a pass, and the failure is attributed to keyboard-reach.
- `contrast` is an `error` (not a pass) when text sits on a background image or when no ancestor has an opaque background; set the page background.
- `interaction` focuses the target with Tab, sends the record's keys as trusted events and checks the record's expectation.
- `tokens` compares color, opaque background, border colors (when drawn) and the first font family with the record's tokens.

## Critic independence

The critic is dispatched with its own run directory, its own seat and job, and a working directory that is the bundle: the
screenshots, inspection.json, trace.jsonl, the source tree of the commit and the record, listed with sha256 in
bundle.json. Its prompt is built only from bundle.json and the record. The builder's revision prompt embeds the findings and their
sha only. `verify` recomputes the bundle source from the commit and requires a closed file set. This is input
construction, not a sandbox: `local-command` seats are not sandboxed (see the threat model in `../orchestration/README.md`).

## Limits

- The workers in `examples/design` are authored stand-ins; no model judgement is exercised. A live-model run is NOT RUN.
- The renderer runs the project's own page scripts, so use it on your own local source only. It stops and records what it can see, within a window: requests through Chrome's Fetch gate (same-origin GETs are answered from the tree, everything else is failed and logged), the Content-Security-Policy `connect-src 'self'; report-uri` header on every answer (workers inherit it; each violation comes back as a report the renderer records itself, so a page cannot wipe it), and the sink proxy, which receives every other socket (loopback is not exempt) such as WebSocket and WebRTC. The result is "no attempt seen during the run": the run ends after a 0.5 s drain, so an attempt later than that, or one that never reaches the browser's network stack, is not seen. Chrome features that ride outside these channels are not claimed to be gated.
- Independence means input construction, not a sandbox.
- A receipt is evidence against mistakes, not against a same-user forger: anyone who can write the run directory can rewrite every file and the receipt together.
- Chrome's own background traffic reaches the sink proxy. Every sink attempt to a loopback address or an IP literal counts as the page's, and so does one naming a host that appears in the page sources; the rest is ignored.
- On this host Playwright is NOT RUN. The component contract (`@playwright/test` and the `experimental-ct-*` packages) is `unsupported here`; interaction and trace diagnosis come from the CDP renderer. Registry rows 45, 46 and 50 are `partial` (see `../../capabilities/registry.json`; row 50 lists `playwright` as an optional requirement).
- No taste defaults: no palette, font, ban list or house style ships here. The example tokens are generic and marked `"origin": "example"`.
- A different language, framework build step or non-HTML surface is out of scope.
