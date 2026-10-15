# Capability map

## Reading status

This map is a dated, source-grounded translation of the current operating model, not
a runtime audit. **Workflow** means an inspected process that can be described here;
**tool/adapter required** means a host-specific provider is needed; **missing port**
means no corresponding public implementation exists; **inventory-needed** means an
entry was observed but not inspected deeply enough to state behavior; **experimental**
means staged or source-stated only; **excluded** means deliberately not public.
Runtime missing is different from unread: neither is evidence that a capability runs.
A short checklist is not automated routing or enforcement.

The current local candidate exposes 18 skills, 6 roles, and 6 commands in
[CATALOG.md](CATALOG.md). Its optional journal remains byte-identical to its recorded
source copy; that identity is a packaging fact, not continuity automation.

## Routing and coordination

Each row is an operational contract: **trigger/input → decisions and roles → done
evidence → stop/fallback → dependency and public state**. “Recipient config” means
the inspected source does not supply a portable policy; it is not an implicit default.

| Family | Operational contract | Public status |
| --- | --- | --- |
| Routing, models, fallback | **Trigger/input:** a new intent plus explicit capability/model availability. **Order/roles:** classify; report bounded roles, stable IDs, checks, stops, and a generic configured-model order; unavailable checker seats remain visible. **Done:** COMPLETE or a recorded REFUSE/clarification. **Stop:** unread, ambiguous, critical-missing, pending, or unknown-arrival state refuses. | **Report-only local port.** [routing](skills/routing/SKILL.md) and `scripts/route.py` do not dispatch, discover tools, enforce permissions, or carry personal model/account configuration. |
| Execution and delegation | **Trigger/input:** decided scope, named inputs, acceptance checks. **Order/roles:** planner/orchestrator writes a bounded brief; builder makes the artifact; fresh verifier checks it. **Done:** typed result plus check evidence and Next. **Stop:** ambiguous architecture/interface returns to planning; failed check repairs only within the agreed budget. **Dependency:** recipient dispatch and test tool. | **Workflow; partial asset.** [planning](skills/planning/SKILL.md), [delegation](skills/delegation/SKILL.md), [execution](skills/execution/SKILL.md), [builder](agents/builder.md), and [verifier](agents/verifier.md). |
| Context and cache | **Trigger/input:** a task with source material. **Order/roles:** reader/orchestrator searches before broad reads, batches bounded reads, and sends lean briefs. **Done:** current context and stated unknowns. **Stop/fallback:** omitted or stale source remains unknown; cache retention/eviction is recipient config. **Dependency:** search/read facility. | **Workflow; partial asset.** [context](skills/context/SKILL.md) is guidance; cache accounting and enforcement are missing. |
| Continuity, jobs, results | **Trigger/input:** checked work, results, and pending jobs. **Order/roles:** coordinator banks results, records pending state, saves exact Next; at handoff it stops discretionary work. **Done:** durable restart record with Next and unresolved risk. **Stop/fallback:** unknown job or failed receipt prevents a readiness claim; ledger/hash/mtime policy is recipient config. **Dependency:** durable storage. | **Workflow; local lifecycle asset.** [lifecycle](skills/lifecycle/SKILL.md), [continuity](skills/continuity/SKILL.md), and journal are manual/local; job ledger, readiness receipts, and concurrency handling are missing. |
| Stakes, tribunal, convergence | **Trigger/input:** artifact, stakes, and frozen evidence. **Order/roles:** scale review; independent checker lenses review; refuter tests one finding; coordinator integrates. **Done:** checked disposition or explicitly parked risk. **Stop:** severe unresolved issue is parked/descope; two repair-review rounds bound the local process. **Dependency:** independent reviewers. | **Workflow; local process asset.** [convergence](skills/convergence/SKILL.md) preserves missing seats; it is not a tribunal runner or enforcement engine. |
| Evaluation and accounting | **Trigger/input:** completed or exploratory work and known usage. **Order/roles:** coordinator records work, model/tool when known, outcome, usage/cost, and uncertainty; separates exploration from release evidence. **Done:** a measurement record with unknowns preserved. **Stop/fallback:** missing cost or verdict stays UNKNOWN; no score is fabricated. **Dependency:** receipts/telemetry supplied by recipient. | **Workflow; bounded offline asset.** [evaluation](skills/evaluation/SKILL.md), [measurement template](templates/measurement.md), and `skills/evaluation/scripts/receipt_report.py` summarize only explicit strict-v1 JSONL inputs. They do not discover receipts, create telemetry, join checker outcomes, or provide a scoreboard. |
| Loops, drift, autonomy | **Trigger/input:** declared cadence/condition and authorized scope. **Order/roles:** maintainer defines tier, trigger, evidence, stop/rollback; scheduled/headless run reports rather than acts unless separately authorized. **Done:** checked report or authorized rollback evidence. **Stop:** missing authorization/wiring is a GAP. **Dependency:** scheduler and harness permissions. | **Workflow; selective snapshot report.** [maintenance](skills/maintenance/SKILL.md) includes `skills/maintenance/scripts/loop_check.py`, a stdlib-only report over two explicit authored JSON inventories. It does not probe native scheduling, services, hooks, freshness, or health; automatic remediation remains missing. |

