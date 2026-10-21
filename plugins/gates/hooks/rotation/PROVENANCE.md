# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` = sha256 of the source file
as read when it was ported). Sources named `~/.claude/...` or `private operator repository, bin/...` are an author's installed hook
and operator-repository test files; they are not shipped. `scripts/check_collection.py` validates this file.

## model-rotate

Kit file: hooks/rotation/model-rotate.py
Source: ~/.claude/hooks/model-rotate.sh
Source-sha256: 0dbdaa3acb51b378b4005d6b5ab5f88fd15a4b73cfe0a79b7a3113aaf4628d7d
State: adapted

Deltas:
- permission-neutral output ported from the pinned staged hook: `updatedInput` without `permissionDecision`; no external startup machinery is included, so the source's external-start gate is unnecessary here
- the embedded `python3 -c` program is a standalone file; fail-open on every parse, pool, mode, role and eligibility problem is unchanged and now also covers a symlinked or oversized pool file, an unknown pool `version`, and any malformed entry in a role's list (the whole role is then not rotated instead of skipping the entry)
- RNG seam added: `ROTATION_SEED` selects `random.Random("rotation:" + seed)` instead of `secrets.randbelow` (default unchanged: cryptographic)
- model names and weights are validated strictly (name regex, integer weight 0..1000000, boolean `enabled`); nothing from the payload or pool reaches a shell
- pool schema reduced to placeholder models and weights: `{model, weight, enabled}` entries per role, an optional `independent_roles` list; no `harness:model` arms, harness allowlists, quotas, accounts or usage data. The rotated-role set comes from the pool instead of a built-in list
- removed: external-worker arms and the dispatch-to-job start, the effort-trial variant swap, the sandbox path narrowing, charge-id extraction and the dispatch-ledger fields, the personal default pool and log paths (the log is now opt-in via `ROTATION_LOG`)
- `pin_arm` and `builder_arm` are header-only (before the first blank line, at most 15 lines, CRLF-normalised). Independent roles exclude the builder; duplicate `builder_arm:` lines anywhere skip rotation. A pin equal to the builder, or an independent-role pin without a builder header, is ignored; unusable pins fall back to the weighted draw.
- the single O_APPEND, O_NOFOLLOW log write is kept

## test-model-rotate

Kit file: hooks/rotation/tests/test-model-rotate.sh
Source: private operator repository, bin/tests/test-model-rotate.sh
Source-sha256: f1e2217d128645a221b7a4596cf057bc8aa6cc5c09780b16eb86eb32fa28dd40
State: re-authored

Deltas:
- cases kept in spirit: rewrite and log row, off switch, non-rotated role, malformed stdin, mode off, missing pool, pin handling (header, late, 15-line cap, unknown, disabled, CRLF), builder_arm exclusion (header-only, late ignored, CRLF, duplicate lines anywhere skip rotation, pin equal to builder, independent-role pin without builder ignored)
- cases dropped with the removed features: external-worker arms, effort trial, harness allowlists, sandbox narrowing, personal pool equivalence
- new cases: fail-safe table for invalid pools (symlink, oversized, wrong version, bad names, bad weights), seeded-draw determinism checked against the documented algorithm and the 3:1 weights, crypto draw reaches only listed models
- the source's failure-isolation fix (one failed case no longer aborts later ones) is kept: every case runs and the script reports a count
- explicit decision-free output checks for both Task and Agent, including ask/deny and forged identity metadata; the source's external-start fixtures are not ported because no worker path exists
