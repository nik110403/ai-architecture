# knowledge-adapters: graph and brain capability handshakes

Two read-only adapters that call a backend you declare in a config file. One CLI: `knowledge.py`. Stdlib only; the
adapters contain no network code. Every command needs `--out DIR` (a 0700 receipt directory) and prints one JSON object.

Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`. Declared dependencies: core-contracts, memory and memory-backends
(`declared-untested`).

## Backends and config

The config is the command-config shape of `../memory-backends/recall.py` (`argv`, `timeout_s`, `name`, `version`, `license`,
`network`) plus a `kind`: `{"kind": "graphify", "graph": PATH}` or `{"kind": "brain", "provider": TOKEN}`. The child process
gets exactly `PATH=/usr/bin:/bin` and a throwaway HOME (no API keys, no base URLs), runs under a timeout and a 1 MiB output
cap, and is killed with its process group on timeout.

## Graph

```bash
python3 -B modules/knowledge-adapters/knowledge.py graph handshake --out R --config graph.json
python3 -B modules/knowledge-adapters/knowledge.py graph query --out R --config graph.json --op explain --arg "node name"
```

Allowed operations: query, path, explain, affected, always run with `--graph PATH`. update, extract, label, add and save-result
are refused before any process starts. Each fact carries `{backend, version, graph_sha256}`; when the graph hash cannot be
recomputed the fact is `unverified`.

## Brain

```bash
python3 -B modules/knowledge-adapters/knowledge.py brain handshake --out R --config brain.json
python3 -B modules/knowledge-adapters/knowledge.py brain status    --out R --config brain.json
python3 -B modules/knowledge-adapters/knowledge.py brain query     --out R --config brain.json --q "..." \
    --source-id src-1 --purpose research --permissions permissions.json
```

`status` returns only the metadata keys id, kind, state, counts and updated_at, each type-checked (short tokens, a string to
integer map, an ISO 8601 time); anything else the backend sends is dropped, and a free-text value in a metadata field is
rejected. `query` and any content transfer need a permission file `{"records":[{"source_id", "provider", "purpose"}]}` holding a
record that matches the request on all three; a missing, malformed, mismatching or mixed-source request exits 3 before any
process is spawned.

## Exercised or not run

`knowledge.py exercised --out R` recomputes, from the run receipts in knowledge.jsonl, whether each lane was actually run: a
lane is EXERCISED only with a successful run receipt that carries a backend version and an attested network denial
(`--isolation netns` or `net-denied`, set by the caller that really ran it that way). A config can never claim it. Brain stays
NOT RUN: its only real backend is not part of this collection.

## Exit codes

0 ok, 1 backend failure, 2 usage or refusal, 3 permission refused (fails closed).

## Limits

- The isolation value is the caller's attestation; the adapter cannot prove a network denial.
- A declared command may do anything its own code allows; the scrubbed environment limits secrets, not behaviour.
- Registry rows 21 (graph, needs `graphify`) and 22 (brain, needs a backend) are `planned`: the default install result is MISSING. See `../../capabilities/registry.json`.