## Specialist families

| Family | Operational contract | Public status |
| --- | --- | --- |
| Research and references | **Trigger/input:** academic question and source scope. **Order/roles:** researcher makes brief → search/index → source note/citation graph → integrity/verification → deposit. **Done:** attributable source set and checked notes. **Stop:** degraded provider, retraction concern, or unverified claim remains explicit. **Dependency:** search/citation/verification providers. | **Workflow; companion adapter required.** [research](skills/research/SKILL.md) is bounded; [research-kit](https://github.com/nik110403/research-kit) is independent. Full tooling is unported. |
| Empirical audits and referees | **Trigger/input:** claim, exhibit, script, and data vintage. **Order/roles:** auditor maps them, checks statistics/merges/leakage, then fresh reviewer breaks the code; refuter tests one finding. **Done:** finding disposition or NOT_CHECKED label. **Stop:** absent lineage/check blocks a release verdict. **Dependency:** executable analysis and independent reviewer. | **Workflow; missing port.** No audit/referee runner or report contract. |
| Design and visualization | **Trigger/input:** UI or visual-output brief. **Order/roles:** establish product/design decisions; one builder refines; motion/audit follows; critic inspects rendered artifact/recap. **Done:** artifact plus inspection evidence. **Stop:** unavailable renderer/connector is a GAP. **Dependency:** renderer and visual tools. | **Workflow; partial asset/tool required.** [design](skills/design/SKILL.md) and [design-critic](agents/design-critic.md); lifecycle/visual ports are missing. |
| Browser and external actions | **Trigger/input:** proposed external act and observed state. **Order/roles:** survey → proposal → independent review → state-bound approval → authorized deterministic act → receipt. **Done:** receipt matching approval. **Stop:** takeover prompt, mismatch, or absent browser/approval stops before submission. **Dependency:** browser/connector and recipient authorization. | **Workflow; missing adapter.** [safety](skills/safety/SKILL.md) is a boundary, not browser control or ledger. |
| Discovery and bootstrap | **Trigger/input:** confirmed project intent/location. **Order/roles:** coordinator confirms → inventories instructions/memory → scaffolds/registers where adapters exist → verifier checks → snapshots. **Done:** verified inventory/scaffold evidence. **Stop:** existing/ambiguous state is not guessed. **Dependency:** templates, registry, and checker. | **Workflow; partial asset.** [discovery](skills/discovery/SKILL.md) and [workspace](skills/workspace/SKILL.md); general bootstrap/register/verify ports missing. |
| Memory and instructions | **Trigger/input:** curated memory/instruction inventory. **Order/roles:** deterministic linter checks links/paths/index/freshness; operator distinguishes dry-run from apply. **Done:** check report or bounded applied repair. **Stop:** unclear scope/failing integrity check blocks health claim. **Dependency:** memory store and linter. | **Workflow; partial tool.** Report-only [memory_check.py](skills/discovery/scripts/memory_check.py) checks links/index/freshness; no store, repair automation or native gate. |
| Scout and provenance | **Trigger/input:** need for a reusable capability. **Order/roles:** scout inventories local options → searches sources → vets security/provenance/license → records use/fork/create/reject disposition. **Done:** evidence-backed disposition. **Stop:** unknown provenance/license stays unresolved. **Dependency:** local inventory and search sources. | **Inspected generic workflow; report-only process.** [operations](skills/operations/SKILL.md) supplies no scout/search automation; the named `skill-scout` entrypoint has a manual [playbook](skills/specialist-journeys/playbooks.md) but no tool port, so `scripts/route.py` refuses it as inventory-needed. |
| Retro and promotion | **Trigger/input:** bounded session evidence and candidate pattern. **Order/roles:** reviewer identifies candidate → tests repetition/outcome/delta → deduplicates → submits for approval. **Done:** approved promotion or rejected/parked candidate. **Stop:** insufficient repetition/evidence prevents promotion. **Dependency:** session records and approver. | **Inspected generic workflow; report-only process.** [operations](skills/operations/SKILL.md) never rewrites rules automatically; named `retro` and `instinct-promote` have manual [playbooks](skills/specialist-journeys/playbooks.md) but no tool port, so `scripts/route.py` refuses them as inventory-needed. |
| Generic writing | **Trigger/input:** purpose, register, and factual sources. **Order/roles:** writer drafts; reviewer checks fidelity, bounded diagnosis, and intentional choices. **Done:** disclosed draft/review outcome. **Stop:** unknown fact remains unknown; no automatic send/publish/train/rerender. **Dependency:** recipient-selected writer/reviewer. | **Workflow; excluded personal layer.** Calibration, samples, and engines are absent. |
| Documents and formats | **Trigger/input:** requested format and existing artifact/revision. **Order/roles:** select provider/editor → create/edit/read → render/package/formula inspect. **Done:** inspected requested format. **Stop:** missing provider stops; existing cloud file is not recreated blindly. **Dependency:** format-specific tool/connector. | **Workflow; adapter required.** Word, PDF, spreadsheet, presentation, and cloud implementations are not bundled. |
| Code, security, debugging, domain quality | **Trigger/input:** decided change, defect evidence, security concern, or domain scenario. **Order/roles:** builder prefers reuse/standard facilities; debugger reproduces before hypothesis; security reviewer separates new-code safeguards from existing-code review; domain reviewer compares terms/scenarios. **Done:** behavior evidence or contradiction/UNKNOWN record. **Stop:** meaning change, unproved root cause, or unreviewed risk returns to decision. **Dependency:** tests and specialist review. | **Workflow; partial/missing ports.** [execution](skills/execution/SKILL.md), [review](skills/review/SKILL.md), and security role are generic; detailed ports/enforcement missing. |

## Additional executable and inspected assets

The following additions refine the family-level status above; they do not supply
the missing native runtime layers:

- [Manual context manifest](skills/context/context-packet.md): explicit project
  routing table to deterministic JSON, admitted/missing paths and warn-only size
  estimate; no task-body loading, dispatch, cache enforcement or verified consumer.
- [Continuity snapshot checker](skills/continuity/continuity-check.md): explicit
  tracker hash/mtime/Resume/Next against caller-declared jobs and observations;
  CONSISTENT is not authenticated native readiness or semantic correctness.
- [Checker-outcome report](skills/evaluation/evaluation-report.md): offline
  charge-ID joins with latest known-time verdicts; no live telemetry, sibling
  inference, causal model comparison or ranking. Accounting numeric limits remain.
- [Memory/index reporter](skills/discovery/SKILL.md): an explicit-root diagnostic
  with the syntax limitation in [COVERAGE.md](COVERAGE.md); a store, automatic
  index repair and native enforcement remain missing.
- [Specialist playbooks](skills/specialist-journeys/playbooks.md): inspected
  manual design/visual, troubleshooting/security, bootstrap, scouting/provenance
  and retrospective/promotion workflows. Their source entrypoints were read for
  system-atlas, UI design/critic guidance, troubleshooting/security audits,
  project setup, scouting, retro, instinct promotion and memory promotion; the
  playbooks are manual, do not bundle their tools, search services, rendering or
  approval enforcement, and `scripts/route.py` still refuses these entrypoints as
  inventory-needed rather than routing them automatically.

## Inventory-needed and excluded boundary

Other memory entrypoints and any uninspected specialist instructions remain
inventory-needed; do not infer their detailed behavior from a generic family
description. Staged research project/synthesis entries remain experimental,
not active implemented routes. Source-inspected is distinct from runtime-ported.

Disabled/retired entries are provenance, not missing public ports. Private accounts,
models, host configuration, schedules, ledgers, personal writing data, and proprietary
or third-party instructions are excluded.

The deterministic [system journey](examples/system-journey/README.md) connects a
happy route and refusal states to a fresh-process record. Selective assets remain the
safe adoption unit; whole-system runtime parity remains open.
