# research-integrity

A stdlib module that runs a research project through `init -> query -> check -> finalize -> release` and
writes a hash-chained receipt for every step under `<project>/.research/receipts/`. It makes no model call,
opens no socket, copies no companion code and needs Python 3.9 or newer.

## What it delegates and what it owns

Quote, claim and scaffold work belongs to the public companion provenote (`plugins/provenote`, tested
`0.2.x`). The module runs `<companion>/bin/verify-quotes --strict <note>` once per note,
`<companion>/bin/check-claims --strict-quotes --root <project> <draft>` once, `<companion>/bin/new-project <dir>` and
`<companion>/bin/new-pipeline <dir>/03_code --lang python|r` as subprocesses (its own interpreter, own process
group, a timeout, and an environment reduced to `PATH`, `LC_ALL`, `TZ` and two Python flags). Companion identity
and version come from `modules/research-adapter` (`handshake`, loaded by path, read-only). Without a
companion, with an unsupported version, or with a bin that is missing, a symlink or not a regular file, the
affected checks are `UNAVAILABLE: <exact requirement>` and no bin runs. A bin that exits 2, crashes, is killed
by a signal or times out is a FAIL (`usage`, `crash`, `timeout`), never UNAVAILABLE or PASS.

The module itself owns source-hash binding, exhibit-to-code lineage, the statistical-reporting check, the
analysis runner, the audit/referee/replication contracts and release gating.

## Commands

```bash
python3 integrity.py init --project P [--companion DIR] [--pipeline python|r]
python3 integrity.py query --project P --index OUT/notes/index.json
python3 integrity.py check --project P [--companion DIR] [--bin-timeout SECONDS]
python3 integrity.py finalize --project P
python3 integrity.py release --project P --out OUT
python3 integrity.py verify-receipt --project P
python3 integrity.py replicate --project P
```

Exit codes: 0 PASS, 1 FAIL / stale / broken receipt chain, 2 usage or refused input (symlinks, `..` or
absolute paths, duplicate JSON keys), 3 INCOMPLETE. Precedence: FAIL > INCOMPLETE > PASS. Release refuses
anything but PASS.

- `init` adopts a populated directory, or runs the companion's `new-project` into an absent or empty one
  (companion required). It mints a random `project_id` that every later receipt carries.
- `query` binds the sha256 of the research-adapter's `<OUT>/notes/index.json` and of its companion receipt. A status other
  than `ok` makes `query` UNAVAILABLE. The module runs no discovery of its own.
- `check` runs the fixed required set: `scaffold, query, sources, quotes, claims, stats, exhibits, analysis,
  audit, referee, replication`, and binds the sha256 of every project file outside `.research/`. A missing
  contract file is FAIL `missing: <path>`; zero notes or zero draft citations is FAIL `empty: <what>`.
- `finalize` re-hashes every bound file and exits 1 `stale: <path>` on any difference. It never accepts
  INCOMPLETE or FAIL.
- `release` re-verifies the chain and the bound files, then writes `<OUT>/release-manifest.json` (atomically) to
  `--out`, which must be absent or empty, not a symlink, and outside the project. The manifest links each source
  sha256 to its note quotes and draft paragraphs, and each exhibit to its file sha256, producing script and
  analysis-run output. No tag, push, deposit or publication happens.
- `verify-receipt` checks the chain and reports `stale: <path>` for files changed since the latest bound receipt.
- `replicate` reruns the analysis entry from `<project>/analysis.json` (same argv and `cwd`, `runs` times) in a fresh temp
  copy and compares every analysis output to the committed file. It writes nothing in the project.

## Project contracts (JSON, project relative, duplicate keys refused)

`<project>/analysis.json`: `{"entry": ["python3"|"Rscript", "<script>", ...], "cwd": ".", "timeout_s": N,
"outputs": [...]}`. `python3` runs as the current interpreter; an `Rscript` that is not on `PATH` is
`UNAVAILABLE: Rscript not found on PATH` (for an R pipeline use `new-pipeline --lang r` and
`["Rscript", "run_all.R"]` with `"cwd": "03_code"`). The run happens in a temp copy; the timeout kills the child
and its process group.

Lineage: before every run the temp copy has each declared output and each exhibit file removed. After the run, a
declared output the script did not write is FAIL `missing output <path>`, and one whose sha256 differs from the
committed file is FAIL `output differs <path>`. So "exhibit X was produced by the entry" means the entry, run on the
project's inputs and code, rewrote exactly the committed file; a no-op script or a changed input fails. The
`analysis_run.output_sha256` in the release manifest is the hash the run produced.

`<project>/replication.json` adds only `{"runs": N (1-10, default 1), "env": {"PYTHONHASHSEED"|"SEED"|"RANDOM_SEED"|
"SOURCE_DATE_EPOCH": "<value>"}}`. It may repeat `entry`, `outputs`, `cwd` or `timeout_s`, but an `entry`, `outputs` or
`cwd` that differs from `<project>/analysis.json` is a contract FAIL naming the key. Replication therefore proves the same
entry reproduces every committed output (within the seed variables given); it does not prove the analysis is
correct, and a run that depends on state outside the project copy is not covered.

`<project>/exhibits.json`: `{"exhibits": [{"id", "file", "sha256", "produced_by"}]}`. A draft `{{exhibit:id}}` must exist,
the file hash must match, `produced_by` must be a script under `03_code/` and the file must be an output of the
recorded analysis run.

`<project>/00_admin/audit.json`: `{"status": "RUN"|"NOT RUN", "reviewed": {path: sha256}, "findings": [{"id",
"disposition": "fixed|refuted|accepted-risk", "evidence": path, "evidence_sha256"}]}`. `00_admin/referee.json`:
`{"status", "reviewed", "summary"}`. `reviewed` must cover `<project>/10_paper/draft.md` and `<project>/exhibits.json`; a mismatch is
`stale-review: <path>`. `NOT RUN` is UNAVAILABLE. Referee and audit results are recorded attestations that are
validated, not reviews this module performed.

Source files live at `02_sources/md/<citekey>.md` with a front-matter `md_sha256`; `<project>/02_sources/manifest.csv` has
columns `stem,md_sha256`; notes sit at `01_notes/lit/<citekey>.md` and the draft cites with `[@citekey]`.

## Statistical reporting

Grammar: `t(df) = x, p = y`, `F(d1, d2) = x, p = y`, `z = x, p = y`, `chi2(df) = x, p = y` (also the chi letter).
`p` may use `<`, `>`, `<=`, `>=`. The p value is recomputed with stdlib numerics (two-sided for t and z, upper
tail for F and chi2). A reported `p = v` is consistent when some statistic inside the rounding interval of x
gives a p that rounds to v at v's decimals; `p < c` passes when the smallest p over that interval is below c.
Any `p` comparison outside the grammar is `unsupported-grammar`: the stats check is UNAVAILABLE and release is
refused.

## Limits

The receipt chain is unkeyed: it is tamper-evident only against partial edits, not against a fully recomputed
forgery, and the most recent receipt can be deleted without trace. The checks bind and compare hashes of files;
they do not judge whether a quote means what a paper claims. See `INTEGRATION.md` for status and removal.
