# Integration, status, removal

Install: copy `modules/research-integrity/` together with `modules/research-adapter/` (the handshake is loaded by
path from the sibling directory) and, for the demo, `examples/research-companion/` (which holds
`examples/research-companion/verify_receipt.py` and `mini_study/`). Nothing else is needed; the module is stdlib only.

Companion: install provenote 0.2.x separately and pass `--companion <plugins/provenote directory>` to `init` and
`check`. Only `init` (when it scaffolds) and `check` run companion bins. Without a companion the quote and claim
checks are UNAVAILABLE, the overall status is INCOMPLETE and `finalize` and `release` refuse.

Demo: `python3 examples/research-companion/verify_receipt.py` runs the lifecycle on the synthetic mini study
without a companion and prints `companion: NOT RUN (...)`; with `--companion DIR` it also reaches `release` PASS.
`AIARCH_REQUIRE_COMPANION=1` turns a missing companion into an error.

Status: `claimed_support` is `planned` in `descriptor.json` and the provenote dependency is declared and
untested. Registry rows, the contracts mapping and COVERAGE are not changed by this module.

Removal: delete `modules/research-integrity/`, `examples/research-companion/verify_receipt.py` and
`examples/research-companion/mini_study/`. Receipts under a project's `.research/` and files under your `--out`
are ordinary files and stay.
