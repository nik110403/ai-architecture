# Catalog

| Component | Use it when | Local asset |
| --- | --- | --- |
| [planning](skills/planning/SKILL.md) | turning an outcome into bounded work | [plan-template.md](skills/planning/plan-template.md) |
| [execution](skills/execution/SKILL.md) | carrying out a decided work item | [work-item.md](skills/execution/work-item.md) |
| [delegation](skills/delegation/SKILL.md) | briefing a bounded worker | [brief.md](skills/delegation/brief.md) |
| [context](skills/context/SKILL.md) | reducing stale or excessive context | [context-card.md](skills/context/context-card.md) |
| [continuity](skills/continuity/SKILL.md) | preserving a handoff and next step | [resume.md](skills/continuity/resume.md), [optional journal](skills/continuity/scripts/journal.py) |
| [review](skills/review/SKILL.md) | independently checking an artifact | [review.md](skills/review/review.md) |
| [evaluation](skills/evaluation/SKILL.md) | defining measurable checks | [measurement.md](skills/evaluation/measurement.md) |
| [workspace](skills/workspace/SKILL.md) | keeping scoped changes isolated | [workspace-checklist.md](skills/workspace/workspace-checklist.md) |
| [design](skills/design/SKILL.md) | recording interface decisions before UI work | [design-brief.md](skills/design/design-brief.md) |
| [research](skills/research/SKILL.md) | source-bounded research | [source-note.md](skills/research/source-note.md) |
| [safety](skills/safety/SKILL.md) | checking action boundaries | [action-boundary.md](skills/safety/action-boundary.md) |
| [maintenance](skills/maintenance/SKILL.md) | routine upkeep with evidence | [maintenance.md](skills/maintenance/maintenance.md) |
| [routing](skills/routing/SKILL.md) | reporting a conservative route | [route-report.md](templates/route-report.md) |
| [convergence](skills/convergence/SKILL.md) | bounded multi-lens review | [panel-review.md](templates/panel-review.md) |
| [lifecycle](skills/lifecycle/SKILL.md) | pending results and safe handoff | [lifecycle-record.md](templates/lifecycle-record.md) |
| [discovery](skills/discovery/SKILL.md) | instructions, memory, and project inventory | [report-only memory check](skills/discovery/scripts/memory_check.py) |
| [operations](skills/operations/SKILL.md) | drift, loops, scout, retro, promotion | local process only |
| [specialist-journeys](skills/specialist-journeys/SKILL.md) | dependency-explicit specialist work | [playbooks.md](skills/specialist-journeys/playbooks.md) |
| [plan-pack](skills/plan-pack/SKILL.md) | writing a strategy, spec and tracker for bigger work | [templates](templates/), `tools/plancheck/` |
| [exec-pack](skills/exec-pack/SKILL.md) | running a spec's items as parallel waves of workers | `tools/gate-check/` |
| [tribunal](skills/tribunal/SKILL.md) | sealed multi-judge review of a high-stakes change | `tools/tribunal-scribe/` |

## Roles

- [builder](agents/builder.md)
- [implementer](agents/implementer/implementer.md)
- [verifier](agents/verifier/verifier.md)
- [tribune](agents/tribune/tribune.md)
- [refuter](agents/refuter/refuter.md)
- [researcher](agents/researcher.md)
- [security-reviewer](agents/security-reviewer.md)
- [design-critic](agents/design-critic.md)

## Commands

- [plan](commands/plan.md): `/ai-architecture:plan`
- [execute](commands/execute.md): `/ai-architecture:execute`
- [review](commands/review.md): `/ai-architecture:review`
- [checkpoint](commands/checkpoint.md): `/ai-architecture:checkpoint`
- [research](commands/research.md): `/ai-architecture:research`
- [health](commands/health.md): `/ai-architecture:health`

The optional journal is a local companion utility, not required for the continuity skill. A separate companion research-kit may supply domain-specific research tooling; this collection's research skill remains usable without it.

For the broader operating-model map, statuses, and unported dependencies, see
[CAPABILITIES.md](CAPABILITIES.md) and [WORKFLOWS.md](WORKFLOWS.md). This catalog has
21 skills, 8 roles, and 6 commands. Router reports do not create native enforcement.
