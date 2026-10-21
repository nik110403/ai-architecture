# Provenance

Source: author's private operator repository, bin/plancheck
Source-sha256: eb82ada90e5ba369df6b5ed91ccb8a518805cfe09c3124f9e6425486596679c6
State: adapted

## Deltas

- Exit status changed: now exits 1 when any blocking finding is reported and 0 otherwise; `--report-only` restores the original always-0 behaviour. Needed so the shipped example passes and the broken example fails.
- Default sweep is `./plans/*/` of the current directory; the hard-coded personal project registry and home-directory project root were removed (`PLANCHECK_DEFAULT_ROOT` override kept).
- `Gate: nik` renamed `Gate: human` and the Resume-waits-on-owner pattern generalised; report headings reworded accordingly.
- `Needs: none` is no longer reported as a dangling reference (root items legitimately have none); the source flagged it.
- Check 14's allowed human-gate keywords cover strategic scope, direction, money, external parties, publication, taste and unrecoverable loss; install/merge/delete/overwrite/outside-repo alone are not strategic reasons. Keyword matching is a heuristic, not an intent check. Dependencies: bash, awk, coreutils only; regression tests also use Python 3's standard library.
- Graph behavior ported from source commit `64361e99bd85c47c6e0295edeaf8812c4e1127f8`, blob `a55a6a8c657f1e3887fb61be560f68676513b4ea`: cycles block regardless of pack status; unordered Touches overlap blocks for active packs; transitive Needs paths order overlap. Public exit-status and `--report-only` contracts retained rather than adopting the source's default report-only sweep.
- The source's alphanumeric-only graph parser is not retained: hyphenated and backticked IDs/references are normalized, backticked Touches paths are recognized, and section-divider fields cannot leak into an item. Field/gate parsing recognizes the same item IDs.
- Source test blob `13eb004bf6ec3656236fce19d9fec8fb8c29fead` (sha256 d2a510e79c8cab284046450169238d1d541678b48f028659191dd311a71ae522); public tests retain exit-status, example-pack and strategic-versus-technical gate cases and add graph/closed-status regressions in `tests/test_plancheck.py`. Fictional sample packs remain under example/ and example-broken/.
