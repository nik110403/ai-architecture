# Coverage and compatibility

## Test counts (generated)

The block below is written by `python3 -B scripts/coverage_counts.py` and checked by
`python3 -B scripts/coverage_counts.py --check`; counts are not typed by hand. "Count" is the
runner's own summary (passed or ran), `PASS, part NOT RUN` marks a run in which a suite skipped a
step because a tool such as `bun` or a browser was absent, and `NOT RUN` stays `NOT RUN`. Counts
are what this host ran; a host with fewer tools skips more.

<!-- counts:begin -->
| Suite | Command | Result | Count |
| --- | --- | --- | --- |
| `collection structure` | `python3 -B scripts/check_collection.py` | PASS | n/a |
| `privacy scan` | `python3 -B scripts/privacy_scan.py` | PASS | n/a |
| `unittest tests/` | `python3 -B -m unittest discover -s tests` | PASS, part NOT RUN | 1651 tests, 10 skipped |
| `pytest tests, worked example` | `python3 -B -m pytest -q -p no:cacheprovider tests examples/worked-example/tests` | PASS | 151 tests |
| `skills/context/scripts/test_context_packet.py` | `python3 -B skills/context/scripts/test_context_packet.py` | PASS | 20 tests |
| `skills/continuity/scripts/test_continuity_check.py` | `python3 -B skills/continuity/scripts/test_continuity_check.py` | PASS | 10 tests |
| `skills/discovery/scripts/test_memory_check.py` | `python3 -B skills/discovery/scripts/test_memory_check.py` | PASS | 9 tests |
| `skills/evaluation/scripts/test_evaluation_report.py` | `python3 -B skills/evaluation/scripts/test_evaluation_report.py` | PASS | 6 tests |
| `skills/evaluation/scripts/test_receipt_report.py` | `python3 -B skills/evaluation/scripts/test_receipt_report.py` | PASS | 13 tests |
| `skills/maintenance/scripts/test_loop_check.py` | `python3 -B skills/maintenance/scripts/test_loop_check.py` | PASS | 7 tests |
| `native-system example` | `python3 -B -m unittest discover -s examples/native-system/tests` | PASS | 69 tests |
| `modules/autopilot/tests` | `python3 -B -m unittest discover -s modules/autopilot/tests` | PASS | 72 tests |
| `modules/browser/tests` | `python3 -B -m unittest discover -s modules/browser/tests` | PASS | 31 tests |
| `modules/browser-operations/tests` | `python3 -B -m unittest discover -s modules/browser-operations/tests` | PASS | 31 tests |
| `modules/config-sync/tests` | `python3 -B -m unittest discover -s modules/config-sync/tests` | PASS | 18 tests |
| `modules/continuity/tests` | `python3 -B -m unittest discover -s modules/continuity/tests` | PASS | 359 tests |
| `modules/control-plane-adapters/tests` | `python3 -B -m unittest discover -s modules/control-plane-adapters/tests` | PASS | 24 tests |
| `modules/design-pipeline/tests` | `python3 -B -m unittest discover -s modules/design-pipeline/tests` | PASS | 104 tests |
| `modules/development/tests` | `python3 -B -m unittest discover -s modules/development/tests` | PASS | 82 tests |
| `modules/discovery-inbox/tests` | `python3 -B -m unittest discover -s modules/discovery-inbox/tests` | PASS, part NOT RUN | 27 tests, 2 skipped |
| `modules/documents/tests` | `python3 -B -m unittest discover -s modules/documents/tests` | PASS | 44 tests, 1 skipped |
| `modules/git-automation/tests` | `python3 -B -m unittest discover -s modules/git-automation/tests` | PASS | 31 tests |
| `modules/installer/tests` | `python3 -B -m unittest discover -s modules/installer/tests` | PASS | 25 tests |
| `modules/isolation/tests` | `python3 -B -m unittest discover -s modules/isolation/tests` | PASS | 15 tests |
| `modules/knowledge-adapters/tests` | `python3 -B -m unittest discover -s modules/knowledge-adapters/tests` | PASS | 11 tests |
| `modules/maintenance/tests` | `python3 -B -m unittest discover -s modules/maintenance/tests` | PASS | 22 tests |
| `modules/media/tests` | `python3 -B -m unittest discover -s modules/media/tests` | PASS | 78 tests |
| `modules/memory/tests` | `python3 -B -m unittest discover -s modules/memory/tests` | PASS | 20 tests |
| `modules/memory-backends/tests` | `python3 -B -m unittest discover -s modules/memory-backends/tests` | PASS | 71 tests |
| `modules/memory-operations/tests` | `python3 -B -m unittest discover -s modules/memory-operations/tests` | PASS | 34 tests |
| `modules/model-evaluation/tests` | `python3 -B -m unittest discover -s modules/model-evaluation/tests` | PASS | 42 tests |
| `modules/observability/tests` | `python3 -B -m unittest discover -s modules/observability/tests` | PASS | 29 tests |
| `modules/orchestration/tests` | `python3 -B -m unittest discover -s modules/orchestration/tests` | PASS | 128 tests |
| `modules/productivity/tests` | `python3 -B -m unittest discover -s modules/productivity/tests` | PASS | 27 tests |
| `modules/research-adapter/tests` | `python3 -B -m unittest discover -s modules/research-adapter/tests` | PASS | 49 tests, 1 skipped |
| `modules/research-integrity/tests` | `python3 -B -m unittest discover -s modules/research-integrity/tests` | PASS | 71 tests, 6 skipped |
| `modules/review/tests` | `python3 -B -m unittest discover -s modules/review/tests` | PASS | 9 tests |
| `modules/state-adapters/tests` | `python3 -B -m unittest discover -s modules/state-adapters/tests` | PASS | 9 tests |
| `modules/stewardship/tests` | `python3 -B -m unittest discover -s modules/stewardship/tests` | PASS | 16 tests |
| `modules/writing/tests` | `python3 -B -m unittest discover -s modules/writing/tests` | PASS | 26 tests |
| `tools/gate-check/tests/run.sh` | `HOME=<temp dir> bash tools/gate-check/tests/run.sh` | PASS | 9 tests |
| `tools/loop-sync/tests/run.sh` | `HOME=<temp dir> bash tools/loop-sync/tests/run.sh` | PASS | 33 tests |
| `tools/memcheck/tests/run.sh` | `HOME=<temp dir> bash tools/memcheck/tests/run.sh` | PASS | 55 tests |
| `tools/plancheck/tests/run.sh` | `HOME=<temp dir> bash tools/plancheck/tests/run.sh` | PASS | 33 tests |
| `tools/tribunal-scribe/tests/run.sh` | `HOME=<temp dir> bash tools/tribunal-scribe/tests/run.sh` | PASS | 25 tests |
| `plugins/gates/tests/run.sh` | `HOME=<temp dir> bash plugins/gates/tests/run.sh` | PASS | 247 tests |
| `opencode/tests/run.sh` | `HOME=<temp dir> bash opencode/tests/run.sh` | PASS, part NOT RUN | 113 tests |
| `opencode/rotation/tests/run.sh` | `HOME=<temp dir> bash opencode/rotation/tests/run.sh` | PASS | 59 tests |
<!-- counts:end -->

