# Control-plane assessment

Generated from `assessment.json` by `conformance.py --render-assessment`. Dispositions: `supported` (every contract cell passed), `adapter-slot` (the harness can drive it once an operator supplies it), `not-ported` (equivalent or blocker named), `incomplete` (visibly unfinished, blocker named).

## native-autopilot

- What: The native optional unattended runner in modules/autopilot, driven through the adapter protocol by native_adapter.py.
- Kind: native
- Disposition: **supported**
- Requirements: Linux, git, Python 3.9+, bubblewrap for Verify
- Contract cells: 14 pass, 0 fail, 0 not-run
- Equivalent: itself: the supported runner for this contract
- Evidence: `test:modules/control-plane-adapters/tests/test_control_plane.py::test_a14_native_autopilot_passes_every_cell`; `test:modules/autopilot/tests/test_autopilot.py::test_a1_journey`

## paperclip

- What: The third-party control plane (a Node.js package with an embedded database) that the operator's experiments were built around.
- Kind: third-party
- Disposition: **not-ported**
- Requirements: Node.js runtime, embedded database, network install from a package registry
- Contract cells: 0 pass, 0 fail, 14 not-run
- Equivalent: the native autopilot; a third-party adapter can be trialled with conformance.py --adapter <absolute path> --allow-adapter once an operator has installed it
- Evidence: `test:modules/control-plane-adapters/tests/test_control_plane.py::test_a14_assessment_validates_and_is_complete`; provenance:operator control-plane experiment: plan binding/ownership runner

## worker-containment

- What: Isolation of the worker itself (not only of the Verify step) in an unattended run.
- Kind: native
- Disposition: **incomplete**
- Requirements: a sandbox with a network mode for the harness, changes to the dispatcher
- Contract cells: not applicable
- Blocker: harness needs network; sandbox.py has no network mode; orchestration outside the fence
- Evidence: `test:modules/autopilot/tests/test_autopilot.py::test_a10_write_outside_rw_fails`; provenance:operator unattended plan runner, controller

## background-round-runner

- What: A background plan runner that executes one round per scheduler tick.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: a scheduler entry created by the operator
- Contract cells: 0 pass, 0 fail, 14 not-run
- Equivalent: autopilot resume run by the operator's own scheduler; maintenance.py scheduler plan prints the unit or cron text without installing it
- Evidence: provenance:operator background plan runner (one round per tick); `test:modules/autopilot/tests/test_autopilot.py::test_a2_detach_creates_no_scheduler_entry_and_records_pid`

## ready-frontier-report

- What: A report of the ready frontier of a plan pack.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: a plan pack
- Contract cells: not applicable
- Equivalent: autopilot status --json lists done, pending and queued items from the same selection rule the runner uses
- Evidence: provenance:operator ready-frontier report; `test:modules/autopilot/tests/test_autopilot.py::test_a12_status_is_read_only_and_lockless`

## sandboxed-verify-runner

- What: Running an item's Verify command in a fresh tree inside a sandbox.
- Kind: operator-experimental
- Disposition: **supported**
- Requirements: bubblewrap
- Contract cells: not applicable
- Equivalent: modules/autopilot/verify.py (object-store tree) composed with modules/isolation/sandbox.py
- Evidence: provenance:operator sandboxed verify runner; `test:modules/autopilot/tests/test_autopilot.py::test_a10_write_outside_rw_fails`; `test:modules/autopilot/tests/test_autopilot.py::test_a10_no_bwrap_is_unverifiable_and_never_unsandboxed`

## plan-binding-runner

- What: A runner that binds a plan to a third-party control plane and records ownership there.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: the third-party control plane, Node.js runtime, embedded database
- Contract cells: 0 pass, 0 fail, 14 not-run
- Equivalent: the native autopilot's persisted ownership: a flock plus an owner record with pid, start ticks and boot id
- Evidence: provenance:operator control-plane experiment: plan binding/ownership runner; `test:modules/autopilot/tests/test_autopilot.py::test_a7_double_kickoff_exactly_one_controller`

## pilot-instance-installer

- What: An installer for a pilot instance of the third-party control plane.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: network install from a package registry, embedded database
- Contract cells: not applicable
- Equivalent: modules/installer: a generic manifest-driven install with rollback; a pinned third-party package is described in PATTERNS.md, never executed
- Evidence: provenance:operator control-plane experiment: pilot instance installer; `test:modules/installer/tests/test_installer.py::test_a15_apply_noop_upgrade_rollback`

## network-relay

- What: A network relay in front of a loopback-only instance of the third-party control plane.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: LAN exposure, mutual TLS
- Contract cells: not applicable
- Blocker: exposes a control plane on the network; outside the optional-adapter contract and excluded on purpose
- Evidence: provenance:operator control-plane experiment: network relay in front of a loopback-only instance

## trusted-pilot

- What: A trusted pilot of the third-party control plane with its own authorisation reference.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: the third-party control plane, an operator authorisation reference
- Contract cells: not applicable
- Blocker: depends on an authorisation reference specific to one operator and one control plane; nothing portable to ship
- Evidence: provenance:operator control-plane experiment: trusted pilot

## trusted-process-adapter

- What: A process adapter that lets the third-party control plane start a runner under operator controls.
- Kind: operator-experimental
- Disposition: **adapter-slot**
- Requirements: the third-party control plane
- Contract cells: 0 pass, 0 fail, 14 not-run
- Equivalent: conformance.py accepts any adapter named by an absolute path; an empty slot reports every cell not-run
- Evidence: provenance:operator control-plane experiment: trusted runner (process adapter, operator controls); `test:modules/control-plane-adapters/tests/test_control_plane.py::test_a14_empty_slot_is_not_run_never_pass`

## trusted-launch-entrypoint

- What: The launch entrypoint that starts a trusted run with an authorisation reference.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: an operator authorisation reference
- Contract cells: not applicable
- Blocker: an authorisation reference is not portable; native live kickoff uses a TTY plus a typed confirmation instead
- Evidence: provenance:operator control-plane experiment: trusted launch entrypoint

## guard-installer

- What: The historical installer for the destructive-command guard.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: a host-specific guard
- Contract cells: not applicable
- Equivalent: modules/installer: backup, hash, install and rollback as one pattern; guard integrity itself stays with the gates plugin
- Evidence: provenance:operator guard installer (historical); `test:modules/installer/tests/test_installer.py::test_a15_interrupt_at_every_journal_step_then_recover`

## sandbox-installer

- What: The historical installer for the sandbox package.
- Kind: operator-experimental
- Disposition: **not-ported**
- Requirements: a package manager and root
- Contract cells: not applicable
- Equivalent: sandbox.py probe plus the documented install line in the isolation README; no installer runs a package manager
- Evidence: provenance:operator sandbox installer (historical)

