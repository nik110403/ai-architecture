# Integration, status, removal

Install: copy `modules/research-adapter/` (and, for the demo, `examples/research-companion/`). Nothing else
is needed; the module is stdlib only and needs Python 3.9 or newer.

Companion: install provenote 0.2.x separately and point `--companion` at its `plugins/provenote` directory.
Run `python3 research_adapter.py handshake --companion <companion>` first; `integrated: true` means both
capabilities are available. The adapter does not download or vendor the companion.

Demo: `python3 examples/research-companion/run.py --offline --companion <companion> --out <OUT>` serves
only the canned responses in `examples/research-companion/fixture.json`. Without `--companion` it prints that the companion is
unavailable and exits 0.

Status: `claimed_support` is `planned` in `descriptor.json`; the provenote dependency is declared and
untested. Registry rows 33, 34 and 35 and the contracts mapping are not changed by this module.

Removal: delete `modules/research-adapter/` and `examples/research-companion/`. Files written under your
`--out` and the bibliography you passed are ordinary files and stay.
