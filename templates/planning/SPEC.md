# <Topic> — Spec

Implementable spec for [STRATEGY.md](STRATEGY.md) <T2: drop link, state the settled
why in one line>. Progress in [TRACKER.md](TRACKER.md).

Self-contained: an implementer dispatched with only this file must be able to build
it — inline the conventions, paths, and house patterns it needs.

**Walking skeleton first.** The first ready wave is the thinnest end-to-end slice: input → a
result the owner reads or uses (crude is fine). It includes ≥1 `Lane: use` item; if none can be ready,
say why in one line here. Hardening items `Needs:` the skeleton — never the reverse. Infra gaps
found later become `Lane: build` items in THIS pack, not a new pack (unless they serve ≥2 repos).

---

## Items

### S1 — <name>

- What: <the change, concretely — files, structure, behavior>
- Acceptance: <observable criterion — what must be true when done>
- Verify: `<command that proves it>`
- Lane: use | build <use = produces/uses the Outcome (an analysis, a post, the owner using the thing);
  build = platform, hardening, tooling. Absent = build. A use item names the Outcome/First-result
  step its output feeds ("feeds: First-result"); plancheck counts use items without one.
  exec-pack keeps ≥1 ready use item in every wave and breaks ties by shortest path to an open
  First-result>
- Stakes: LOW | STANDARD | HIGH | CONSTITUTIONAL <review depth per `rules/stakes-dial.md`.
  Absent = STANDARD; `Lane: use` and exploratory items default LOW (deterministic checks only)
  UNLESS the item falls in a HIGH class row (live install, cross-dir write, outward-facing post,
  money, irreversible) — the class table wins over the lane default>
- Needs: S1, S3 <hard ordering edge — this item consumes those items' outputs; absent =
  exec-pack serializes the item (it never guesses independence) — declare edges
  explicitly on T2+ items. `Needs: soft S2` = run on whatever exists now; orders, never blocks
  (typical for use items reading data while hardening continues)>
- Box: <n> sessions|days — started YYYY-MM-DD <optional budget for build items/chains. On expiry
  only three moves are legal: good-enough (done with known gaps) · descope · park. Never re-plan.
  Good-enough is NOT legal while a SEV1/SEV2 finding is open on a HIGH item — descope or park>
- Lineage: Sn <repair items name the item they repair (mandatory for `S<n><letter>` ids and
  fix/repair items — plancheck flags omissions); fix rounds count per lineage with the root
  item's Stakes budget (STANDARD 1, HIGH 2), then a decision row — never another repair item>
- Touches: <path-prefix>, <path-prefix> <declared write scope — literal path prefixes
  only, no globs; item may not write outside it; mandatory for T2+ items>
- Gate: auto | human — <reason> <start gate; ABSENT = auto (builds once Needs: are done). `human`
  only for strategic items: direction/scope · money · external party / public push · product taste ·
  unrecoverable data loss. The reason text must contain one of: scope · direction · money ·
  external · publication · taste · unrecoverable (plancheck checks keywords, not intent).
  Installs and merges are not intrinsically human-gated; they are `auto` plus the required
  verification and fresh-context reviewer. Governs starting only — review depth stays with Stakes:>
- Blocked-by: Qn <only if an open question in QUESTIONS.md gates this item — blocked
  items are not built; implementers append `Deviation:` lines here when the build
  diverges from What>
- Assumption: <default chosen> — basis: measured|inferred|guess; reversible by <what>;
  wrong if <signal> <a documented default instead of a question; a `guess` on an
  irreversible default must be a Qn instead>

### S2 — <name>

- What:
- Acceptance:
- Verify: `<command>`

<Split rule: extract SPEC-<workstream>.md only when that workstream is independently
buildable AND verifiable; the split spec must stand alone as an implementer brief.>
