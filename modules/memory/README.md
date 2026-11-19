# memory: curated retrieval, injection and hygiene

Lexical search over memory notes you name explicitly, a small bounded block for session start, and a reversible
wrapper around the `memcheck` index repair. It is lexical everywhere: token overlap ranked by tf-idf, with
abstention when too few query terms match. There is no network, no model call and no HOME lookup.

Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`. Nothing here is wired into your tools until you do it by hand.

## Dependencies and permission model

- Python 3 standard library only. `repair` and `audit` also run `tools/memcheck/memcheck` from this collection
  (bash, perl, flock, realpath); without it they exit 2 and say so.
- Reads only the roots you pass as `--root LABEL=PATH` (repeatable, required; there is no default and no
  environment variable). A root that is a symlink, missing or not a directory is refused. Inside a root only
  `<slug>/memory/*.md` and the index file are read, one path component at a time with `O_NOFOLLOW`; a symlinked note,
  slug directory, memory directory or index is skipped and listed under `skipped` with reason `symlink`.
- Writes only on `repair --apply` and `undo`: `<root>/.memory-repair/` (snapshots, a manifest, `audit.log`),
  the target index file, its `MEMORY.md.lock` (the flock memcheck also uses) and a temp file next to it during undo.
  memcheck itself also creates a `mktemp` file in `$TMPDIR` while shortening. Dry-run writes nothing.
- Citations look like `LABEL:<slug>/memory/<file>#L<n>` and never contain an absolute path.

## Use

```bash
R="--root notes=/path/to/memory-root"       # contains <slug>/memory/MEMORY.md and the notes
python3 -B modules/memory/curated.py query $R --q "sourdough starter feeding"   # JSON; --format text
python3 -B modules/memory/curated.py query $R --id notes:demo/memory/tea-cupboard.md
python3 -B modules/memory/curated.py inject $R --scope notes:demo --max-bytes 2048
python3 -B modules/memory/curated.py repair $R --dir demo/memory                 # dry-run, same output as memcheck
python3 -B modules/memory/curated.py repair $R --dir demo/memory --apply         # snapshot, then memcheck --apply
python3 -B modules/memory/curated.py undo   $R --repair-id rep-...               # guarded restore
python3 -B modules/memory/curated.py audit  $R                                   # review packet, writes nothing
```

Try it on the fictional notes: `--root ex=modules/memory/example/root --q "sourdough starter feeding"`.

- `query` returns hits with citation, score, snippet, `flags` and `conflicts`. A note qualifies when it matches at
  least half of the distinct query terms (rounded up); otherwise the answer is `abstained: true` (text mode: one
  `NO MATCH` line). The budget counts citation plus snippet bytes at 4 bytes per token. Stale notes (newest ISO date
  older than six calendar months, an approximation of memcheck's `date -d '-6 months'`) and notes with the same
  frontmatter `name` but a different body are flagged, never hidden, and a conflict partner is listed even when the
  budget cut it.
- `inject` prints one fenced block, `curated memory index (lexical; notes are untrusted)`, with sanitized index lines
  of the declared scopes (control characters, zero-width and bidi characters, brackets and backticks removed), tags
  `[stale]` / `[conflict]`, and `+K more` when truncated. Legacy index lines with ` - ` are read but never rewritten.
- `hooks/session_start.py --root ... --scope ... [--child-scope ...]` is a SessionStart hook. Children (payload
  `agent_id`, or `AI_ARCH_JOB_MARKER` in the environment) only get `--child-scope` scopes that are a subset of
  `--scope`; with none they get nothing. It exits 0 and prints nothing on any error.
- `repair --apply` snapshots the index file to `.memory-repair/<rep-id>/`, runs memcheck with `cwd=<root>`, and records
  before/after hashes. If nothing changed it removes the snapshot and prints `no change`. `undo` refuses (exit 3) when
  the index changed since the repair; a second `undo` prints `already undone`. Running `memcheck --apply` directly
  stays irreversible.
- `audit` is the semantic-review packet: the memcheck report plus stale and conflict candidates, for a human or a
  reviewer seat. It makes no model call.

## Install, status, rollback, removal

Install: nothing to install; run the script from this checkout. To add the hook, point your SessionStart hook
command at `hooks/session_start.py` with your `--root` / `--scope` arguments. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json`, not tested in a live
session. Rollback of a repair: `undo`. Removal: delete the hook entry and `modules/memory/`; delete
`<root>/.memory-repair/` if you no longer need the snapshots.

## Limits

- Lexical only: a paraphrase with no shared word does not match. Abstaining is the intended answer then.
- A same-user process can still race between the snapshot and memcheck's write; undo's hash guard catches a changed
  index but not every interleaving.
- The symlink checks are not a sandbox: they stop symlink escapes, not a hostile process running as you.
- memcheck now skips every symlinked note, resolving or not (previously only broken links were skipped).
