# Provenance

Same `Field: value` format as the other provenance files in this repository. Every upstream is an author's private operator
runner, named here only by its logical role; `Source-sha256` is the sha256 of the source file as read on 2026-11-09. Nothing was
copied: each unit is re-authored (the ideas, not the text). `scripts/check_collection.py` validates this file, and the
module's own test suite runs that check on it because the collection walk covers only `plugins/` and `opencode/`.

## operator unattended plan runner, controller

Kit file: modules/autopilot/autopilot.py
Source: operator unattended plan runner, controller
Source-sha256: a874acecb619414072067e09fd35587e5a9af8f9411cfaffe695a97d605b21cc
State: re-authored

Deltas:
- frozen acceptance, Verify and Touches at kickoff, typed-only refreeze and clear-corrupt, per-plan lock plus a state file, and the stop-reason vocabulary are kept as ideas; the code is new
- the fix-round budget is keyed by lineage root with a Stakes cap, and an added or removed item is drift (speed-tribunal rows 3 and 10)
- the runner integrates onto its own branch from a runner worktree and never pushes; a moved user branch stops the run
- live kickoff needs a TTY, a typed plan id, a finite budget and refuses an agent environment or ancestor; it is a tripwire and says so

## operator unattended plan runner, round library

Kit file: modules/autopilot/spec.py
Source: operator unattended plan runner, round library
Source-sha256: f5ebaf994e3ecbb083b9e23d8810383228f209db6687d0fd38df5d925fa6423d
State: re-authored

Deltas:
- the SPEC, TRACKER and QUESTIONS parsing and the selection rule (first ready `Gate: auto` item, absent means auto, unknown gate words mean human) are re-written against the plan-pack vocabulary
- Touches are literal path prefixes, checked on the net diff of the worker's commits

## operator unattended plan runner, launch library

Kit file: modules/autopilot/budget.py
Source: operator unattended plan runner, launch library
Source-sha256: bd95709ab99e8f5c02706f086c96ec1785d9da59f57321458e31f02033fb0582
State: re-authored

Deltas:
- budget arithmetic reuses the observability cost function; an unknown usage or cost is never zero, a round without usage stops the run and a reserve is charged pessimistically
- launch goes through continuity and the dispatcher, with no transient-user-unit launcher

## operator unattended plan runner, round protocol note

Kit file: modules/autopilot/README.md
Source: operator unattended plan runner, round protocol note
Source-sha256: eb21518f9c53ac3eb98f255f220b69a444edd0d315cf94a7c0e52426b1f7383d
State: re-authored

Deltas:
- the round protocol is described in the README in plain words; the owner-answer injection heading of the note is not carried over

## operator runner capability probe

Kit file: modules/autopilot/verify.py
Source: operator runner capability probe
Source-sha256: 3478d070e7e330ceb74dea19017da89176e2e216085b704b6c2782bc523a8e83
State: re-authored

Deltas:
- only the idea of probing before relying on a capability is kept, as a sandbox probe through the isolation module; the original launches a model to validate a permission mode and that is not ported

## operator background plan runner (one round per tick)

Kit file: modules/autopilot/INTEGRATION.md
Source: operator background plan runner (one round per tick)
Source-sha256: 679b6d5825d658a773037379c88df654a89a392f6eb09fa8398d8b9652cfeec9
State: re-authored

Deltas:
- no background runner ships; resume is the one-round-at-a-time primitive and a person creates any scheduler entry (the assessment records this as not-ported with an equivalent)

## operator ready-frontier report

Kit file: modules/autopilot/autopilot.py
Source: operator ready-frontier report
Source-sha256: aa838af24b0c6008dc3938b63268bc993be73acd129b469c1ba1c10f7220154a
State: re-authored

Deltas:
- the ready frontier is the status listing of done, pending and queued items, computed with the same selection rule as the runner

## operator sandboxed verify runner

Kit file: modules/autopilot/verify.py
Source: operator sandboxed verify runner
Source-sha256: dedbfb8adf9c977b303d8bed5c32b6b41e43d10ba4cee94497477a3eb70abf63
State: re-authored

Deltas:
- Verify runs in a tree built from the object store (no checkout, no hook or filter) and then in the isolation module; exit 3 from the sandbox is `unverifiable` and never an unsandboxed run
- the sandbox PATH seam can only make the sandbox less available, never more

## Not ported

- the transient-user-unit launcher as a default
- any permission-skip default and any validated-mode probe that launches a model
- the owner-answer injection heading
- operator policy paths and allow-lists under the operator's home
- the rule that a repository must be a direct child of one folder
- OpenCode credit accounting tied to one provider account
- the third-party control plane's package install, embedded database, relay, mutual TLS, LAN exposure and trusted-launch authorisation reference
- any notify hook
