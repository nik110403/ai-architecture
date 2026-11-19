# writing

A contract layer for prose work. It is not a writer. It routes a piece of prose to a register, checks
that a draft keeps the source facts and the author's stated intent, keeps a user's own edits across
revisions, moves a generic style profile in and out with rollback, and talks to an optional external
writer through a declared command.

The module generates no prose and makes no model call. It has no send, publish, upload or training
path and no flag for one. It never writes to a destination: `route` always reports
`destination_updated: false`. Everything it writes goes under `--out` or `--profile-dir`.

## Commands

    python3 modules/writing/writing.py registers
    python3 modules/writing/writing.py validate-registers FILE
    python3 modules/writing/writing.py route --register R --destination D --writers W
    python3 modules/writing/writing.py check --source S --draft D --register R --out O [--previous P --edited E]
    python3 modules/writing/writing.py profile export --profile-dir P --out F
    python3 modules/writing/writing.py profile import F --profile-dir P
    python3 modules/writing/writing.py profile rollback --profile-dir P
    python3 modules/writing/writing.py writer status --writers W --name N
    python3 modules/writing/writing.py writer run --writers W --name N --register R --destination D \
        --source S --out O --allow-writer [--previous P --edited E]

Exit codes: 0 ok, 1 check failed or register table invalid, 2 usage or refused input, 3 nothing to
restore or the writer failed.

## Registers

`registers.json` lists seven registers (correspondence, chat, essay, op-ed, academic, personal,
fiction). Each has a purpose, generic rule-set names and the registers it may inherit from. Fixed
constraints, enforced by `validate-registers`: correspondence never inherits from essay or academic;
personal and fiction inherit from nobody and apply no nonfiction rule set; no register defaults to
essay.

## Fidelity check

`check` writes fidelity.json and exits 1 on any miss.

- Facts come from the source: numbers (with a unit or a percent sign), quoted strings, URLs, DOIs,
  citekeys and spans marked `[[like this]]`. Each must appear in the draft on token boundaries, so
  `4.2%` does not match `14.2%` and a URL cut or extended by one character does not match.
- Intent is the source line starting `Intent:`. Its text must appear verbatim in the draft.
- User edits are the lines that changed from `--previous` to `--edited`. Added or changed lines must
  be in the draft (`lost` lists the ones that are not); lines the user deleted must stay out
  (`resurrected`).
- The file also carries a `review` object: register, the rule sets the register applies, the writer
  name and the verdict.

## Profiles

A profile is `{version, registers: {R: {prefer, avoid}}, samples: [{register, sha256}]}`. Only hashes
of samples are stored, never sample text. Import validates the schema, refuses unknown keys, non-string
items, duplicate keys, files over 64 KB, private paths and email addresses, then backs up the current
profile inside `--profile-dir` before an atomic write. Rollback restores the newest backup.

## Personal writers

A personal writer is an external command you declare in a writers.json:

    {"writers": {"name": {"argv": ["python3", "./my_writer.py"], "registers": [...], "destinations": [...]}}}

A leading `./` in an argv item is resolved against the directory of writers.json. The module picks a
writer only from what it reports itself: `writer status` runs `argv --describe` (10 s timeout, no shell,
environment limited to PATH, LANG and HOME, nothing on stdin) and shows the registers and destinations
from that output, never the claim in writers.json. A writer must answer `--describe` offline, with no
model call. If no available writer reports both the register and the destination, `route` returns
`assistant-draft` with a limitation sentence, and `writer run` exits 2 without running anything.

`writer run` spawns the writer only with `--allow-writer`, passes the source on stdin together with
`--register` and `--destination`, writes its stdout to draft.md, and runs `check` on it.

Keep any personal corpus, learned weights and voice rules in your own writer. They do not belong in this
repository, and the module has no place to put them.

See `examples/writing/` for an offline walkthrough with a template stub writer.
