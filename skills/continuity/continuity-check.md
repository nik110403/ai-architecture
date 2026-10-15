# Continuity snapshot checker

`scripts/continuity_check.py` is an offline, read-only validator for an explicit tracker
and caller-produced receipt. It reports only `CONSISTENT` or `UNREADY`; neither result is
native harness readiness or semantic completion.

```text
python3 scripts/continuity_check.py --tracker TRACKER.md --receipt receipt.json --format json
```

The receipt must contain exactly schema version 1 plus a lowercase full-byte tracker SHA-256,
`mtime_ns`, observed-edit and known-usage booleans, and unique job/arrival ID arrays. A
consistent snapshot has a nonempty `## Resume here` section with a nonempty `- **Next:**`,
matching hash and mtime, an observed edit, known usage, no active jobs, and every arrival
banked. Invalid input exits 2; a valid unready snapshot exits 1; a consistent snapshot exits 0.

Section parsing ignores backtick and tilde fenced code, including unclosed fences. Fences
allow up to three leading spaces; closing fences must use the same character, be at least
as long as the opener, and have only trailing whitespace. ATX headings allow up to three
leading spaces, tabs or spaces after the marker, and optional whitespace-separated closing
hashes. The Resume section ends at the next unfenced level-1 or level-2 heading; deeper
subsections remain inside it. Fenced examples cannot supply a Resume heading or Next action.
Line endings are normalized only for parsing; SHA-256 always covers the original bytes.

Caller-supplied observations are not authenticated facts. The checker provides no native
readiness certification, locking, monotonic transaction, freshness, race-proof path handling,
or semantic-truth guarantee.