## Support states in the capability registry

`capabilities/registry.json` gives each capability one state; `python3 -B scripts/capabilities.py status [ID]` prints it.
`partial` means the row's behaviour runs from this collection on a recipient's machine, possibly with fakes or fixtures, or
with common system tools the user installs. Every such tool is listed in the row's `optional_requirements` (so `status ID`
prints MISSING for it), and `next_setup_action` states what is fake or missing. `planned` means declarations only, or the row
needs a companion or backend that is not part of this collection, so the default install result is MISSING. No row is
`available`. Module descriptors keep `claimed_support: planned` because the contract accepts `partial` only with
a native harness receipt (a recorded run under a real agent harness), and none is recorded; the registry rows carry the
per-capability state.

## Expanded checks

The expanded assets are part of this release. Independent review passed the
continuity snapshot checker, five specialist manual playbooks
and copied-recipient adoption (counts: generated block below).
The checker-outcome report's tests passed after correcting a stale fixture
expectation. The context manifest's tests passed after correcting fenced-table
parsing, lexical path boundaries and capability-selected filesystem handling;
fresh final review passed their bounded behavior checks. The subsequent copied-
recipient link regression was corrected by copying the newly linked contracts;
final integrated disclosure review is separate from those behavior checks.
The collection suite's pass count is in the generated block below; this is authored/offline evidence.
Native Windows execution is NOT RUN; fallback behavior is tested by simulation
on Linux and is not a sandbox or race-proof filesystem boundary.

See [context manifest](skills/context/context-packet.md),
[continuity snapshot](skills/continuity/continuity-check.md),
[checker outcomes](skills/evaluation/evaluation-report.md) and
[recipient fixture](examples/adoption/README.md) for their exact scope.

## Local memory-check disposition

