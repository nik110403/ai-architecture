# Changelog

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
