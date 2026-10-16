# memcheck

A zero-token linter for a folder of curated memory files (plain Markdown notes that an agent
loads as context). No LLM, no network. Status: runnable.

## Layout it expects

```
<root>/<slug>/memory/MEMORY.md     index, one line per note: dash, [Title], (file name), em dash, hook text
<root>/<slug>/memory/<name>     frontmatter (name, description, metadata.type) + body
```

## Usage

```
tools/memcheck/memcheck --root <root>                 # report
tools/memcheck/memcheck --root <root> --fix-index     # dry-run: propose index lines for orphans
tools/memcheck/memcheck --root <root> --fix-index --apply
tools/memcheck/memcheck --root <root> --fix-index --shorten [--apply]
    options: --dir <memory-dir>  --actor human|hook  --audit-log <file>
bash tools/memcheck/tests/run.sh                      # tests, fictional fixtures
```

## What it checks

1. Dangling `[[wikilinks]]` (informational).
2. Index mismatch: a file missing from the index, or an index link to a missing file (the
   only drift-class finding, marked with a stop sign).
3. Dates more than six months old (informational).
4. Isolated notes with no links in or out (informational).
5. Index lines over 120 characters (informational; `--fix-index --shorten` trims the hook text
   to 100 characters and keeps quoted strings, paths, negations and dates whole).

The scan always exits 0. Usage errors exit 2.

## Safety properties

- The trust root is only the `--root` flag; no environment variable can change it or the audit log.
- `--dir` must resolve (realpath) to `<root>/<one component>/memory`. Symlink escapes and nested
  `memory/deep/memory` directories are refused. Symlinked memory dirs that leave the root are skipped.
- Text copied from a note into the index is sanitized (control characters, zero-width and
  bidirectional Unicode, `[]()` and backticks removed, whitespace collapsed, length capped).
- Filenames containing brackets or parentheses are refused. Applies are serialized with `flock`
  and logged to the audit log.

## What it does not do

It does not judge whether a note is true or useful, does not delete anything (dangling index
lines are reported, never removed), does not rewrite note bodies or frontmatter, and does not
check file paths mentioned inside notes. Needs bash, perl, flock, realpath and GNU `date`.