The standalone memory/index reporter is usable as an explicit-root, report-only
diagnostic, with a known input-grammar limitation. Its focused tests and the
collection suite pass (counts: generated block below), and an independent check confirmed that symlink
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
| Offline structure | `python3 -B scripts/check_collection.py`: PASS, 21 skills / 13 roles / 6 commands | Exact inventories/catalog (counts are asserted from the tree, not typed in), local links, both plugin manifests, provenance records under `skills/`, `agents/`, `tools/`, `plugins/` and `opencode/`, and static regular-file/escape/symlink paths. Not exhaustive YAML/Markdown or a security race proof. |
| Negative fixtures | Current collection-root discovery: see the generated block (an earlier, smaller count predates the plugin and provenance checks) | Temporary copies plus typed route availability, whole-word/explicit intent selection, conditional external approval, bounded review disposition, pending/unknown lifecycle, and copied recipient bundles. No real-user project or real-agent trial. |
| Parser fixture | `python3 -B -m unittest discover -s examples/worked-example/tests -v`: OK | Real parser/checker artifacts; empty, nonempty, malformed, nontext and list independence. Authored handoff records, not an AI builder/checker transcript. |
| System journey | `python3 -B examples/system-journey/run.py`: PASS | Authored route/brief/result/check/recovery fixture with refusals for entry points with no tool and for pending jobs; no provider discovery, dispatch, or Next execution. |
| Continuity fixture | `python3 -B examples/continuity/run.py`: `persisted Next executed: PASS (summary total=10)` | Temporary-directory fixed action, with fresh check/Next/verify processes. No arbitrary Next execution or production concurrency trial. |
| Native system journey | `python3 -B examples/native-system/run.py --offline` then `python3 -B examples/native-system/verify_receipt.py`; `python3 -B -m unittest discover -s examples/native-system/tests` | offline + fake only; native live run: NOT RUN. Temporary-directory request, SIGKILL of the coordinator, restart that consumes its own Next, separate checker, `ready`; fake `claude`/`opencode` stand-ins only. No real harness, model or adapter has been exercised; a receipt is evidence against mistakes, not a forger. |
| Browser protocol fixture | `python3 -B -m unittest discover -s modules/browser/tests`; `python3 -B examples/browser-local/run.py` | local fixture backend only; real browser backend: NOT RUN (U10). Loopback `http.server` pages and file fixtures; stdlib parser, state-bound approval, separate reviewer process, single-use approval, loopback-only fence. A receipt is evidence against mistakes, not a forger; page text is untrusted data. |
| Journal copy | SHA-256 of source and shipped copy both `71a60f8f562f1e74a45396e0a26bf8111a86a0b38130298a5629167a9f3e686a` | Byte identity observed. Example source comparison showed only TOOL path resolution changed. |
| Claude schema | `claude plugin validate --strict .` and `claude plugin validate --strict plugins/gates`: both passed | CLI schema checks, not plugin installation/loading. Initial marketplace warning about missing description was fixed; final validation passed. Non-verdict README installation advice remained. |
| Adapter sources | Preparation review supplied official documentation observations dated 2026-10-14; links in adapters | Those pages were not re-fetched for this release. Documentation retrieval is not native behavior verification. |
| Memory-check fixture | `python3 -B skills/discovery/scripts/test_memory_check.py`: OK (temporary explicit-root clean, drift, incomplete, sorting, strict, immutability, copied-bundle, malformed-link, and symlink-hop cases) | Report-only portable fixture observation. Symlink-hop instrumentation observed no descendant outside-root `lstat`/open for index, wikilink, or configured-path references; this is not a sandbox or race-proof claim. No native memory runtime, index repair, global scan, or gate is ported. |
| Loop-check fixture | `python3 -B skills/maintenance/scripts/test_loop_check.py`: focused explicit-snapshot tests | Authored JSON only: validates all inventory rows, reports checked/info/drift/unavailable/skipped, and reads no referenced files. `complete` describes snapshot coverage only, not scheduler health, freshness, installation, or live proof. No host probes, scheduling, or remediation are ported. |
| Gates plugin (opt-in hooks; bash guard: tripwire (declared limits)) | `bash plugins/gates/tests/run.sh`: destructive-guard cases, hook tests and configuration-scope tests, 0 failed (counts: generated block) | Bash test runner replaying recorded hook payloads and transcript fixtures with an empty `HOME`, including a project-level config that tries to loosen a gate. It does not run Claude Code itself: hook payload and transcript formats are as recorded, and a Claude Code update may change them. Static text analysis of commands is a tripwire, not a sandbox. |
| OpenCode bridge | `bash opencode/tests/run.sh`: agent-generator golden test plus the bridge tests under `bun` | The bridge spawns the real gates hooks, including a denied destructive command, symlink and `..` refusals, fail-closed behaviour on a crashed or unreadable hook list, and a Bash classifier allow-list check. The Bash structural check is tested against a **stub parser**: the real `web-tree-sitter` and `tree-sitter-bash` packages are not exercised. Not run against a live OpenCode session. Without `bun` that part prints `NOT RUN`. |
| Model rotation (opt-in) | `bash opencode/rotation/tests/run.sh`: Claude Code hook tests and the OpenCode module tests, 0 failed (the OpenCode module half runs only with `bun`; without it the runner prints `NOT RUN` for that half) | Seeded weighted draws, fail-safe on invalid pools, no unlisted model ever produced. Rotation is off until you enable it; no quality benefit of rotating models is claimed. |
| Observability and model evaluation modules (staged) | `python3 -B -m unittest discover -s modules/observability/tests`; `python3 -B -m unittest discover -s modules/model-evaluation/tests` | offline + fake only; real provider quota, real model runs and a live rotation reweight: NOT RUN. Usage, price and cache gaps stay `null` (never a zero cost or a ranking); quota is read from an operator snapshot file only (no network, no provider API; other adapters are unsupported); the reweighter moves integer weights only on matched per-task evidence, with sample, floor and cap limits, a recomputing apply and an exact rollback; outcome counts never rank. Accounting is staged: no price data, no per-account accounting. Module descriptors claim `planned` (see Support states above); registry rows 59-61 are `partial`. |
| OpenCode continuity runtime (opt-in) | `bash opencode/runtime/tests/run.sh`: bun tests against a fake OpenCode client and the real continuity CLI in a temporary project, 0 failed (without `bun` the runner prints `NOT RUN` and exits 1) | Usage recorded for root sessions and left unknown when a token count is missing (readiness reports `usage UNKNOWN`), the checkpoint Next consumed once across a simulated restart, freeze denies outside edits and bash for the frozen session only and agrees with `freeze check` on a path table, the HANDOFF gate, the marker-env switch-off, and a hub limited to read roles that registers before prompting and banks once (completed, cancelled, orphaned). Not run against a live OpenCode session or a real model; the `@opencode-ai/plugin` tool wrapper is not unit tested; the plugin cannot sandbox a hub child. |
| Tools | `plancheck`, `gate-check`, `memcheck`, `loop-sync`, `tribunal-scribe`: 0 failed (each tool's own test runner; counts in the generated block) | Fixture-based, offline, Linux. |
| Continuity module (durable job ledger) | `python3 -B -m unittest discover -s modules/continuity/tests`: ledger, job, reconcile and concurrency tests with real dispatcher subprocesses and fake `local-command` workers | Linux only (`fcntl.flock`, `/proc`, `pidfd`). Proves, offline: exactly-once banking under 8 concurrent processes, failed and invalid ledger writes leave the previous revision, cancel, crash and restart, late results, drain, the recovery table (identity, stuck, worker, worktree rules) and that external jobs are never signalled. Does not prove: any real harness, a real model, multi-host or network filesystems, or a hostile same-user worker (the ledger is the coordinator's record, not a security boundary). Also proves offline, with fake workers and synthetic transcripts: checkpoint hash receipts, READY with every failing condition listed and unknown usage never ready, phases and the handoff gate, child budget judgement and pool, the Next delivered once (compaction never consumes), and the Claude Code resume and usage hooks. Freeze and context consumption are not in this row. |
| Privacy scan | `python3 -B scripts/privacy_scan.py`: exit 0 | Pattern scan for personal identifiers over the tree; it cannot prove the absence of every personal detail. |
| Receipt-report numeric correction | CLI regressions cover two `cost_usd: 1e308` rows, two `dur_ms: 1e308` rows, integer `cost_usd: 10**400`, exact large integer median, mixed-range failure, and invocation from the exact documented selective bundle | Deviation: aggregates must fit Python arithmetic and strict JSON serialization. Float maximum on the tested host is `1.7976931348623157e308`; decimal integer input/output retains the interpreter digit limit (default 4300). Overflow/range failure produces exit 2, stderr diagnostic and no report/traceback in either format, not null/clipping/dropped data. See [receipt-report.md](skills/evaluation/receipt-report.md); the original all-finite-number requirement is not met; this range limit is the accepted behavior. |

The initial collection test run found an unbound-command detection gap (22 passed,
1 failed); the checker was corrected, then 23 tests passed. With the added
catalog/health/placeholder fixtures, all 27 tests passed at that stage; the current
count is in the generated block. This history is not
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
