# memory-operations: promotion, snapshots with evaluation, database-aware sync

Three small tools on top of the observation stores (`../memory-backends`) and curated memory (`../memory`), one CLI:
`memops.py`. Stdlib only. No network, no model call, no process start or stop. Every command needs `--out DIR`, a 0700
receipt directory, and prints one JSON object.

Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`. Nothing is wired into your tools until you do it by hand.

## Dependencies and permission model

- Python 3 with `sqlite3` and FTS5. `promote apply` also runs `../../tools/memcheck/memcheck` (bash, perl, flock, realpath).
- Declared dependencies: core-contracts, memory and memory-backends (all `declared-untested`).
- Receipts live only under `--out` (created 0700; a symlink or a foreign owner is refused), never inside a store or a memory
  root, so a restore or rollback cannot erase them. One append-only file per lane: promotion.jsonl, snapshots.jsonl,
  sync.jsonl. The knowledge adapters write their own knowledge.jsonl.
- Writes outside `--out`: only `promote apply` / `rollback` (one note and MEMORY.md in the root you name) and `sync merge
  --apply` / `recover` / `restore` (the target store you name). Everything else only reads.

## Promotion (reviewed, reversible)

```bash
python3 -B modules/memory-operations/memops.py promote stage   --out R --from-recall recall.json --candidate-id obs-... \
    --name tea-notes --type reference --description "Tea facts."      # redact first, then write the candidate
python3 -B modules/memory-operations/memops.py promote approve --out R --id cand-... --sha <sha256> --approver reviewer-1
python3 -B modules/memory-operations/memops.py promote apply   --out R --id cand-... --root notes=/path/root --dir demo/memory
python3 -B modules/memory-operations/memops.py promote rollback --out R --receipt rcpt-... --root notes=/path/root
```

- `stage` redacts with the 12 classes in `redact.py` (the six of `observe.Redactor` plus jwt, slack webhook, bearer,
  credential URL, github_pat, mysql -p) before any byte is built or written.
- `approve` records `{id, sha256, approver}`. It is refused when AI_ARCH_JOB_MARKER, CLAUDECODE, CLAUDE_CODE_* or OPENCODE* is
  set. That is not authentication: anyone who can run the CLI outside an agent session can approve. Types `user` and `feedback`
  also need `--type-confirm` naming the type.
- `apply` recomputes the hash from the candidate (a stored sha256 field or an `approved` flag is never trusted) and refuses with
  no approval, an approval for different content, a name collision (existing file, symlink, symlinked memory directory) or an
  already-promoted candidate. Order: snapshot MEMORY.md, create the note O_EXCL|O_NOFOLLOW, memcheck fix-index (audit log under
  `--out`), receipt. If the receipt cannot be written the note and the index are put back and the command exits non-zero.
- `rollback` removes the note and restores the index bytes, only if both still match the receipt.

## Snapshots and evaluation

```bash
python3 -B modules/memory-operations/memops.py snapshot freeze  --out R --store S
python3 -B modules/memory-operations/memops.py snapshot query   --out R --snapshot P --pin P.pin.json --q "alpha beta"
python3 -B modules/memory-operations/memops.py snapshot eval    --out R --snapshot P --pin PIN --goldset gold.json
python3 -B modules/memory-operations/memops.py snapshot compare --out R --a A --a-pin PA --b B --b-pin PB [--eval-a RA --eval-b RB]
```

- `freeze` copies obs.db with the sqlite3 backup API from a read-only connection to `<out>/snapshots/obs-<sha12>.db` (0444) and
  a pin `{sha256, rows, source_store_id, created_at}`.
- `query`, `eval` and `compare` verify the whole file against the pin's full sha256 (a mismatch exits 4 with `sha-mismatch`),
  then load the bytes into memory. They never open the live store.
- `eval` takes a goldset `{questions:[{id, q, expect:[ids]}]}` and labels each question `hit`, `miss` or `abstain`. A question
  abstains when it has no searchable term or when no observation matches at least half of its distinct terms (rounded up), the
  curated retrieval rule. Abstain is never a hit. With fewer than `--min-questions` (default 5) answered questions the status is
  `insufficient`. `compare` reports added, removed and changed ids and eval deltas only when both runs used the same question
  ids; it is descriptive and ranks nothing.

## Sync (never a byte copy of a live database)

```bash
python3 -B modules/memory-operations/memops.py sync status  --out R --store S
python3 -B modules/memory-operations/memops.py sync backup  --out R --store S
python3 -B modules/memory-operations/memops.py sync merge   --out R --target T --replica REPLICA          # dry-run plan
python3 -B modules/memory-operations/memops.py sync merge   --out R --target T --replica REPLICA --apply
python3 -B modules/memory-operations/memops.py sync recover --out R --id sync-... --target T [--restore --accept-loss]
python3 -B modules/memory-operations/memops.py sync restore --out R --target T --backup bk-... --expect-sha SHA
```

- A database, its -wal or its -shm is never copied as bytes; the tool never opens a database in a mode that skips locking.
  Reads use a read-only connection (the main file and the -wal stay byte- and mtime-identical; SQLite itself may touch the -shm),
  copies use the backup API, so a row that only exists in the -wal is included.
- `merge` is a dry-run by default and writes only a plan under `--out`. `--apply` takes `BEGIN IMMEDIATE`, takes the pre-sync
  backup under that same lock, unions rows by id, writes each full-text row at the target's new rowid, commits, runs
  `integrity_check` and the full-text integrity check, and keeps a journal `<out>/sync/<id>.json`
  (planned, backed_up, committed, verified).
- Conflict: the same id with any difference in project, kind, tool_name, ts, captured_at or summary exits 4 and writes nothing.
  captured_at is compared strictly; resolve such a pair by hand.
- `status` reports `interrupted` while a journal for the store is not verified. `recover` opens the store read-write (SQLite
  rolls a hot journal back), checks integrity, and reports rows that are in neither the backup nor the replica (captures
  that landed meanwhile); they are preserved. It never restores on its own: `--restore` is explicit and needs `--accept-loss` when
  it would drop such rows; the receipt and output then report `dropped_extra_rows` and `dropped_ids` (ids only) instead of `preserved_extra_rows`.
- Pairwise reconciliation: merge A from B, then B from A, and the id sets are equal.

## Exit codes

0 ok, 1 failed verification or I/O error, 2 usage or refusal, 3 a guarded write was refused, 4 conflict or sha mismatch.

## Limits

- Approval is a record, not authentication. A hostile process running as you can approve, apply and edit receipts.
- A capture that hits the lock during `merge --apply` is dropped (the capture hook exits 0 on errors).
- rollback and apply do not hold a lock against a concurrent editor of MEMORY.md; rollback's hash guard refuses a changed file.
- Sync works on this collection's observation store. It does not touch the third-party claude-mem database.
- Transport of a replica between hosts is yours to do; this tool reads a local replica file or store.
- `mem-comparator`, the model-calling evaluators and the installers are not ported (see PROVENANCE.md).
