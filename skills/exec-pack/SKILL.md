---
name: exec-pack
description: Execute a plan pack - "execute the pack", "implement the next phase", "build it" - whenever a plans/<topic>/ directory with an active tracker is the plan of record, or when plan-pack has just finished writing one. Compiles the needs/touches graph into waves of implementer plus verifier runs, merges them one at a time and closes each wave into the tracker.
---

## When this applies

Use to execute a plan pack: "execute the pack/plan/spec", "implement the next phase", "build it" - whenever a `plans/<topic>/` directory with a `Status: active` tracker is the plan of record. It compiles the pack's `Needs:` / `Touches:` graph into waves of implementer plus verifier runs (worktree-isolated where your harness supports it), merges them in the orchestrator, and closes each wave back into the tracker. Not for writing the plan itself (see the plan-pack skill), not for whole-codebase audits, and not for plans that are not packs - a plan with no tracker and no SPEC items is executed task by task with whatever subagent process you already use.

# exec-pack - plan-pack's execution-side mirror

plan-pack writes the pack; exec-pack runs it. It is a thin **compiler**, not an executor: it turns the pack's dependency graph into waves over the agent bench (implementer, verifier), then owns the merge and tracker bookkeeping. Serial is the degenerate case - a fully ordered spec produces waves of size 1.

**Status of this port (read first).** The pack format, the wave logic, the brief and report contracts and the tracker bookkeeping below are plain procedure and can be followed by hand or by any orchestrator. The parallel path assumes a harness that offers a Workflow-style tool (a script that fans out subagents in waves, isolates each in a worktree and journals the run); on such a harness, use it. When the harness has no such tool, use the **sequential fallback** in section 3. Both paths are UNVERIFIED outside the author's own setup. Unattended runners, run ledgers and morning reports used by the original setup are not included.

State the trade honestly when kicking off: **parallelism buys wall-clock, not tokens.** N parallel implementers plus N verifiers plus merged-tree verification after each merge spend more total tokens than serial work, compressed into less clock time. Fine for launch-and-walk-away runs; not a savings move.

## 1. Kickoff (orchestrator)

- **Trigger without asking** when plan-pack just wrote the pack or the Resume `Next:` names a `Gate: auto` item. Never end a turn on "shall I execute?".
- Read the tracker (done rows drop out of the graph) and every SPEC file of the pack.
- **Outcome first:** a STRATEGY `Trigger:` that is `open` and past its date runs before anything else (it can close the pack). `Needs: soft` edges order but never block. Every wave carries at least one ready `Lane: use` item while one exists; a wave of pure build work is legal only when no use item is ready, and the Resume block says why.
- The ready set excludes items with `Gate: human` or an open `Blocked-by:` (absent `Gate:` = `auto`). When only those remain, stop and name them in the Resume block - that is the one legal "waiting on the owner".
- Extract a **per-item brief file** for each undone item: the item text plus interfaces from its `Needs:` parents, at an **absolute path**. Never hand an implementer the whole SPEC. Worktrees only see committed files, so briefs must resolve regardless.
- Validate the graph. Refuse to start on: a `Needs:` cycle, a `Needs:` reference to an item id absent from the pack, or overlapping `Touches:` prefixes between two items with no `Needs:` edge between them. Items missing `Needs:` / `Touches:` entirely are serialized (never guess independence). `tools/plancheck/plancheck` reports the dangling and overlapping cases.
- Assess provisioning cost: docs-and-script repos parallelize for free; dependency-heavy repos (per-worktree installs) get smaller waves or serial execution in the main tree when provisioning costs more than the parallel gain.
- Check that each item's `Touches:` targets are COMMITTED: worktrees see only committed files. Uncommitted targets force the main-tree fallback (disjoint prefixes still mandatory; record pre-run file snapshots as BASE; the scope fence degrades to report- and mtime-based evidence).
- Restate the tracker's `Goal:` line as the completion condition for the run.

## 2. Compile (orchestrator)

