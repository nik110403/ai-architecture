# Provenance: knowledge-adapters

Each section below is one source unit. The code is a stdlib re-authoring: capability handshakes for a graph backend and
a brain backend, not a copy of the operator's tools.

## mem-context

Source: author's private operator repository, bin/mem-context
Source-sha256: 2aa3e9a6b51115d4ddf998d9fc31daf8c324418eb4821b22fd3b0ae539a4a077
State: re-authored

Deltas:
- The adapter half only: `brain.py` and `graph.py` declare a backend in a config file and call it as a child process under a scrubbed environment. No context assembly is ported.

## graphify skill

Source: author's local Claude skills, graphify/SKILL.md
Source-sha256: a8ff9329f353230d6001ac149a90a2bf7e4d50be2fa53956aaa88c62c3e4ec4e
State: re-authored

Deltas:
- Only the read operations (query, path, explain, affected) are allowed, always with an explicit `--graph`; update, extract, label, add and save-result are refused before any process starts.
- Every fact carries `{backend, version, graph_sha256}`; without a recomputable graph hash the fact is marked unverified.
- The child environment is exactly PATH=/usr/bin:/bin and a throwaway HOME: no API keys, no base URLs.

## memory-recall skill

Source: author's local Claude skills, memory-recall/SKILL.md
Source-sha256: 418d9a5691c52aecfa1765b701bb81a9ae950c56172445f3a9809fd27283fe64
State: re-authored

Deltas:
- The config shape and the sandboxed command runner idea come from `../memory-backends/recall.py` (reused by path); this unit contributes the handshake idea only.

## project-knowledge skill

Source: author's local Claude skills, project-knowledge/SKILL.md
Source-sha256: b24a91421a7d543e4952f152f3dee8c49e3c71a90de81d84927314e801533ea6
State: re-authored

Deltas:
- Used as the description of the capability handshake only; nothing else is ported.

## source-library rules

Source: author's private source-library repository, CLAUDE.md (Non-Negotiables)
Source-sha256: be124d6cccfdd4f8376c2ac3b2d85b2c067e2a67d987442e9680f7de35c30c5c
State: re-authored

Deltas:
- The rule that brain access is metadata-only unless an explicit permission exists becomes the permission-record check in `brain.py` (source_id, provider and purpose must all match; missing, unknown or mixed-source records fail closed with exit 3 and no process spawned).

## brain command line

Source: author's private source-library repository, bin/brain
Source-sha256: 44c029f224b377150e598795539973dcf4a5d651f846b3755a3236c91452d7e7
State: re-authored

Deltas:
- Only the status/query split is mirrored. `status` passes typed metadata keys only (id, kind, state, counts, updated_at). The real backend lives in a private repository, so the brain lane is never reported as exercised here.

## Not ported

- graphify-provision: it installs software; install graphify yourself.
- Completion of the graph and brain rows is a later unit; this module is the adapter half.
