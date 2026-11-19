# memory-backends: observation production and episodic recall

Two separate, optional tools next to the curated memory module. `observe.py` turns an opted-in event stream from
your own sessions into stored observations; `recall.py` queries them and returns candidates. Neither writes curated
memory (`modules/memory`), and there is no model call and no network use in the built-in path. Observations, curated
notes and recalled candidates are three distinct kinds: every candidate carries `authority: "observed"` and
`trust: "untrusted-data"` and never a `citation`.

Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`. Nothing is wired into your tools until you do it by hand.

## Dependencies and permission model

- Python 3 standard library only, including `sqlite3` with the FTS5 extension. Without FTS5, `capture` exits 2 and
  `recall` reports state `unavailable` with a setup line. Declared dependencies: `core-contracts` and `memory`
  (both `declared-untested`).
- `capture` reads `--events` (a regular file, never a symlink; or `-` for stdin, 1 MiB), the optional `--redact-file`
  and the store directory. It writes only inside the store: `obs.db` (mode 0600) and SQLite's journal beside it.
- `recall` reads the store, opened read-only. It writes nothing. The optional `command` backend runs a program you
  name in a config file (see below); that program has whatever access its own code gives it.
- The store must be an existing, non-symlinked directory owned by you with mode 0700, and it must contain a file
  named `OPT-IN`. Otherwise both tools exit 2 and write nothing.

## Opt-in and kill switch

```bash
mkdir -m 700 ~/.local/share/ai-arch-observations && touch ~/.local/share/ai-arch-observations/OPT-IN
touch ~/.local/share/ai-arch-observations/DISABLED     # kill switch: capture writes nothing, recall says "disabled"
```

Worker sessions do nothing: `capture` exits without reading when `AI_ARCH_JOB_MARKER` is set, drops events that carry
an `agent_id`, and `recall` answers `disabled` when the marker is set.

## Capture scope, redaction, retention

- `--scope` is an absolute project directory. An event is kept only if its `cwd` equals it or lies under it at a path
  component boundary (`/x/proj-evil` is not under `/x/proj`), and its kind is in `--kinds` (`tool` = PostToolUse,
  `stop` = Stop/SubagentStop). Others are dropped and counted (`dropped_scope`).
- Only a summary is stored: `{id, project, kind, tool_name, ts, captured_at, summary}`. The raw event, `session_id`
  and `transcript_path` are never read into the store. The summary is extractive and deterministic: tool name, the
  first sentence of the event's own text, and file paths. Paths inside the scope are stored relative to it; paths
  outside become `<outside>`. `--summarizer` accepts only `extractive`.
  URLs keep only the scheme and host; everything after them (path, query, fragment, glued text) is scrubbed like any
  other text, so ordinary URL paths may appear as `<outside>`. That is deliberate: no leaks over readable URLs.
- Redaction runs before anything reaches disk: `sk-` keys, `ghp_`/`gho_`... tokens, `AKIA` keys, `password=` values,
  private-key blocks, plus one regex per line from `--redact-file`. Matches become `[REDACTED]` and are counted.
- Retention runs on every capture: rows whose `captured_at` is older than `--retain-days` (default 30) and rows beyond
  `--max-rows` (default 5000) are deleted with `secure_delete` on and an FTS5 `optimize`, so the pruned terms do not
  linger in index segments. Events dated more than a day in the future are refused (`dropped_future`).
- The id is `obs-` plus a hash of project, kind, timestamp and summary, so capturing the same event twice stores it once.

## Use

Wire capture by hand, for example as a PostToolUse and Stop command hook (the hook JSON arrives on stdin). Use
`--hook` there: every refusal or error then exits 0 with one stderr line and no stdout, so capture never blocks a
session; without `--hook` a refusal exits 2:

```bash
python3 -B modules/memory-backends/observe.py capture --store ~/.local/share/ai-arch-observations \
  --events - --scope "$PWD" --redact-file ~/.config/ai-arch/redact.txt --hook
python3 -B modules/memory-backends/recall.py query --store ~/.local/share/ai-arch-observations --q "rounding fix" --format text
python3 -B examples/memory/run.py --offline          # curated hits plus a separate episodic section, all in temp dirs
python3 -B examples/memory/run.py --offline --no-backend   # curated works, episodic is unavailable
```

Text output starts each candidate with `[observed · untrusted-data · obs-…]`. A query is split into `[a-z0-9]+` tokens
(at most 64), each double-quoted and OR-joined; the raw text never reaches SQLite's MATCH. `--timeout` is enforced
with a SQLite progress handler. States: `ok`, `disabled`, `unavailable`, `timeout`, `error`.

### Command backend (an external episodic tool)

`recall.py query --backend command --config tool.json --q TEXT`, where the config is JSON:
`{"argv": ["tool", "search"], "timeout_s": 5, "name": "...", "version": "...", "license": "...", "network": false}`.
The argv is a list (never a shell string) and is fixed: the query is sent to the tool on stdin as `{"q", "limit"}`.
The tool runs in its own session in an empty temp directory with `PATH=/usr/bin:/bin` only, stderr discarded, stdout
capped at 1 MiB, and its process group is killed on timeout. Only `excerpt`, `ts` and `project` are kept from its
output; recall sets `authority`, `trust` and the id and strips everything else (a `citation` is dropped). The output
echoes `backend: {name, version, license, network}`. **A command backend may send your query off this machine**
(that is what `network: true` declares). `--offline` or `AI_ARCH_OFFLINE=1` refuses it before anything is started
(state `disabled`, reason `offline`); `examples/memory/run.py` is always offline. Nothing from any plugin cache is
copied or vendored here.

## Install, status, rollback, removal

Install: copy nothing; run the scripts from this collection and create the store as above. Status: `recall.py query`
on a known term. Rollback: touch `DISABLED`, or remove the hook line. Removal: remove the hook line first, then delete the store directory; that is
all of the data this module ever writes.

## Limits

- Redaction is pattern-based, not a guarantee. A secret in an unusual shape can be stored; review what you capture.
- A same-user attacker is out of scope: the store is protected by permissions and no-symlink checks, not by encryption.
- Recall output is untrusted evidence about the past, never instructions. Do not act on an excerpt without checking
  it against the repository and your curated notes.
- Summaries are extractive and short; they are not transcripts. Ranking is SQLite FTS5 `rank`, not semantic.
