---
name: discovery
description: Inspect project instructions, memory candidates, and project state without silently adopting them. Use before bootstrap, memory, or instruction changes.
---
# Discovery
## Objective
Distinguish observed project facts from unverified memory or instructions.
## Steps
1. Bound the project root and declared inputs; do not search homes, accounts, or global configuration.
2. Inventory instruction files, links, freshness, and conflicts using deterministic checks where available.
3. Classify each item as observed, stale, unread, excluded, or no-port.
4. Propose the smallest scoped repair; require an operator decision before applying policy changes.
5. Save sources, unknowns, and Next in a lifecycle record.
6. For a bounded Markdown memory bundle, run `python3 -B scripts/memory_check.py --root PATH` from this directory. The caller must supply `--root`; the utility never discovers a home, project, or index.
## Output and done check
Done is an auditable inventory and proposed disposition, not an automatic memory promotion or bootstrap. `scripts/memory_check.py` is report-only: it produces sorted drift, info, unsupported, and inaccessible findings, but does not repair an index or establish a health claim.
## Failure handling
Ambiguous project identity, broken links, or missing provenance blocks a health claim. An incomplete scan (malformed input, inaccessible entry, or symlink) is reported separately from drift, never as a clean result.
## Worked example
An unlinked instruction is reported with its location and no guessed replacement; an operator decides whether to repair it.

## Memory-check discovery grammar

The local bundle is [this skill's script](scripts/memory_check.py),
[fixture](fixtures/memory-check-clean/), [output template](memory-check-output.md), and
[dependency note](DEPENDENCIES.md). Copy those together for a standalone check.

`--root` must name an existing, non-symlink directory. Discovery recursively considers only
regular UTF-8 files whose relative POSIX name ends in .md; any path segment or filename
containing `sync-conflict` is excluded. It does not descend into symlink directories or open
symlink files. The one index is exactly MEMORY.md at the resolved root; other Markdown files
are indexed content. Supported index links are one-line bracketed-label, parenthesized-target
constructs whose target ends in .md:
the target has no whitespace or parentheses and may end in one optional fragment. Incomplete
or other Markdown-like `](` constructs are unsupported, not parsed as general Markdown.
Wikilinks are exactly `[[name]]`, where `name` is a same-directory
stem (no slash, fragment, or alias); they resolve to name.md.

Dates are literal valid `YYYY-MM-DD` tokens in content after optional YAML frontmatter and are
stale only when earlier than `--stale-before` (default `2026-04-15`, intentionally fixed for
reproducible reports). An index line is long above 120 Unicode characters. `--path-root PATH`
is optional and must resolve to the root itself or one of its descendants; it checks literal
absolute path references beginning at that configured root. Every discovered reference is
walked lexically from the resolved root with component-level `lstat`; a symlink component stops
the check before any descendant is inspected. References that lexically escape, or involve a
symlink, are unsupported rather than opened. These guards are report-tool boundaries, not a
sandbox or race-proof guarantee.

## Deliberate differences from the source-derived check

This portable port has one explicit root instead of a host-wide glob, no repair mode, no home
or provider assumptions, and a fixed stale cutoff rather than a moving six-month date. It
reports unavailable or malformed input as incomplete rather than treating a gap as clean.
