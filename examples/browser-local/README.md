# browser-local

Runs both journeys of `modules/browser` against a loopback page. No network, no real browser, no model.

```bash
python3 -B examples/browser-local/run.py            # state under ${XDG_STATE_HOME:-~/.local/state}/ai-architecture/browser-local
python3 -B examples/browser-local/run.py --out DIR  # DIR must be new, empty, or hold an earlier run
```

It serves `site/form.html` (a "library card request" form plus a paragraph that tries to give the reviewer an
instruction) on 127.0.0.1, runs the form-mode journey (the script plays the user; `--ask` prompts instead) and then
the agent-mode journey with `reviewer.py` as a separate process, and exits 0 only when both receipts verify.
Each run writes to `runs/<id>/`: the artifacts, one receipt per journey and a
server log (what the server actually received). A latest-run pointer file names the last run.

Hooks for tests: `--mutate-before-act dom|revision|login` changes the page between approval and action, so act
stops (exit 3) with no submission; `--port-file F` writes the listening port.

The page text is untrusted data; the injection paragraph appears only under `untrusted_text` in the survey. A
receipt is evidence against mistakes, not against a same-user forger.

## Lifecycle

`lifecycle.py` and `verify_receipt.py` exercise `../../modules/browser-operations` on the same loopback setup, with
`site/lifecycle-form.html`. `python3 -B examples/browser-local/verify_receipt.py` runs the journey through `../../modules/browser-operations/ops.py`
with the fixture backend, and again with real headless Chrome when Chrome is installed, verifies every receipt and
prints `real_backend: ran` or `real_backend: NOT RUN (<reason>)`. Set `AIARCH_REQUIRE_BROWSER=1` to make a missing or
unlaunchable Chrome an exit 1. `--receipt R` verifies one receipt (a tampered one exits 1). Nothing leaves 127.0.0.1.
