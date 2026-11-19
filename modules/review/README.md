# review

Artifact-bound review receipts. Stdlib only; no network, no model. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json` (no live evidence yet).

```
python3 -B modules/review/receipt.py issue --artifact F [--artifact G ...] --reviewer ROLE --verdict PASS|FAIL --out R
python3 -B modules/review/receipt.py check --receipt R [--root D] [--format text|json]
```

A receipt is a JSON file `{"schema_version":1,"reviewer","verdict","artifacts":[{"path","sha256"}],"issued"}`
naming each reviewed file by absolute realpath and sha256. It is written atomically.

- `issue` refuses a symlinked artifact, a duplicate path (after realpath), a missing or non-regular file and a bad
  verdict or role (exit 2); nothing is written.
- `check` exits 0 `fresh` only if every artifact exists, is not a symlink and still has the recorded sha256. It exits 1
  `stale` naming each changed, missing or replaced file, and 2 on an unknown `schema_version`, duplicate JSON keys, a
  malformed receipt or one over 1 MiB. Content hashes are compared, not mtimes.

A receipt binds content, not git history, and it is evidence for a gate rather than authority: anyone can run `issue`.
The gates plugin therefore treats receipts as an extra requirement on top of proof that the reviewer ran (see
`plugins/gates/README.md`, "Review receipts"). Tribunal accounting stays staged until it has been exercised.
