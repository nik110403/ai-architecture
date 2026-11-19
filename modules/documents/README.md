# documents

Create, read, inspect, package and formula-check office files, and talk to editable or cloud documents
through a revision-bound connector interface. Stdlib only, offline, no credential storage.

The module is a contract layer, not an office suite. A small stdlib writer and reader for docx, xlsx, pptx
and pdf always runs. Optional local converters (`soffice`, `pdftotext`, `pandoc`) are observed at run time
with `--version` (`-v` for pdftotext, which has no long form) and used only behind `--allow-tool`. A missing, hanging or garbled tool reports
`unavailable: <reason>`, never a pass.

## Commands

    python3 modules/documents/documents.py tools status
    python3 modules/documents/documents.py create --kind docx|xlsx|pptx|pdf --spec S.json --out FILE
    python3 modules/documents/documents.py inspect FILE
    python3 modules/documents/documents.py check-formulas FILE.xlsx
    python3 modules/documents/documents.py render FILE --out DIR [--spec S.json] --allow-tool soffice --allow-tool pdftotext
    python3 modules/documents/documents.py package --in DIR --out P.zip
    python3 modules/documents/documents.py verify-package P.zip
    python3 modules/documents/documents.py cloud read|write|create --connector NAME --store S --doc ID \
        [--connectors C --allow-connector] [--base-revision R] [--title T] [--content FILE] [--evidence E]

Every command takes `--format text|json`. Exit codes: 0 ok, 1 check failed, 2 refused or invalid input,
3 revision conflict or existing target, 4 connector unavailable or operation not offered, 5 tool
unavailable. With `--format json` each result carries `status` and, on a non-zero exit, a `reason` to
branch on.

## Specs

- docx: `{"title": "...", "paragraphs": ["...", ...]}`
- xlsx: `{"sheets": [{"name": "Sheet1", "cells": {"A1": 3, "B1": "text", "C1": {"f": "A1+B1", "v": 5}}}]}`
- pptx: `{"slides": [{"title": "...", "body": ["...", ...]}]}`
- pdf: `{"pages": [["line", "line"], ["line"]]}` (at most 80 characters per line)

The same spec always yields byte-identical bytes: fixed zip timestamps, fixed generic document properties
with a title and a constant date, and no creator field.

## Inspecting hostile files

`inspect` decides the kind from the content, not the extension, and never extracts to disk. It refuses
(exit 2, `reason` set) entry names with `..` or absolute paths (`path_traversal`), more than 1000 entries
(`too_many_entries`), expansion beyond 100x (`expansion_ratio`), a macro project (`macro_present`),
external relationships (`external_relationship`), XML with a DTD (`dtd_present`) and truncated or corrupt
files (`truncated`, `corrupt`).

## Formulas

`check-formulas` recomputes a closed grammar (numbers, same-sheet cell references, `+ - * /`, parentheses,
`SUM(range)`) and compares with the cached values. A wrong cached value exits 1 and names the cell
(`Sheet1!C1`). Any other function or reference form is `unsupported`, a cycle is `cycle`; neither passes.

## Connectors

Providers are external commands declared in a connectors file:

    {"connectors": {"name": {"argv": ["python3", "./connector.py"], "operations": ["read", "write", "create"]}}}

The `operations` list is a claim and is ignored. The module runs `argv --describe` (nothing on stdin) and
routes only on what the command reports. Spawning needs `--allow-connector`; without it the command is
refused before any spawn. The child gets only `PATH`, `LANG` and a throwaway `HOME`, runs in its own
process group and is killed as a group on timeout (`DOCUMENTS_CONNECTOR_TIMEOUT`). A connectors file
holding a credential-looking key, a token-shaped value or an email address is refused. `sim` is the one
built-in connector: a local JSON store with revisions (`r1`, `r2`, ...).

`cloud write` needs `--base-revision`; a stale one exits 3 and leaves the store byte-identical. `create`
with an existing title or id exits 3, and `write` to an unknown document exits 3: nothing is ever
recreated. A missing or unavailable connector exits 4 and nothing falls back to `sim` or a local file.
`native: verified` appears only when `--evidence` names a file `{disposable: true, doc, revision_before,
revision_after}` that matches the receipt; otherwise `native: unverified`.

## Limits

The token and credential checks are pattern based, not a secret scanner. Revisions are the connector's own
word: the module binds a write to the revision a caller read, it does not prove the provider honoured it.
