# research-companion example

Offline demo of `modules/research-adapter` against a provenote companion checkout (version 0.2.x).

```bash
python3 run.py --offline                                   # no companion: reports unavailable, exits 0
python3 run.py --offline --companion <companion> --out <out>
```

`<companion>` is the `plugins/provenote` directory of a provenote checkout. `brief.json` is the typed
brief, `fixture.json` holds synthetic responses for the three hosts the companion would call (OpenAlex,
Semantic Scholar, doi.org), so no socket is opened. Output: `<out>/notes/index.json` and notes, then
`<out>/citations.json` for the first two DOIs found. Fixture DOIs use the `10.5555/` test prefix; nothing
here is a real paper. `--brief` and `--fixture` replace the shipped files. The demo reads no environment
variable; pass `--companion` explicitly.

## Provenance and release

`mini_study/` is a synthetic project (no real papers, quotes or data) for `modules/research-integrity`. `verify_receipt.py`
copies it to a temp directory, runs `init -> query -> check -> finalize -> release` and asserts the integrity guarantees:

```bash
python3 verify_receipt.py                      # bare: prints "companion: NOT RUN (...)"; asserts INCOMPLETE, stale source, bad statistics, unknown exhibit, receipt chain
python3 verify_receipt.py --companion <companion>   # also reaches release PASS and shows a planted fake quote refused
python3 verify_receipt.py --receipt <project>/.research/receipts/<file>.json   # exit 1 if the chain around that receipt was edited
```

`Lineage and replication: `check` removes the declared outputs and exhibit files from a temp copy, reruns `03_code/run_all.py`
and requires every output to be rewritten byte-identical (`missing output` / `output differs` otherwise). `mini_study/replication.json`
only sets `runs`; the entry and outputs always come from `<project>/analysis.json`.

`AIARCH_REQUIRE_COMPANION=1` makes a missing `--companion` an error. Nothing here opens a socket or calls a model; the receipts and the
release manifest hold relative paths only.
