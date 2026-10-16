# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` = sha256 of the source file
as read when it was ported). Sources named `~/.claude/...` or `claude_improvement/bin/...` are an author's installed hook
and operator-repository test files; they are not shipped. `scripts/check_collection.py` validates this file.

## model-rotate

Kit file: hooks/rotation/model-rotate.py
Source: ~/.claude/hooks/model-rotate.sh
Source-sha256: ec7bcdadee99f92c3af1b2453b5a87149c03e0869bc1d663ec8b4c0cf40649d8
State: adapted

Deltas:
- the embedded `python3 -c` program is a standalone file; fail-open on every parse, pool, mode, role and eligibility problem is unchanged and now also covers a symlinked or oversized pool file, an unknown pool `version`, and any malformed entry in a role's list (the whole role is then not rotated instead of skipping the entry)
- RNG seam added: `ROTATION_SEED` selects `random.Random("rotation:" + seed)` instead of `secrets.randbelow` (default unchanged: cryptographic)
- model names and weights are validated strictly (name regex, integer weight 0..1000000, boolean `enabled`); nothing from the payload or pool reaches a shell
- pool schema reduced to placeholder models and weights: `{model, weight, enabled}` entries per role, an optional `independent_roles` list; no `harness:model` arms, harness allowlists, quotas, accounts or usage data. The rotated-role set comes from the pool instead of a built-in list
- removed: external-worker arms and the dispatch-to-job start, the effort-trial variant swap, the sandbox path narrowing, charge-id extraction and the dispatch-ledger fields, the personal default pool and log paths (the log is now opt-in via `ROTATION_LOG`)
- kept: `pin_arm` (header-only, 15-line cap, CRLF-normalised), `builder_arm` exclusion anywhere in the brief for independent roles, pin equal to the builder is refused, an unusable pin falls back to the weighted draw
- the single O_APPEND, O_NOFOLLOW log write is kept

## test-model-rotate

Kit file: hooks/rotation/tests/test-model-rotate.sh
Source: claude_improvement/bin/tests/test-model-rotate.sh
Source-sha256: 21b4cb6a260449bed12a55628c736119115ea0cb1de60d2c049e13bb8d59c425
State: re-authored

Deltas:
- cases kept in spirit: rewrite and log row, off switch, non-rotated role, malformed stdin, mode off, missing pool, pin handling (header, late, 15-line cap, unknown, disabled, CRLF), builder_arm exclusion (anywhere, CRLF, pin equal to builder)
- cases dropped with the removed features: external-worker arms, effort trial, harness allowlists, sandbox narrowing, personal pool equivalence
- new cases: fail-safe table for invalid pools (symlink, oversized, wrong version, bad names, bad weights), seeded-draw determinism checked against the documented algorithm and the 3:1 weights, crypto draw reaches only listed models
- the source's failure-isolation fix (one failed case no longer aborts later ones) is kept: every case runs and the script reports a count
