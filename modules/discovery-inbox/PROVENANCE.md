# Provenance: discovery-inbox

Each section below is one source unit. Only the public interface of the operator's companion was read; nothing is copied.
The code is a stdlib re-authoring: an adapter that drives a companion through its own commands, not a port of the companion.

## operator discovery companion, handoff command

Source: operator discovery companion, handoff command
Source-sha256: 711f0aa25dbc5818eb0e0bab79f8ed6f41a524fecc79163bcde0806dd13ec462
State: re-authored

Deltas:
- Interface only: the command line (`--apply --targets-root`), the staged and placement report lines, and the `unchanged` and `conflict` outcomes are pinned as `handoff-brief/1`. No routing table, need-tag list or category list is ported.

## operator discovery companion, set command

Source: operator discovery companion, set command
Source-sha256: 19ef8348bd37e52b1c07a1f01c3b4d71878811dbe93a5fa5164697837602bb5e
State: re-authored

Deltas:
- Interface only: `set <owner/name> status <value>` and its acknowledgement line; the adapter confirms through the list command rather than trusting the acknowledgement.

## operator discovery companion, list command

Source: operator discovery companion, list command
Source-sha256: ca21876d2376e411209f7aa19631909672d69e9a0e0141eb2abd7fa5d3e85b12
State: re-authored

Deltas:
- Interface only: whitespace-separated rows with the status first and the candidate id as an exact token; used for the read-back and `inbox list`.

## operator discovery companion, shared library (record format, enums)

Source: operator discovery companion, shared library (record format, enums)
Source-sha256: 32a461f2518cc6ebe900352b951714be0f1bbca65fd4a968aff9dd0bc9cc9113
State: re-authored

Deltas:
- Record shape only: frontmatter keys, the status vocabulary and the body labels, as the closed `handoff-brief/1` key and label set in `interface.json`. The adapter writes a disposable record in that shape and reads only its status line.

## operator discovery companion, project instructions (workflow and handoff sections)

Source: operator discovery companion, project instructions (workflow and handoff sections)
Source-sha256: 26aed9ede85f4c4bc5a3af45b58eebd0b82468c69fecba4a358217c2545c0c26
State: re-authored

Deltas:
- The handoff workflow idea (a candidate moves from the companion into a project's plan pack and returns a recorded decision) is re-expressed as the roundtrip; none of the instructions' text is carried over.

## operator discovery companion, handoff sandbox test

Source: operator discovery companion, handoff sandbox test
Source-sha256: a0b65e3fc303bb36c4d0195d6150531c3f1d7f097d64209b654e23bccefe5490
State: re-authored

Deltas:
- The sandbox idea (copy the command directory, give it an empty data folder and a throwaway targets root) becomes a `git archive` of only the declared files; the authored fixture companion in the tests mirrors the idempotency and conflict semantics, not the test script.

## Not ported

- The sync command (calls a hosted API), the interactive terminal UI, the deep-scan command (starts agents), and the triage, review, detect, fold, batch, seed, splice-survey and check commands.
- The routing tables (need-tag and category to target) and the live candidate ledger and its data.
