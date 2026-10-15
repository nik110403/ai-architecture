# Specialist journey cards

## Research and empirical audit
**Input:** question or claim, source scope, quotes/citations, and for empirical work exhibits, scripts, and data vintages. **Maker:** researcher/auditor records provenance and a claim map. **Checker:** independent citation/quote verifier or break-the-code reviewer; a refuter tests one finding. **Done:** attributable notes and dispositions; missing source, retraction concern, lineage, execution, or checker blocks a release claim. **Dependency:** search, citation, and execution adapters. [research-kit](https://github.com/nik110403/research-kit) is an independent companion, not bundled; assess its installation and license separately.

## Design and visual artifacts
**Input:** product outcome, constraints, and accessible interaction requirements. **Maker:** one design builder records design decisions and renders/inspects the artifact. **Checker:** a separate design critic checks the rendered artifact, motion, and accessibility. **Done:** inspected artifact plus evidence. **Stop:** no renderer/browser/visual connector means no visual-inspection claim. UI lifecycle sources without a tool port are no-port, not failed implementations.

## Browser and external action
**Input:** observed external state and proposed act. **Maker/operator:** survey, then write a proposal. **Checker:** independently compare proposal to state. **Approval:** bind approval to that state before an authorized deterministic act; then preserve a receipt. **Stop:** absent approval support, state mismatch, takeover prompt, or browser adapter stops before submission. This collection neither grants permission nor automates browser actions.

## Writing and document formats
**Input:** purpose, register, facts, sources, requested format, and existing artifact identity. **Maker:** writer/editor preserves numbers, quotations, causality, and stated choices. **Checker:** proportionate fidelity review plus rendered/package/formula inspection. **Stop:** unknown facts stay unknown; absent format/cloud provider stops; never recreate a cloud artifact blindly. Personal calibration, samples, and engines are excluded. Word, PDF, spreadsheet, presentation, and cloud connectors are selective external dependencies.

## Coding, debugging, security, and domain quality
**Input:** decided change or reproduced defect, acceptance tests, and domain scenarios. **Maker:** builder reuses existing facilities and records checks. **Checker:** fresh verifier tests behavior; security reviewer audits existing risk or reviews new safeguards; domain reviewer compares terms and scenarios. **Stop:** undecided architecture/meaning, unproved root cause, or unreviewed material risk returns to decision. **Dependency:** test execution and specialist reviewers; these roles are guidance, not enforcement.

## Design and visual lifecycle
**Trigger:** a request to create, replace, refine, audit, or explain an interface or architecture visualization. This is a **generic manual workflow**, not a UI runtime, renderer, browser connector, or native role activation.

**Inputs:** outcome and audience; incumbent artifact and constraints; interaction, accessibility, responsive, and brand requirements; available renderer or screenshot evidence; and the repository's documentation policy for an architecture map.

**Ordered decisions and roles:**
1. A coordinator classifies the surface: a new expressive surface, a task-oriented product surface, a narrow refinement, an accessibility review, or an architecture explanation. If the desired outcome is unclear, resolve that before changing a visual system.
2. A single maker records the chosen direction and the evidence that constrains it. Preserve an incumbent identity for refinement; treat a confirmed redesign as a replacement direction rather than mixing old and new indiscriminately.
3. The maker produces the smallest inspectable artifact and checks the relevant states, including error, empty, keyboard, touch, responsive, and reduced-motion states where applicable.
4. An independent checker inspects the rendered result before automated or checklist-oriented signals, then records accessibility and interaction findings separately. The checker does not silently repair the maker's work.
5. For an architecture atlas, keep one editable structured source, generate all derivative views from it, give questions stable identifiers, and update every derived view together.

**Output artifacts:** direction brief; constraint or token record when a reusable system is needed; inspectable render or screenshot set; independent review findings; for an atlas, one source record, generated text view, question register, and glossary/decision records as appropriate.

**Bounded review and repair:** make one batched inspection pass, repair the confirmed findings in one bounded batch, then perform at most one confirmation pass. Stop rather than entering open-ended aesthetic polishing. Reopen only for a newly reported defect, changed requirement, or failed confirmation.

**Failure and fallback:** no renderer, browser, device, or accessibility test environment means report an inspection GAP rather than claiming visual or operational quality. Missing direction, product context, or documentation policy blocks the affected decision. A missing native design tool is not permission to invent a substitute workflow.

**Selective dependencies:** renderer or browser only for visual claims; accessibility inspection support only for operability claims; a structured-data generator only for generated atlas views. None is bundled or activated by this collection.

**Worked artifact example:** a settings-review record says “preserve the existing task-oriented layout,” lists three keyboard paths and two viewport screenshots, and includes a checker table with `state | evidence | disposition`. A linked `architecture-data` record contains `Q-auth-1: open`; its generated text view repeats that identifier rather than restating an answer.

## Troubleshooting and security review
**Trigger:** an observed recurring system symptom, or a request to assess a codebase for exploitable security weaknesses. These are separate journeys: a code defect belongs to ordinary debugging; a suspected live security incident stops for responsible escalation.

**Inputs:** timestamped symptom or target boundary; exact errors, measurements, history, and success threshold for troubleshooting; or target, scope, deployment assumptions, prior findings, and permission boundary for security review.

**Ordered decisions and roles:**
1. A diagnostician captures observable facts and defines what recovery would mean before choosing a fix. It first checks relevant local records and authoritative upstream information for a matching, bounded remedy.
2. If one remedy has a present distinguishing precondition, the operator may prepare it. Otherwise, a coordinator compares a small set of candidates by evidence, risk, prerequisite, rollback, and verification metric; independent reviewers rank the first safe experiment.
3. Apply one reversible change at a time, recheck its prerequisite immediately before action, and compare the original metric afterward. Two failed remedies return to fact gathering rather than escalating edits.
4. For a security audit, a mapper identifies assets, trust boundaries, input surfaces, and prior coverage. Focused hunters propose concrete attack scenarios; independent validators try to disprove and deduplicate them.
5. A reporter distinguishes confirmed exploit, deployment-testing requirement, and hardening note. Severity reflects both attacker access/likelihood and demonstrated impact; a missing secondary control is not itself a vulnerability when another control blocks the attack.

**Output artifacts:** fact snapshot; success metric; candidate/rollback table; verification record; reusable incident note. Security work additionally produces an architecture map, finding detail with attack scenario and evidence, human report, and structured findings validated against its schema where one exists.

**Bounded review and repair:** troubleshoot one change per measurement and stop after two unsuccessful changes. Security review validates every candidate independently once; unresolved environmental claims remain explicitly unconfirmed rather than cycling through speculation.

**Failure and fallback:** missing historical evidence yields an unknown cause, not a dismissal of a recurring symptom. Missing safe rollback, precondition, execution authority, or verification environment blocks application. Missing a local build or deployment environment downgrades a security claim to “requires deployment testing”; no concrete attack scenario means no vulnerability finding.

**Selective dependencies:** system measurements and logs for diagnosis; source/build/test environment for dynamic confirmation; independent reviewers for disputed remedies or findings. This collection supplies neither privileged access nor a security-testing runtime.

**Worked artifact example:** an incident-options record lists a rising latency metric, a recovery threshold, two candidate changes, each prerequisite, rollback, and result. A finding-014 record names an attacker capability, request path, observed outcome, impact, and status `requires-deployment-testing`, so it cannot be counted as confirmed.

## Bootstrap a project workspace
**Trigger:** a request to initialize a new project workspace or repair an incomplete scaffold. This playbook describes a manual selection and verification process; it does not create directories, install tools, register projects, or activate a template.

**Inputs:** project root; stated purpose and domain; existing instructions and configuration; template candidates; unresolved scope facts; and the owner-approved registration destination if registration is in scope.

**Ordered decisions and roles:**
1. An initializer inventories the root and determines whether it is an unconfigured project, a complete existing project, or a partially configured workspace that needs additive repair.
2. Classify from the stated intent before asking questions. Ask only for facts that would select a different template or make a durable instruction false.
3. Select the smallest matching template. Preserve existing substantial project instructions; do not overwrite them merely because a template is available.
4. Apply the template and replace only facts the owner supplied. Leave unresolved placeholders visible and mark dependent setup as incomplete.
5. A verifier walks the intended task paths, checks that the selected instructions and required files are present, and confirms registration only if the actual registration action and destination are in scope.

**Output artifacts:** setup classification; template decision record; project instruction file(s); unresolved-facts list; optional registration receipt; and a verification walk-through.

**Bounded review and repair:** one inventory/classification pass and one post-setup walk-through. Repair is additive unless the owner explicitly authorizes replacement; stop after surfacing a conflict rather than guessing an architecture or project thesis.

**Failure and fallback:** existing substantial instructions stop a blind bootstrap. Missing template, registration authority, destination policy, or essential project facts produces a bounded partial scaffold and GAP list. Do not call a project complete while material placeholders or required dependencies remain unresolved.

**Selective dependencies:** local template store, optional registry, and a project-specific verifier. Their availability and licenses must be assessed separately.

**Worked artifact example:** a setup record says `classification: partial existing workspace`, `template: analysis`, `preserved: project instructions`, `unresolved: data owner`, and `verification: task-routing walk passed; registration not requested`.

## Scout a reusable workflow and preserve provenance
**Trigger:** before proposing a new reusable workflow, or when evaluating an existing local, marketplace, or remote candidate.

**Inputs:** intended job; trigger conditions; relevant tools/data; three to five search terms and synonyms; candidate locations; and adoption constraints such as license, maintenance, permitted actions, and security review requirements.

**Ordered decisions and roles:**
1. A scout writes a narrow intent statement and checkable completion criteria before searching.
2. Search local installed material first, then available catalogs, then remote sources. Label each result by observed state—usable locally, disabled/unavailable, catalog-only, or remote—rather than treating names as installed capabilities.
3. Read a candidate's instructions and metadata before recommending it. Record unexpected writes, commands, network access, credential handling, installations, license, and maintenance evidence.
4. A reviewer checks the candidate's action surface and provenance evidence. Rank a bounded list by fit, observed availability, maintenance, and risk.
5. Present use-existing, fork/extend, and create-fresh options. Adoption or creation remains a separate authorized change; a search result is not an activation.

**Output artifacts:** search terms; result table with state and provenance; candidate risk notes; ranked options; and a declared decision or explicit no-match result.

**Bounded review and repair:** cap the candidate list and targeted remote searches; review each shortlisted candidate once before ranking. Stop after a ranked decision set rather than expanding weak matches indefinitely.

**Failure and fallback:** unavailable local classification, unreadable candidate instructions, unknown license, or unreviewed high-risk action surface prevents an adoption recommendation. If no close match exists, record that evidence and offer a fresh workflow only after the owner chooses it.

**Selective dependencies:** local indexes, catalog search, remote search, and optional static inspection tools. This collection provides no marketplace client, network access, installation, or trust guarantee.

**Worked artifact example:** a workflow-scout record has rows `local-note | catalog-only | trigger match | unavailable`, `remote-a | remote | partial match | writes configuration`, and a final decision: `do not adopt until license and action review are complete`.

## Retrospective and evidence-backed promotion
**Trigger:** an explicit request to review one selected work session, promote a repeated behavior, or preserve a durable observation. This is a manual evidence workflow, not an observer, memory system, or automatic rule-writing mechanism.

**Inputs:** selected session or observation identifiers; event evidence; outcome evidence; candidate target; recurrence history; and the owner-approved durable destination, if any.

**Ordered decisions and roles:**
1. A retrospective reviewer identifies the exact session and evidence. Missing session evidence stays `UNKNOWN`; it is not reconstructed from a vague memory.
2. Record at most a small, ranked set of candidates. Each includes event reference, observed defect or hypothesis, category, proposed local target, checkable completion criterion, and evidence status.
3. A promoter groups only independent repeated occurrences. Discard singletons; label each candidate's outcome and record the measured before/after delta or leave it unpromotable.
4. Before proposing a new reusable workflow, run the scout journey to find an existing match. Choose between reuse, extension, a local convention, or no promotion.
5. An owner reviews the evidence and target before any durable write. A separate recorder checks duplicates and superseded records, records provenance, updates an index mechanically if one exists, and logs the decision.

**Output artifacts:** retrospective candidate table; recurrence and outcome table; scout result; proposed target and provenance; approval record; duplicate/supersession decision; index update receipt; and promotion audit entry.

**Bounded review and repair:** retrospective output is candidates only. Promotion requires recurrence, outcome evidence, measured delta, deduplication, a checkable rule, and explicit approval. One ordinary local fix may be proposed, but it never proves a generalized rule.

**Failure and fallback:** no selected session, one occurrence, unclear outcome, no measurable delta, duplicate record, uncheckable lesson, missing approval, or unavailable durable store stops promotion. Preserve the candidate as unpromoted evidence or report the adapter GAP; do not create a hook, observer, or automatic write as a fallback.

**Selective dependencies:** session/observation store for evidence; scout/search support for deduplication; durable memory store and index only after approval. No such adapter is bundled or activated here.

**Worked artifact example:** a promotion-candidates record holds `P-02 | three independent occurrences | worked_after_correction(1) | setup time 40→25 minutes | rule is checkable`. Its disposition remains `unpromoted—awaiting owner approval and duplicate check`.
