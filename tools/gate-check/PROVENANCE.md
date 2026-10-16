# Provenance

Source: claude_improvement/bin/gate-check
Source-sha256: d0a0e42a7fb7e6dc3f538939c39b6be3de084a3942d39afee4d3398fef3a5bc5
State: adapted

## Deltas

- Fixed the ABANDON line parser: the source's lazy pattern captured only the first character of the gate id (`G5` became `G`), so abandoned gates were reported as unmet. The format `ABANDON: G5 <reason>` and `ABANDON: G5: <reason>` now both work.
- No other behaviour change; stdlib only. The source had no dedicated test file, so tests/run.sh is newly written (re-authored) and covers exit codes 0/1/2, EXPECT substring and regex, the manual-gate evidence rule, ABANDON and --dry-run.
