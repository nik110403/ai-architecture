# Provenance: memory-operations

Each section below is one source unit. The code is a stdlib re-authoring against this collection's public stores
(`modules/memory-backends` observation stores and `modules/memory` curated memory), not a copy.

## memory-promote

Source: author's private operator repository, bin/memory-promote
Source-sha256: 7d38d2ada67f8890ccac3e2181f016bf038ca119fd02657bf24633806bab6067
State: re-authored

Deltas:
- Ported as `promote.py` (`stage`, `approve`, `apply`, `rollback`). The source wrote the note before its receipt and had no rollback; apply now snapshots MEMORY.md, creates the note with O_EXCL|O_NOFOLLOW, runs memcheck fix-index, then writes the receipt, and any failure undoes the write.
- The source's authority-user tty rule becomes "approval is refused from agent sessions" (AI_ARCH_JOB_MARKER, CLAUDECODE, CLAUDE_CODE_*, OPENCODE*). This is not authentication. The `user` and `feedback` types still need `--type-confirm` naming the type.
- An approval binds to a hash recomputed from the candidate content on every use; a stored hash field or an `approved` flag in the candidate is never trusted.
- Name collisions (an existing file or a symlink at the target, or a symlinked memory directory) and a second promotion of the same candidate are refused.

## candidate-store.py

Source: author's private operator repository, bin/candidate-store.py
Source-sha256: cc6e71a61b244c7bdf185cc724fa46e84a207241ee0f5e9838eb47e396dba155
State: re-authored

Deltas:
- Candidates are kept as <out>/candidates/<id>/candidate.json with {id, name, type, description, body, redacted_count, sha256}; the id derives from the content hash, and receipts are one JSON line per event in `<out>/promotion.jsonl`.
- Candidates come from `../memory-backends/recall.py` JSON output of this collection, redacted before anything is built or written.

## mem-redact

Source: author's private operator repository, bin/mem-redact
Source-sha256: d4771d5fe9c48166ba78a5f2e4dab11bd5cf076481c88bebd1953e59b8ee5612
State: re-authored

Deltas:
- Ported as `redact.py`. The six built-in classes are reused from `observe.Redactor` by path; six extra classes (jwt, slack webhook, bearer, credential URL, github_pat, mysql -p) are added, 12 in total.
- Matches become `[REDACTED]` and are counted; nothing unredacted reaches a candidate, a receipt, stdout or stderr.

## mem-observed

Source: author's private operator repository, bin/mem-observed
Source-sha256: f2c1c13e139231555d2688a62f29cf63805cd258142f3d9c0f90670e8ae5f52c
State: re-authored

Deltas:
- The frozen-snapshot half is ported as `snapshot.py`: a sqlite3 backup from a read-only connection, stored 0444 with a pinned sha256, read back through one verified read and an in-memory load. Observation production stays in `modules/memory-backends`.
- Evaluation labels each goldset question hit, miss or abstain using the curated retrieval rule (at least half of the distinct query terms, rounded up); abstain is never a hit, and too few answered questions give `insufficient`.
- Comparison is descriptive (added, removed and changed ids; evaluation deltas only for identical question ids) and ranks nothing.

## claude-mem-sync

Source: author's private operator repository, bin/claude-mem-sync
Source-sha256: 4cd975f6c84be00a463a1c7a0c3ce5cdd66efe1adec617cb68a776f6b9a10ecc
State: re-authored

Deltas:
- Ported as `sync.py` for this collection's observation store (`obs.db`), not the third-party claude-mem schema.
- Worker-stop reconciliation: the source docstring says `run --apply` stops the workers, swaps the database and restarts them. Its `cmd_run` does something else: online-backup snapshots, then an insert of missing rows into the live database with no stop; only its `agent stop/deploy/start/reset-chroma` commands stop workers, and `deploy` deletes the -wal/-shm files. The docstring is stale live drift. This port stops, starts, signals and swaps nothing.
- Not ported defects: the `ATTACH '<path>'` f-string, fixed temp paths, ssh/scp to a hard-coded remote, `deploy` deleting -wal/-shm, and `insert or ignore` hiding id conflicts. Transport between hosts is the operator's job; merge reads a local replica.
- A database, -wal or -shm file is never copied as bytes: reads use a read-only connection, copies use the backup API, and a merge is one BEGIN IMMEDIATE transaction with a journal (planned, backed_up, committed, verified), a pre-sync backup taken under the same lock, integrity checks and an explicit `recover` and `restore`.
- Full-text rows are written at the target's own new rowid; the same id with a different row is a conflict (exit 4) and nothing is written.
- A capture that hits the lock while a merge runs is dropped (the capture hook exits 0 on errors); this is a declared consequence of the single transaction.

## claude-mem-sync (operator manual)

Source: author's private operator repository, bin/README.md section claude-mem-sync
Source-sha256: 8dabe902baa17ddd8c2ffd52136d39efac599dca609e39fac8434da45f3b309f
State: re-authored

Deltas:
- The manual's description of the run path (online-backup snapshots, then an insert of missing rows, no worker stop) is the behaviour that was ported; the commands that stop or restart workers were not.

## memory-index-fix hook

Source: author's local Claude hooks, memory-index-fix.sh
Source-sha256: 5e641401fe666cc5383cbd08af54e095a02c27dd95082988f22d2ca32b12b0f2
State: re-authored

Deltas:
- The index re-write after a new note is `tools/memcheck/memcheck --fix-index --apply` run by `promote apply`, with its audit log under `--out`; the audit stays an explicit separate step.

## Not ported

- mem-comparator: it drives third-party indexers.
- mem-raw, recall-harness and mem-baseline: they make model calls or use the private goldset.
- mem-export and claude-mem-to-vault: tied to the third-party claude-mem store.
- episode-brief and graph-refresh-quiet: session tooling outside this module.
- graphify-provision: it installs software.
- chroma reset: a worker-affecting command.
- U12 model-evaluation: absent from this collection at this point.
