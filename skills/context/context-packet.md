# Manual context packets

The [compiler](scripts/context_packet.py) is a portable manual manifest producer.
It reads the explicit project's instruction file and emits JSON to stdout. It
does not dispatch agents, select models, execute routing cells, or integrate
with a downstream runtime consumer. No native consumer is supplied or verified.

From the copied context bundle, run:

```sh
python3 -B scripts/context_packet.py --project /path/to/project --task 'Inspect input'
```

The caller may redirect stdout to a file. The compiler itself writes nothing.
The [example project](fixtures/routed/CLAUDE.md) supplies a minimal routing table.
Run the offline tests with:

```sh
python3 -B -m unittest discover -s scripts -p 'test_*.py'
```

## Inputs and selection

- `--project` is required and must identify a directory. Relative paths are
  relative to the caller's working directory; JSON records the absolute path.
  The explicit root may itself be an alias; targets below the opened root may
  not traverse symlinks.
- `--task` is required and cannot be empty or whitespace-only.
- `--room` provides an exact fallback. The first exact Task match wins, even
  over an earlier Room match. Otherwise the first exact Room match wins.
  Duplicate rows retain source order; matching is case-sensitive, never fuzzy.
- `--budget` is a nonnegative integer, default 4000.
- `--stamp` is an optional literal string, escaped by the JSON serializer.
  Without it, provenance date is null. No clock or environment lookup occurs.

Only the explicit project's instruction file is read as content. The compiler
ignores backtick/tilde fenced examples, normalizes line endings for parsing, and
uses its unfenced `## Task routing` section and the six columns Task, Room, Read first,
Do not load, Capabilities, Output, in that order. It accepts a Markdown separator
with at least three dashes per column and optional alignment colons. Rows end
at the first non-table line. Cells may use balanced backticks. Routing lists
split on commas and semicolons, trim whitespace and remove backticks. A
case-insensitive `agent:` capability goes into the agent list; other entries
go into the skills list. Output is parsed but not emitted or invented.

Embedded or escaped pipes, unbalanced backticks, wrong column counts, invalid
headers and malformed separators produce a controlled error rather than a
best-effort interpretation. This is a bounded table parser, not general Markdown.

## JSON version 1

Every successful result includes:

| Field | Meaning |
| --- | --- |
| schema_version | Integer 1; deliberately JSON rather than the source producer's YAML |
| disposition | ROUTED or KERNEL_ONLY |
| task, project, room | Requested task, explicit absolute project path, selected room or null |
| read, exclude | Ordered routing paths; degraded read list contains the instruction filename |
| skills, agent | Ordered capability names; declarations only |
| budget_tokens | Requested advisory budget |
| expected_turns | Always null; no heuristic |
| estimated_tokens | Sum of existing read-file byte sizes, integer-divided by four |
| missing_paths | Unique absent read/exclude targets in encounter order |
| over_budget | Whether the estimate exceeds the advisory budget |
| provenance | Generator name and literal date string or null |

Missing instruction file, absent routing section, or zero rows yields
KERNEL_ONLY, an explicit no-task-routing note, and a read list containing the
instruction filename. Missing files contribute zero bytes. The instruction
file contributes its own size in a degraded packet if it exists.

Over-budget packets still exit 0 and retain all entries, with a WARN diagnostic
on stderr. This preserves the source's warn-only budget: no truncation and no
enforcement. Identical files, root path and arguments yield identical JSON.

## Filesystem boundary and errors

Read/exclude entries must be relative paths below the explicit root. Absolute
paths, Windows drive paths, backslashes, trailing directory separators and any
parent-traversal component are rejected before target metadata lookup. Where
supported, ancestors are inspected relative to directory descriptors and opened
with no-follow directory flags. Otherwise a lexical-component and lstat walk
rejects symlinks, reparse points and non-directory ancestors before descending.
This fallback is not raceproof: the safe-path contract is not a sandbox against
concurrent filesystem changes. Symlink leaves and ancestors are rejected;
regular-file targets are sized without reading
their bodies. Directories and other non-regular targets are unsupported.
Exclusions are inspected for path safety and absence, but never count toward
the estimate. Missing paths are reported rather than silently omitted.

Malformed tables, unknown task/room, unsafe paths, invalid UTF-8, access errors
and invalid CLI input exit 2 with stderr diagnostics and no JSON report.
The compiler does not read room files, discover home configuration or sibling
skills, run shell commands, or fetch anything. The source's Bash/vendor
instructions are not included. Recipient tests include an exact minimal copied
bundle without environment changes; runtime/consumer integration remains unavailable.
