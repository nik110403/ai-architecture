# Provenance: memcheck

Source: claude_improvement/bin/memcheck
Source-sha256: d9100b3e5be3c83069eaf69778a593d8721148420f39283033a8154309b02fd9
State: adapted

Also derived from claude_improvement/bin/lib/sanitize.sh (lib/sanitize.sh, near-verbatim) and
claude_improvement/bin/tests/test-memcheck.sh (tests/run.sh, extended).

## Deltas

- Memory location comes only from the required `--root <dir>` flag (memory dirs are `<root>/<slug>/memory`); the home-directory default and the `MEMCHECK_GLOB` override are removed.
- The realpath trust check on `--dir` is kept: it must resolve to `<root>/<one component>/memory`, so symlink escapes and nested `memory/deep/memory` dirs are refused. Added: the scan also skips any memory dir that resolves outside the root.
- The test-sentinel env overrides (`MEMCHECK_HOME_ROOT`, `LOOP_AUDIT_LOG`) are removed. No environment variable can relocate the trust root or the audit log; the audit log defaults to `<root>/.memcheck-audit.log` and can be set with `--audit-log`.
- The dead-path-reference check (paths under the author's config directory) is removed because it depends on one machine's layout. The remaining five checks are unchanged.
- Usage errors (missing or invalid `--root`) exit 2; the report itself still always exits 0.
- Comments and fixtures rewritten to generic, fictional content; no spec or plan references.
- Tests: the source's shorten tests are ported against fictional fixtures; added tests for scan checks, fix-index, trust-root refusals, env-var immunity, sanitization, and usage errors.
