# Provenance

Source: claude_improvement/bin/tribunal-scribe
Source-sha256: 426b45b92c1ab75305eda73107417c126936e00329258d30e08527a0a9b166e5
State: adapted

## Deltas

- Only the module docstring changed (private tool and plan-step references reworded). All integrity checks - transcript hash chain, non-scribe commit detection, lens validation, fence framing, verify - are unchanged and still fail closed.
- Tests ported from claude_improvement/bin/tests/test-tribunal-scribe.sh (sha256 c0885d676db7748d2cbd9ba53e42044c831d897633578467c0492e50d22dca6a) as tests/run.sh; scratch files now live in the temporary test directory instead of a shared temp path.
- Requires git and python3 (stdlib only); the tribunal workspace must be inside a git repository.
