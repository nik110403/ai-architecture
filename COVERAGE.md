# Coverage and compatibility

## Expanded checks

The expanded assets are part of this release. Independent review passed the
continuity snapshot checker (10 focused tests), five specialist manual playbooks
(12 entry points covered), and copied-recipient adoption (3 tests).
The checker-outcome report's six tests passed after correcting a stale fixture
expectation. The context manifest's 20 tests passed after correcting fenced-table
parsing, lexical path boundaries and capability-selected filesystem handling;
fresh final review passed their bounded behavior checks. The subsequent copied-
recipient link regression was corrected by copying the newly linked contracts;
final integrated disclosure review is separate from those behavior checks.
The collection suite currently passes 63 tests; this is authored/offline evidence.
Native Windows execution is NOT RUN; fallback behavior is tested by simulation
on Linux and is not a sandbox or race-proof filesystem boundary.

See [context manifest](skills/context/context-packet.md),
[continuity snapshot](skills/continuity/continuity-check.md),
[checker outcomes](skills/evaluation/evaluation-report.md) and
[recipient fixture](examples/adoption/README.md) for their exact scope.

## Local memory-check disposition

The standalone memory/index reporter is usable as an explicit-root, report-only
diagnostic, with a known input-grammar limitation. Its nine focused tests and the
57-test collection suite pass, and an independent check confirmed that symlink
ancestors stop reference traversal. Independent review did **not** fully pass:
an opening parenthesis in a Markdown link target is reported as a missing target rather than unsupported
syntax. Do not treat the reporter as a complete Markdown validator or native
memory enforcement. This limitation remains open; passing fixture tests do not
certify the rest of the repository.


Execution observations on a local Linux test host, 2026-10-16, for release
`0.4.1-preview`.
These are deterministic checks, not evidence of agent performance or benefits.

All runtime commands below were executed from the collection root.
Paths in this table are relative to that root; the
strict validator also used the absolute equivalents.

