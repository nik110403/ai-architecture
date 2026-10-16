# Review-depth stakes dial

Governs how much review any artifact gets. Stakes are assigned when the work is specified, so review depth is a plan property, not a mood.

## Classes

| Stakes | What falls in it | Review owed |
|---|---|---|
| **LOW** | docs, report-only artifacts, read-only scripts, staged files with no live consumer yet; `Lane: use` diagnostics | deterministic checks only (the linter, a syntax check, the item's own tests); no per-item model review. LOW items merging into a shared branch share **one** batched verifier per wave |
| **STANDARD** | internal tooling, test harnesses, scripts without live wiring | **ONE** review pass (a verifier or a single reviewer); sub-HIGH findings are logged, **not round-tripped**; at most **one** fix round |
| **HIGH** | live installs (hooks, settings, live agent or skill definitions), writes outside the working repository, the named high-stakes diff classes (crypto, migrations, concurrency, authorization, money paths, security boundaries), irreversible or outward-facing actions | full ladder: a security review and/or a tribunal (at least 3 judges, at least one from a different model family where available, discussion protocol - see the tribunal skill), a refuter on every SEV1/SEV2; at most two full rounds |
| **CONSTITUTIONAL** | changes to routing policy, standing permissions or the approval rules themselves | at least 3 lenses plus at least one judge from a different model family where available, discussion protocol, and arbitration independent of the author where disputed |

## Rules

1. Stakes are assigned at spec time - every SPEC item carries a `Stakes:` line. **Absent = STANDARD.** `Lane: use` items and exploratory or diagnostic work default to **LOW** - the outcome lane must stay cheap enough to run every wave - **unless** the item falls in a HIGH row above (live install, outward-facing post, money, irreversible): the class table always wins over the lane default.
2. The **stage** of an artifact sets its class, not its destiny: a staged hook is STANDARD to build and HIGH to install; the install step carries the HIGH review, exactly once.
3. One fix round per review pass, at most. Rounds count per **lineage**, not per item id: repair items carry `Lineage: Sn` and share Sn's budget (S6a through S6f are one budget; budget = the root item's allowance: STANDARD 1 round, HIGH 2). Budget spent means a decision row (good-enough-with-gaps / descope / park), never another repair item. Good-enough is not legal while a SEV1/SEV2 finding is open on a HIGH item - only descope or park. A second **full** round on the same artifact needs a named reason in the tracker decision log; later rounds target only changed surfaces.
4. Sub-HIGH findings from any pass are recorded verbatim (tracker or fix file), fixed opportunistically or never - no dedicated round-trip.
5. Escalate on signal, never demote mid-review: a STANDARD item found to touch a HIGH class is re-classed upward and the extra review happens before its gate.
6. `SOUND` is a successful verdict - reviewers document serious refutation attempts, not manufactured findings.
7. **Plans are not artifacts to harden.** A plan or spec revision gets a tribunal only when the pack is HIGH **and** money or irreversibility is at stake; otherwise one tribune seat or none. Repeated tribunals on one plan produce a plan, not the thing.

## Where it is used

`skills/plan-pack/SKILL.md` (the `Stakes:` field), `skills/exec-pack/SKILL.md` (ceremony per wave) and `skills/tribunal/SKILL.md` (panel sizing). A review-depth rule that is not written into a plan item is not enforced by anything in this collection.
