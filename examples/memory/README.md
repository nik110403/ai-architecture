# Memory example: curated notes plus episodic recall

`python3 -B examples/memory/run.py --offline` copies the fictional notes from `modules/memory/example` into a temp
directory, captures the fictional session in `modules/memory-backends/fixtures/session.jsonl` into a temp store, and
prints two sections:

- `## curated`: the exact output of `curated.py query` (notes with citations), produced by running it as a subprocess.
- `## episodic`: observed candidates from the store, each `authority: "observed"`, `trust: "untrusted-data"`, no citation.

`--no-backend` shows curated search unchanged with episodic `unavailable`. `--workdir DIR` builds in a directory you
keep. Nothing is read from or written to your home, and there is no network or model call.
