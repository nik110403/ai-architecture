# Continuity fixture

Run from the collection root: `python3 -B examples/continuity/run.py`.
The script uses a temporary directory, starts and completes one journal job,
persists a fixed Next, and executes that Next in a fresh process. Another fresh
process checks the summary total of 10. It does not dispatch an AI worker or
execute arbitrary checkpoint text. No user project is inspected or modified.

The optional tool is [journal.py](../../skills/continuity/scripts/journal.py).
After selectively copying the continuity skill, invoke that script explicitly
with an existing project directory, for example:

```sh
python3 -B .claude/skills/continuity/scripts/journal.py resume --target . --json
```

Mutating subcommands are `start`, `finish`, and `checkpoint`; inspect `--help`
before use. This is a manual companion, not native harness continuity.
