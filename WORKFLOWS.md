# Workflows

These are portable process contracts written up from the workflows I use. The
stdlib router reports a conservative route from explicit input only; it is not
executable automation. The active harness decides available models, tools,
permissions, isolation, scheduling, and persistence. In every journey, a missing
provider or unavailable independent checker is a stop/disclosure condition, not a
reason to invent a successful run.

## Shared spine

`context → route → bounded brief → work → independent check → saved Next → learning`

1. Start from a bounded context card, source set, or artifact; state what is unknown.
2. Route by task kind and stakes. Pick roles by capability, not label; record any
   missing tool or replacement decision.
3. Write a bounded brief with objective, inputs, acceptance criteria, allowed scope,
   evidence expected, and stop conditions. Do not make workers rediscover the plan.
4. The maker produces the requested artifact and its evidence. A checker who did not
   make it checks the frozen artifact against the brief. Refutation tests one finding
   at a time; it does not arbitrate the whole project.
5. Save the next exact action, pending jobs, result locations, and unresolved risk.
    Record outcomes and unknown cost/usage for later maintenance or retrospective.

`scripts/route.py` reports route, required context, roles, stable capability IDs,
ordered checks, available/missing adapters, stop/approval conditions, and fallback.
Unknown or mixed intent, entry points with no tool included, missing required tools,
missing checker model roles, exhausted review budget, pending jobs, and unknown arrivals
refuse a COMPLETE route. External action stays CONDITIONAL even with reported typed
state-bound approval metadata: the active harness remains the authority. Manual mode
can describe work but never claims native or enforced operation. It executes none of
its inputs and does not discover providers, install tools, call a network, or dispatch.

Use [worker brief](templates/worker-brief.md), [worker result](templates/worker-result.md),
[review](templates/review.md), [resume](templates/resume.md), and
[measurement](templates/measurement.md) as portable records. The
[parser worked example](examples/worked-example/README.md) is a concrete maker →
fresh-checker → Resume fixture. It is authored evidence, not an agent transcript.

The selective evaluation `skills/evaluation/scripts/receipt_report.py` reads only caller-supplied v1
receipt JSONL files in order and reports known, partial, missing, and explicitly
excluded token observations without inventing totals. It is local accounting only:
it neither finds inputs nor calls providers, joins checker rates, selects/ranks models,
or establishes an evaluation verdict. The separate
[checker-outcome report](skills/evaluation/evaluation-report.md) joins explicit
charge IDs using the latest known-time checker verdict, with unknowns and tie
rules disclosed; it neither supplies telemetry nor ranks models.

The [manual context manifest](skills/context/context-packet.md) compiles a project's
explicit routing table to JSON with admitted paths, missing paths and warn-only
size estimates. It has no verified downstream runtime consumer or cache control.

## Coding journey

**Route:** a decided, non-UI change goes to a bounded builder; interface work needs a
separate design decision and critic. **Work:** choose the least new code that meets
the behavior, reproduce a bug before treating it as fixed, and capture test/build
output. **Check:** a fresh verifier examines the specified behavior and evidence;
security/domain concerns get their own narrow review rather than a maker self-check.
**Failure:** if the interface, architecture, or meaning is undecided, stop and return
it to planning; if checks fail, repair within the agreed budget or park the issue.
**Next:** record the exact remaining check or integration action.