- **Ceremony scales with `Stakes:`** (`rules/stakes-dial.md`; absent = STANDARD; use items default LOW unless a HIGH class row applies - the class table wins). LOW = implement plus the item's deterministic `Verify:`, plus one batched verifier per wave for all LOW items that merge. STANDARD = one verifier, at most one fix round. HIGH or CONSTITUTIONAL = the full chain below plus the named reviewers. Fix rounds count per `Lineage:` chain (budget = root item's Stakes: STANDARD 1, HIGH 2), not per item id; budget spent means a decision row (good-enough-with-gaps / descope / park), never a new repair item. An expired `Box:` allows the same three moves - never a re-plan. Good-enough is illegal while a SEV1/SEV2 finding is open on a HIGH item.
- One chain per ready item: `implement -> verify`.
  - Implementer stage: the implementer agent (or a UI-specialist builder if your setup has one and the item's `Touches:` are UI file classes). Worktree-isolated where possible. Brief = the item's brief file path, its `Touches:` as a hard fence, the report-file path, and a no-subagents contract.
  - Verify stage: the verifier agent (fresh context), reading the report's `Deviation:` lines first, checking acceptance plus the item's `Verify:` command, with the diff computed against a **recorded per-item BASE** as a file - plus the scope-fence check (a diff outside the declared `Touches:` is a finding).
- Wave barriers: a chain waits on every chain its item's `Needs:` names.
- Report contract: every chain ends **DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED** - never free text.
- Fix rounds: STANDARD allows at most 1, HIGH at most 2, counted across the root item's entire `Lineage:` chain, including integration and convergence repairs. CONSTITUTIONAL uses the HIGH cap plus its named reviewers; LOW follows its deterministic checks and batched review. A new item ID or stage never resets a spent budget. Still red at the cap means surface to the orchestrator for a decision row, not another attempt.
- Concurrency: soft cap **4** concurrent writers. Raising it is a deliberate per-run decision by the human owner, not a default.
- Tie-break when the ready set exceeds the cap: take items on the **shortest path to an undelivered `First-result:`** first (use items before the build items they do not need); then the longest remaining critical path; then items unblocking the most `Blocked-by:` questions.

## 3. Run

- **With a Workflow-style tool:** emit one script that declares the waves, the agent type per stage, worktree isolation and the report paths; run it. Its journal is the in-flight record. A dead or compacted session resumes from the journal plus the tracker's done rows and never re-dispatches finished work.
- **Sequential fallback (no such tool):** walk the ready set in tie-break order, one item at a time in the main tree or a worktree you create by hand. For each item: write the brief file, dispatch the implementer, wait for its report, dispatch the verifier, apply at most the remaining lineage fix rounds above, then merge, run merged-tree verification, and only on success flip the tracker row before starting the next item. This loses wall-clock parallelism but keeps every other rule (fences, reports, one-at-a-time merges, tracker bookkeeping). UNVERIFIED outside the author's setup.

## 4. Wave close (orchestrator - never a subagent)

- Merge the wave's branches **one at a time**, not batched. Run the repository's merged-tree verify **after each merge**, before the next merge or marking the item `done (merged <sha>)`; record the verified SHA and evidence. A worker's DONE report alone is not integrated completion.
- A red merged-tree verify, or a non-trivial merge conflict: keep the item explicitly **not done** (integration failed/conflicted). Dispatch a fresh integration-fixer implementer with the failure or conflict output only if the affected lineage has a remaining repair round; charge that round to its STANDARD 1 / HIGH 2 budget. The orchestrator does not resolve code conflicts itself. Re-run merged-tree verification after repair; at budget exhaustion, record the decision and park, descope or revert the failed integration before merging further work. Never mark a failed integration done.
- Merge `Deviation:` lines from each item's **report file** into the pack SPEC. Under parallel execution implementers write deviations to their report, not the shared SPEC (N branches editing one file is the collision the fences exist to prevent); the orchestrator does the merge.
- Overwrite the Resume block - first line `Delivered:` (a path the owner opens or the use item that produced it, or `none (date)`); flip `First-result:` to `delivered <date>` when a use item produced it; append rulings to the decision log; deferred minors get **one** tracker line each, rolled into the final review rather than fixed mid-wave.
- Loop to the next ready set until it is empty or every remaining item is `Blocked-by: Qn` or `Gate: human`.

## 5. Final

Packs with no HIGH or CONSTITUTIONAL item skip the convergence review: green per-merge verification plus every item verified per its Stakes is enough. Otherwise run a whole-branch **convergence review** on the strongest available model (add tribune seats for T3 packs; see the tribunal skill), manually. Its output is typed, not prose - one line per SPEC item: `CONVERGED | Sn` or `GAP | Sn | missing|partial|contradicts | evidence`, plus `UNREQUESTED | path | what changed` for tree changes no item's `What:` asks for (an intent judgment, distinct from the verifier's path-based Touches fence). Append-only: record gaps as tracker rows or a SPEC addendum, with repairs retaining their root `Lineage:` and remaining budget; a converged pack means zero writes. Allow at most one combined fix dispatch, only for lineages with budget remaining, then re-verify and at most one re-run of the convergence review. Unresolved findings get a good-enough-with-gaps / descope / park decision, never a fresh repair ID to reset the budget; good-enough remains illegal for an open SEV1/SEV2 on HIGH. Then merge into the shared branch only when the convergence review is clean (it is the reviewer), re-run the verify loop on that merged result, and record completion only on green. Red integration is explicitly not done; repair within the remaining lineage budget or revert.

## Steps

1. Read the tracker and SPEC files; check the ownership of the pack (no other executor active).
2. Validate the graph (`tools/plancheck/plancheck --dir plans/<topic>`), build the ready set, write brief files.
3. Dispatch the chains (Workflow tool or sequential fallback); collect typed reports.
4. Close each wave: merge one at a time, verify each merged tree, then update its tracker row; overwrite Resume.
5. Repeat until the ready set is empty; run the final convergence step if the pack has HIGH items.

## Output and done check

Done when every item is `done (merged <sha>)`, `dropped`, or explicitly blocked/gated in the Resume block; the merged-tree verify is green; Resume starts with `Delivered:`; and the report file of every chain ended in one of the four status words. Evidence to report: merged shas, the verify output tail, remaining gated items.

## Failure handling

- Graph invalid (cycle, dangling `Needs:`, overlapping `Touches:` with no edge): refuse to start, fix the pack first.
- Verify red after the fix budget: stop that item, write a decision row (good-enough, descope or park), keep unblocked items moving.
- Implementer reports NEEDS_CONTEXT or BLOCKED: read its `Deviation:` lines; an architectural divergence goes back to planning, not to a third attempt.
- Uncommitted `Touches:` targets or a harness without worktrees: fall back to serial main-tree execution with BASE snapshots.

## Worked example

Pack with S1 (use, no deps), S2 (build, `Needs: S1`), S3 (build, `Needs: S1`, disjoint `Touches:`). Wave 1 = S1 alone. After S1 merges, passes merged-tree verification and its row flips, wave 2 = S2 and S3 in parallel (or one after the other in the sequential fallback). Each worker chain ends DONE after its verifier agrees; merge S2, verify its merged tree, then flip its row; merge S3, verify again, then flip its row. A failed integration stays not done and uses only the lineage's remaining repair budget. Overwrite Resume with `Delivered:` pointing at what S1 produced, and stop when no ready work remains.
