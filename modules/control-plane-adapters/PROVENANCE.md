# Provenance

Same `Field: value` format as the other provenance files here. Every upstream is an operator experiment around a third-party
control plane, named only by its logical role. `Source-sha256` is the sha256 of the source file as read on 2026-11-09.
Nothing was copied. The assessment, the resource adapter and the conformance harness are new; each unit below records what was
learned from an upstream and what was left out.

## operator control-plane experiment: plan binding/ownership runner

Kit file: modules/control-plane-adapters/contract.json
Source: operator control-plane experiment: plan binding/ownership runner
Source-sha256: 5107b1139de9fcc8aa5c5f083fac6bcb5880ca984567d1f5e318aee20198a313
State: re-authored

Deltas:
- only the idea of binding one plan to one owner and of refusing a second controller is kept, as cells of a provider-neutral contract
- no database, package install, relay or credential handling is ported

## operator control-plane experiment: pilot instance installer

Kit file: modules/control-plane-adapters/ASSESSMENT.md
Source: operator control-plane experiment: pilot instance installer
Source-sha256: 314b37e7c3a9ce0511934c72bc25ff6b6da5683317e3376f2ee7286f7490339e
State: re-authored

Deltas:
- assessed only: the install steps (package install, embedded database) are recorded as requirements and a not-ported disposition, never executed

## operator control-plane experiment: network relay in front of a loopback-only instance

Kit file: modules/control-plane-adapters/ASSESSMENT.md
Source: operator control-plane experiment: network relay in front of a loopback-only instance
Source-sha256: b1e9882e17b7bad5401205e33c6e65b5d93098e6d3a53f40c9ac72efc46fc72d
State: re-authored

Deltas:
- assessed only: network exposure of a loopback service is recorded as a reason for the not-ported disposition and is not built

## operator control-plane experiment: trusted pilot

Kit file: modules/control-plane-adapters/ASSESSMENT.md
Source: operator control-plane experiment: trusted pilot
Source-sha256: beefb74b005c373a77502c63c81cc584762ea1bf94affb0c57a0a2c6fbf6bee7
State: re-authored

Deltas:
- assessed only: the pilot's authorisation reference is not ported; the native runner keeps a human-typed confirmation instead

## operator control-plane experiment: trusted runner (process adapter, operator controls)

Kit file: modules/control-plane-adapters/native_adapter.py
Source: operator control-plane experiment: trusted runner (process adapter, operator controls)
Source-sha256: 99155982e4c4284dbf6f4a37181a15d8a2e2c24548ad84acb785c2ac2016ec5e
State: re-authored

Deltas:
- the idea of a thin process adapter with start, status, pause and stop verbs is kept as the adapter protocol of the contract; the operator controls are not ported

## operator control-plane experiment: trusted launch entrypoint

Kit file: modules/control-plane-adapters/ASSESSMENT.md
Source: operator control-plane experiment: trusted launch entrypoint
Source-sha256: d7fe0ab21340462fa649ddbfba2f2a4b484395850a6918298669d6a0c4229b37
State: re-authored

Deltas:
- assessed only: the launch entrypoint is not ported; the native runner is started by a person on a TTY

## Not ported

- the third-party control plane's package install, embedded database, relay, mutual TLS, LAN exposure and trusted-launch authorisation reference
- any notify hook
- operator policy paths under the operator's home