| Surface | Observed execution | Limits |
| --- | --- | --- |
| Offline structure | `python3 -B scripts/check_collection.py`: PASS, 21 skills / 8 roles / 6 commands | Exact inventories/catalog (counts are asserted from the tree, not typed in), local links, both plugin manifests, provenance records under `skills/`, `agents/`, `tools/`, `plugins/` and `opencode/`, and static regular-file/escape/symlink paths. Not exhaustive YAML/Markdown or a security race proof. |
| Negative fixtures | Current collection-root discovery: 63 tests, OK (the earlier 39-test figure predates the plugin and provenance checks) | Temporary copies plus typed route availability, whole-word/explicit intent selection, conditional external approval, bounded review disposition, pending/unknown lifecycle, and copied recipient bundles. No real-user project or real-agent trial. |
| Parser fixture | `python3 -B -m unittest discover -s examples/worked-example/tests -v`: 5 tests, OK (0.001s) | Real parser/checker artifacts; empty, nonempty, malformed, nontext and list independence. Authored handoff records, not an AI builder/checker transcript. |
| System journey | `python3 -B examples/system-journey/run.py`: PASS | Authored route/brief/result/check/recovery fixture with refusals for entry points with no tool and for pending jobs; no provider discovery, dispatch, or Next execution. |
| Continuity fixture | `python3 -B examples/continuity/run.py`: `persisted Next executed: PASS (summary total=10)` | Temporary-directory fixed action, with fresh check/Next/verify processes. No arbitrary Next execution or production concurrency trial. |
| Journal copy | SHA-256 of source and shipped copy both `71a60f8f562f1e74a45396e0a26bf8111a86a0b38130298a5629167a9f3e686a` | Byte identity observed. Example source comparison showed only TOOL path resolution changed. |
| Claude schema | `claude plugin validate --strict .` and `claude plugin validate --strict plugins/gates`: both passed | CLI schema checks, not plugin installation/loading. Initial marketplace warning about missing description was fixed; final validation passed. Non-verdict README installation advice remained. |
| Adapter sources | Preparation review supplied official documentation observations dated 2026-10-14; links in adapters | Those pages were not re-fetched for this release. Documentation retrieval is not native behavior verification. |
| Memory-check fixture | `python3 -B skills/discovery/scripts/test_memory_check.py`: 9 tests, OK (temporary explicit-root clean, drift, incomplete, sorting, strict, immutability, copied-bundle, malformed-link, and symlink-hop cases) | Report-only portable fixture observation. Symlink-hop instrumentation observed no descendant outside-root `lstat`/open for index, wikilink, or configured-path references; this is not a sandbox or race-proof claim. No native memory runtime, index repair, global scan, or gate is ported. |
| Loop-check fixture | `python3 -B skills/maintenance/scripts/test_loop_check.py`: focused explicit-snapshot tests | Authored JSON only: validates all inventory rows, reports checked/info/drift/unavailable/skipped, and reads no referenced files. `complete` describes snapshot coverage only, not scheduler health, freshness, installation, or live proof. No host probes, scheduling, or remediation are ported. |
| Gates plugin (opt-in hooks) | `bash plugins/gates/tests/run.sh`: 156 destructive-guard cases, 57 hook tests, 81 configuration-scope tests, 0 failed | Bash test runner replaying recorded hook payloads and transcript fixtures with an empty `HOME`, including a project-level config that tries to loosen a gate. It does not run Claude Code itself: hook payload and transcript formats are as recorded, and a Claude Code update may change them. Static text analysis of commands is a tripwire, not a sandbox. |
| OpenCode bridge | `bash opencode/tests/run.sh`: agent-generator golden test plus 29 bridge tests under `bun` | The bridge spawns the real gates hooks, including a denied destructive command, symlink and `..` refusals, fail-closed behaviour on a crashed or unreadable hook list, and a Bash classifier allow-list check. The Bash structural check is tested against a **stub parser**: the real `web-tree-sitter` and `tree-sitter-bash` packages are not exercised. Not run against a live OpenCode session. Without `bun` that part prints `NOT RUN`. |
| Model rotation (opt-in) | `bash opencode/rotation/tests/run.sh`: Claude Code hook tests and the OpenCode module tests, 0 failed (the OpenCode module half runs only with `bun`; without it the runner prints `NOT RUN` for that half) | Seeded weighted draws, fail-safe on invalid pools, no unlisted model ever produced. Rotation is off until you enable it; no quality benefit of rotating models is claimed. |
| Tools | `plancheck` 19, `gate-check` 8, `memcheck` 48, `loop-sync` 26, `tribunal-scribe` 13 checks, 0 failed (each tool's own test runner) | Fixture-based, offline, Linux. |
| Privacy scan | `python3 -B scripts/privacy_scan.py`: exit 0 | Pattern scan for personal identifiers over the tree; it cannot prove the absence of every personal detail. |
| Receipt-report numeric correction | CLI regressions cover two `cost_usd: 1e308` rows, two `dur_ms: 1e308` rows, integer `cost_usd: 10**400`, exact large integer median, mixed-range failure, and invocation from the exact documented selective bundle | Deviation: aggregates must fit Python arithmetic and strict JSON serialization. Float maximum on the tested host is `1.7976931348623157e308`; decimal integer input/output retains the interpreter digit limit (default 4300). Overflow/range failure produces exit 2, stderr diagnostic and no report/traceback in either format, not null/clipping/dropped data. See [receipt-report.md](skills/evaluation/receipt-report.md); the original all-finite-number requirement is not met; this range limit is the accepted behavior. |

The initial collection test run found an unbound-command detection gap (22 passed,
1 failed); the checker was corrected, then 23 tests passed. With the added
catalog/health/placeholder fixtures, all 27 tests passed at that stage; the current
suite is 63 tests. This history is not
hidden as a clean first run.

**NOT RUN:** native Claude/Codex/OpenCode loading, real-agent dispatch or independent
AI judging, cross-OS behavior, public marketplace installation, user-artifact use,
and production concurrency. No universal native-agent import or benefit is claimed.
The guard hooks, the OpenCode bridge and rotation are included and tested as described above, but only
on the recorded payloads and stub parser named there; no live Claude Code or OpenCode session was driven through
them. Isolation enforcement, permission enforcement and the maintenance timers are not included.

The source-grounded map in [CAPABILITIES.md](CAPABILITIES.md) and process contracts in
[WORKFLOWS.md](WORKFLOWS.md) are documentation, not new runtime observations. They do
not establish that the whole private setup is reproduced, native routing, or platform behavior.
