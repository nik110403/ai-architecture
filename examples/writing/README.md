# writing example

An offline walkthrough of `modules/writing`. A template stub writer (`stub_writer.py`, no model, no
network) renders `source.md` into three registers; each draft is checked for facts and intent; one
register and destination pair that no writer supports is routed to an assistant draft; a style profile is
exported, imported into a fresh directory and exported again byte-for-byte.

    python3 examples/writing/run.py --offline --out /tmp/writing-demo
    python3 examples/writing/verify_receipt.py

`run.py` prints one line per step and `writing: OK`, and writes receipt.json. `verify_receipt.py` re-runs
it in a temporary directory and exits 0 only if every step is present and passed; with `--receipt FILE`
it checks an existing receipt and the draft hashes beside it.