Relevant assets: [planning](skills/planning/SKILL.md),
[execution](skills/execution/SKILL.md), [delegation](skills/delegation/SKILL.md),
[review](skills/review/SKILL.md), [verification rule](rules/verification.md), and
[builder/verifier](CATALOG.md#roles). The collection supplies no native dispatch,
test runner, security gate, domain checker, or convergence budget.

## Research and analysis journey

**Route:** first decide whether the output is a bounded source set, an empirical
claim, or a paper/build artifact. **Work:** retain the question, source scope,
provenance, notes, quotations, and uncertainty; for empirical work map claims to
exhibits, scripts, and data vintages. **Check:** independently verify citations or
quotes where tooling exists; run a fresh break-the-code/refuter pass for a concrete
analysis finding. **Failure:** a degraded provider, missing source, unsupported
claim, or unchecked result stays explicit and blocks a release verdict.

Use [research](skills/research/SKILL.md) and its [source note](skills/research/source-note.md)
for the local bounded layer. For executable literature, quote, pipeline, and paper
tools, adopt the independent [research-kit](https://github.com/nik110403/research-kit)
deliberately; it is not installed by this collection.

## UI and visual-output journey

**Route:** distinguish product UI from a slide, infographic, plan, or change recap.
**Work:** establish product/design decisions before building; one maker owns a bounded
visual pass, then render or inspect the actual artifact. **Check:** a separate critic
reviews the artifact, accessibility, and motion/interaction claims against the brief.
**Failure:** unavailable renderer, browser, or connector is a tool gap; do not claim
visual inspection from source text alone. **Next:** save the concrete revision or
missing-adapter action.

[design](skills/design/SKILL.md) and [design-critic](agents/design-critic.md) are the
available building blocks. No renderer, browser, slide, infographic, recap, or
system-atlas adapter is included. The
[specialist playbooks](skills/specialist-journeys/playbooks.md) now include a
written-up design/visual lifecycle; the process translation does not bundle
its renderer, visual generator, browser or native agents.

## Knowledge and maintenance journey

**Route:** classify the request as continuity, memory/instruction health, routine
upkeep, scouting/provenance, or a retrospective. **Work:** use a bounded inventory;
make changes only in declared scope; preserve a durable Next and pending-job state.
**Check:** run deterministic checks where available and read back persisted state.
**Failure:** ambiguous source, failed receipt, unknown pending job, or missing
scheduler means no readiness claim. **Learning:** promote only repeated,
evidence-backed candidates after review; never automatically rewrite rules.

The optional [continuity journal](examples/continuity/README.md) demonstrates a
fixed, temporary-directory handoff only. [lifecycle](skills/lifecycle/SKILL.md)
requires pending/unknown arrivals to remain NOT_READY; [discovery](skills/discovery/SKILL.md)
and [operations](skills/operations/SKILL.md) provide bounded inventory and report
processes. The selective `skills/maintenance/scripts/loop_check.py` reads only explicit
registry and adapter JSON snapshots; it classifies declared wiring but never probes a
scheduler, service, hook, home, or environment. `complete` means no unavailable declared
adapter in that snapshot, not health, freshness, or live proof. The optional
[snapshot consistency checker](skills/continuity/continuity-check.md) compares
explicit tracker bytes and caller-declared receipts, not actual native readiness.
The memory reporter remains a limited explicit-root diagnostic; job ledgers,
native scheduling and automatic repair are not included. Written-up
[specialist playbooks](skills/specialist-journeys/playbooks.md) add manual bootstrap,
scouting, troubleshooting/security and retrospective/promotion decisions without
activating their native tools.

## Writing and document journey

**Route:** choose a register and actual target format before drafting or editing.
**Work:** preserve facts, source limits, numbers, quotations, causal direction, and
explicit author choices; choose a format-specific editor only when it exists.
**Check:** use a proportionate fidelity review and inspect rendered documents,
packages, or spreadsheet formulas as applicable. **Failure:** absent connector or
format provider stops the act; never recreate an existing cloud document blindly.
**Boundary:** no automatic sending, publishing, training, or personal-voice export.

Generic writing and document contracts are mapped in [CAPABILITIES.md](CAPABILITIES.md).
Personal calibration, format connectors, and proprietary integrations are excluded.

## External-action journey

**Route:** pick the lowest-risk mode. **Work:** survey the untrusted external state,
write a proposal, obtain an independent review and approval bound to that state, then
perform the deterministic authorized action and save a receipt. **Check:** compare
the resulting state to the approved proposal. **Failure:** account takeover prompt,
state mismatch, missing browser/connector, or ambiguous approval stops the journey
before submission.

[action boundary](skills/safety/action-boundary.md) supplies the local proposal/check
record. It is not browser automation, a permission grant, or a submission ledger.

## Reproducible local journey

[examples/system-journey/](examples/system-journey/) deterministically exercises
task → route → bounded brief → fixture result → independent check → persisted Next and
fresh-process recovery. It additionally shows a refusal for an entry point with no tool included and a
pending-job refusal. The fixture is authored evidence, not an AI-performance claim;
its saved Next is data and is never executed.

[Copied-recipient adoption](examples/adoption/README.md) runs selectively copied
offline tools in a fresh temporary project and demonstrates only a fixed, literal
Next. Its inputs are authored fixtures, not a recipient's real project or an
AI builder/checker transcript.

## Where the rules are enforced

Most of the steps above are instructions an agent follows. Three pieces make some of them real, and only if
you install them: the [gates plugin](plugins/gates/README.md) (hooks that block destructive commands and
unreviewed or unrecorded stops), the [OpenCode bridge](opencode/README.md) (the same hooks inside OpenCode)
and opt-in [model rotation](opencode/rotation/README.md). [INSTALL.md](INSTALL.md) says how to turn each on and
off. Everything else here stays described.

## What this does not prove

This collection describes the workflows I use and adds
specialist process playbooks, and a deterministic local journey, plus the opt-in guard hooks above. It does not prove
native enforcement of the described workflows, automatic routing, agent/platform behavior, model selection, external
acts, or that the whole private setup is reproduced here. Follow the selective installation guidance in
[INSTALL.md](INSTALL.md), run its offline checks, and treat anything not included as
adoption work you do yourself.
