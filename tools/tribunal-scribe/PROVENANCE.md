# Provenance

Source: author's private operator repository, bin/tribunal-scribe
Source-sha256: ac91b9126515f02e6dc5705104a134426a6eb167e700f0da60328fb6c0bc6920
State: adapted

## Deltas

- Private tool and plan-step references were removed. The upstream line-linked SHA-256 format is retained in `CHAIN.SHA256`; append/close print heads, and `verify --expect-head <sha>` compares a caller-retained head. Private anchor storage, host rules, and automatic commits are omitted. No files outside the requested workspace are written by the tool.
- Plain `verify` checks local consistency only; a consistent rewrite or rollback can pass without a separately retained expected head. A matching supplied head anchors the record to that caller's value, not to an independently authenticated identity or trusted time. There is no claim of authorship or completeness beyond that retained state.
- Existing absent/incomplete-record validation and supported pristine-open/closed-without-turns records are preserved. Chain-less populated records fail rather than being accepted as legacy. Verification also checks chain hashes, turn order/count, checksum history, seat copies and marker-head syntax, alongside turn hashes and fenced payload bytes (allowing one added trailing newline). Mutation commands retain non-scribe commit and lens checks and refuse a broken/missing chain on an existing record. Workspace paths are resolved before git pathspec checks.
- Tests adapted from the author's private operator repository, bin/tests/test-tribunal-scribe.sh (sha256 10800ac4d26d2de3494472d4adb3509fa1f14ac2970644a83b0eba72c33e2086) as tests/run.sh and tests/test_chain.py. Private host/anchor/automatic-commit cases are excluded; public regressions cover altered/removed/reordered chains, retained-head mismatch, consistent forgery/rollback, moved records, and no automatic commit. All test records and test commits use temporary repositories.
- Requires git and python3 (stdlib only); the tribunal workspace must be inside a git repository.
