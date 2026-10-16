# Changelog

## 0.4.0-preview — 2026-10-16

- README and ARCHITECTURE now tour the whole system: plan packs, parallel builds, maker/checker review and tribunal, guard hooks, handoffs, two harnesses, maintenance loops.
- New opt-in gates plugin (`plugins/gates/`, `ai-architecture-gates`): destructive-command and secret-read guard, per-model code-edit rules, review-before-stop and tracker checks, session-start resume. User-scope configuration only; hooks fail closed. Offline test runner included.
- New OpenCode bridge (`opencode/`) running the same guard hooks, with a structural check for Bash commands and an agent-block generator. Tested with a stub parser for the Bash check.
- Opt-in weighted model rotation for Claude Code (hook) and OpenCode (module), off by default and fail-safe.
- Added plan-pack, exec-pack and tribunal skills, the implementer, verifier, tribune and refuter roles, and the `plancheck`, `gate-check`, `tribunal-scribe`, `memcheck` and `loop-sync` tools, each with a provenance record and tests.
- Every ported file now has a provenance record (source, source hash, state, deltas); `scripts/check_collection.py` validates them, including under `plugins/` and `opencode/`, and derives the skill, role and command counts from the tree (21, 8, 6).
- `scripts/privacy_scan.py` runs before release to keep personal identifiers out of the tree.
- The marketplace lists both plugins; both are `0.4.0-preview`.
- OpenCode bridge: the model-gate check and the gates directory follow the same user-scope-only rule: `AIARCH_CONFIG` and `AIARCH_GATES_ROOT` are ignored, with a stderr warning, when they point inside the project (including the plugin directory); the gates directory is resolved once. The Bash classifier now auto-approves `ls` only without operands and with a flag allowlist (no `-L`, `-H`, `-R`); `stat`, `readlink`, `du`, `realpath` and `df` are no longer auto-approved, because a relative path can be a symlink out of the project.
- `scripts/check_collection.py`: multi-unit provenance records reject `#`/`###` headings inside units, need a `## <file>` heading for each covered file, and a non-verbatim unit needs at least one real delta.

## 0.3.2-preview — honesty patch

- README and manifest descriptions no longer claim a whole-system translation; they describe a partial, generic extract.
- Added a "What runs vs what is described" table to the README.
- `scripts/check_collection.py` gained a ported-unit class: ported units must carry a provenance record.
- Added `scripts/privacy_scan.py`, an offline scan for personal identifiers.

## 0.3.1-preview — 2026-10-15

- Renamed the route and status label for known entrypoints without a tool port to `no-port` (it also covers entrypoints with a manual playbook); `scripts/route.py` constant is now `NO_PORT`. Where text meant "not inspected" it says `unread`.
- README and licence presentation per the public repository template: banner, showcase structure, `LICENSE-docs` (CC BY 4.0), `CITATION.cff`, `.markdownlint.yaml`.
- Added `tools/banner/` and generated `assets/` (banner light/dark, social preview).
- `scripts/check_collection.py` now requires the new files.

## 0.3.0-preview — 2026-10-15

- Whole-system map: CAPABILITIES.md and WORKFLOWS.md map the capability families, their workflows and implementation status, including named gaps and inventory-needed entrypoints.
- Eighteen skills: added routing, convergence, lifecycle, discovery, operations and specialist journeys.
- Report-only routing CLI (`scripts/route.py`) with a connected system-journey example; unread entrypoints and unknown routes refuse instead of guessing.
- Standalone offline tools: memory/index report-only check, loop wiring snapshot, receipt/usage report, checker-verdict joins, continuity evidence validator and a manual context-manifest compiler.
- Selective-adoption fixture that copies single bundles into a fresh recipient root.
- License text restored to the full MIT wording.

## 0.2.0-preview — 2026-10-15

- Added a standalone, portable collection with Claude plugin metadata, twelve self-contained skills, roles, commands, templates, adapters, examples, and offline validation.
- Added optional continuity journal and deterministic fixture copied from the preview source.

## 0.1.0-preview

- Earlier recipe-kit preview retained outside this collection unchanged.
