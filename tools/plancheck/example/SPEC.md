# Recipe Box — Spec

Implementable spec for [STRATEGY.md](STRATEGY.md). Progress in [TRACKER.md](TRACKER.md).

**Walking skeleton first.** S1 is the thinnest end-to-end slice (import one recipe, find it again). S2 hardens it and needs S1.

---

## Items

### S1 — Import and find one recipe

- What: a small search script (find.sh) that prints the recipe files whose text contains a search word
- Acceptance: searching for "lentil" lists exactly the lentil soup file
- Verify: `bash find.sh lentil`
- Lane: use — feeds: First-result
- Stakes: LOW
- Needs: none
- Touches: recipes/, find.sh
- Gate: auto

### S2 — Tag filter

- What: extend the search script with `--tag <name>` reading a `tags:` line in each recipe
- Acceptance: `--tag vegan` lists only recipes whose tags line contains "vegan"
- Verify: `bash find.sh --tag vegan`
- Lane: build
- Stakes: STANDARD
- Needs: S1
- Touches: find.sh, tests/
- Gate: auto
