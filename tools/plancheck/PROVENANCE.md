# Provenance

Source: claude_improvement/bin/plancheck
Source-sha256: 5bcffb35dd002fdc542cd6b8949a981ec3ad6b7b2bbb9ff2b261f2874ec539b4
State: adapted

## Deltas

- Exit status changed: now exits 1 when any blocking finding is reported and 0 otherwise; `--report-only` restores the original always-0 behaviour. Needed so the shipped example passes and the broken example fails.
- Default sweep is `./plans/*/` of the current directory; the hard-coded personal project registry and home-directory project root were removed (`PLANCHECK_DEFAULT_ROOT` override kept).
- `Gate: nik` renamed `Gate: human` and the Resume-waits-on-owner pattern generalised; report headings reworded accordingly.
- `Needs: none` is no longer reported as a dangling reference (root items legitimately have none); the source flagged it.
- All other checks (1-23) are unchanged. Dependencies: bash, awk, coreutils only.
- Tests ported from claude_improvement/bin/tests/test-plancheck.sh (sha256 3228b256b7e9b85b10b264d80752842cef34974515c6adc83488c49f32596215) as tests/run.sh; added exit-status and example-pack cases. Fictional sample packs added under example/ and example-broken/.
