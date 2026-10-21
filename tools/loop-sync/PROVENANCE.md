# Provenance: loop-sync

Source: author's private operator repository, bin/loop-sync
Source-sha256: 2a74f484bfe5249cb55b420c410ce13ca8a47f5ee8cded2fbc58ab8fe03f830e
State: re-authored

Also derived from that repository's bin/tests/test-loop-sync-strict.sh (tests/run.sh) and the
column layout of the author's LOOPS.md registry (format only; no rows copied).

## Deltas

- The source hard-coded unit names, hostnames, hook names and a service list. This version reads everything from a registry: `Hosts`, `Check` and `STATUS` columns located by header name. Nothing is built in.
- Registry path is a required flag (`--registry`, with `--file` and `LOOPS_FILE` kept as aliases); the default registry next to the repo is removed.
- Without a usable systemd user manager, timer and service checks report "not checkable" and never fail or crash (the source assumed systemd).
- Hook checks read a settings file given by `--settings` instead of a fixed home-directory path; without it they report "not checkable".
- Added `file:<path>` check type and a `--host` override; removed the cron-store check and the two registry-gated rollup checks tied to the author's scripts.
- Kept: `--strict` exits 1 when a wiring check fails or a row's STATUS starts with the dead marker; default run always exits 0; output is identical with and without `--strict`.
- Unknown flags now exit 2 instead of being ignored.
- Tests: the source's strict tests are ported to fixture registries; a fake `systemctl` on PATH drives the unit checks; added no-systemd, host-scope, hook, file and usage-error tests.
