# Coverage and compatibility

Candidate execution observations on a local Linux test host, 2026-10-15. The `0.2.0-preview`
collection is **NOT PUBLISHED**; the current remote remains the old preview.
These are deterministic checks, not evidence of agent performance or benefits.

All runtime commands below were executed from the collection root.
Paths in this table are relative to that root; the
strict validator also used the absolute equivalents.

| Surface | Observed execution | Limits |
| --- | --- | --- |
| Offline structure | `python3 -B scripts/check_collection.py`: PASS, 12 skills / 6 roles / 6 commands | Targeted simple scalar frontmatter, exact inventories/catalog, local Markdown links and recognized backtick paths, candidate manifest fields, static regular-file/escape/symlink checks. Not exhaustive YAML/Markdown or a security race proof. |
| Negative fixtures | `python3 -B -m unittest discover -s tests -v`: 27 tests, OK (4.378s) | Temporary copies only; malformed/duplicate metadata, names, catalogs, missing assets, escaping references/source, symlinks, inventories and unbound commands; positive source-kit and placeholder cases. No guarded real-file mutations. |
| Parser fixture | `python3 -B -m unittest discover -s examples/worked-example/tests -v`: 5 tests, OK | Real parser/checker artifacts; empty, nonempty, malformed, nontext and list independence. Authored handoff records, not an AI builder/checker transcript. |
| Continuity fixture | `python3 -B examples/continuity/run.py`: `persisted Next executed: PASS (summary total=10)` | Temporary-directory fixed action, with fresh check/Next/verify processes. No arbitrary Next execution or production concurrency trial. |
| Journal copy | SHA-256 of source and shipped copy both `71a60f8f562f1e74a45396e0a26bf8111a86a0b38130298a5629167a9f3e686a` | Byte identity observed. Example source comparison showed only TOOL path resolution changed. |
| Claude schema | `claude plugin validate --strict .`, plus `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`: all passed | CLI schema checks, not plugin installation/loading. Initial marketplace warning about missing description was fixed; final validation passed. Non-verdict README installation advice remained. |
| Adapter sources | Preparation review supplied official documentation observations dated 2026-10-14; links in adapters | This fix worker did not fetch those pages. Documentation retrieval is not native behavior verification. |

The initial collection test run found an unbound-command detection gap (22 passed,
1 failed); the checker was corrected, then 23 tests passed. With the final added
catalog/health/placeholder fixtures, all 27 tests passed. This history is not
hidden as a clean first run.

**NOT RUN:** native Claude/Codex/OpenCode loading, real-agent dispatch or independent
AI judging, cross-OS behavior, public marketplace installation, user-artifact use,
and production concurrency. No universal native-agent import or benefit is claimed.
Hooks, rotation, isolation enforcement, permission enforcement, and automatic gates
remain intentionally outside this collection. License mapping is unchanged.
