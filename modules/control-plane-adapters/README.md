# control-plane-adapters

Two things in one module: an optional **resource-control adapter** (bounded memory, CPU and process count for a command, with a
recoverable cancel) and an **assessment** of external control planes with a **conformance harness**. Stdlib only, Python 3.9 or
later, Linux. Descriptor claim: `planned` (see [descriptor.json](descriptor.json)); per-capability state is in `../../capabilities/registry.json`. Nothing here installs a package, starts a service or
opens a network connection.

## Resource adapter

```
python3 -B modules/control-plane-adapters/resources.py probe
python3 -B modules/control-plane-adapters/resources.py run --backend systemd-scope --memory-max-mb 512 --tasks-max 64 --timeout 60 -- CMD ARG...
python3 -B modules/control-plane-adapters/resources.py stop --unit UNIT
python3 -B modules/control-plane-adapters/resources.py verdict --evidence FILE
```

Backends: `systemd-scope` runs the command in a transient user scope with MemoryMax, MemorySwapMax=0, CPUQuota and TasksMax
and writes an evidence file from the scope's own cgroup counters; `rlimit` sets address-space and CPU limits in the child and
is declared partial (no per-tree task bound, no CPU share). Exit codes: the command's own status; 124 timeout; 125 killed by a
limit; 2 refused (a run with no limit at all is refused); 3 backend unavailable. When a backend is unavailable the command
never runs: there is no unbounded fallback. A scope ends with its command and leaves no unit behind. Read-only telemetry
from the observability module is not a bound; `verdict` reports `not-bounded` for it.

## Assessment and conformance

[ASSESSMENT.md](ASSESSMENT.md) is rendered from [assessment.json](assessment.json) and records, per candidate control plane, a
disposition (`supported`, `adapter-slot`, `not-ported` or `incomplete`), the requirements, the contract cells that passed, and the
evidence. The native runner in the autopilot module is the one supported implementation. Worker containment is recorded as
`incomplete`: the harness needs the network and the sandbox has no network mode.

`conformance.py` runs the 14 cells of [contract.json](contract.json) against an adapter that speaks the protocol of
[native_adapter.py](native_adapter.py). An adapter is given by absolute path and only runs with `--allow-adapter`; the harness
collects the evidence itself (the fake harness's call log, process liveness and the process tree it launched, git on the runner
branches; it starts the adapter in a session of its own, kills the controller inside that tree, corrupts a state file only when
the launched controller provably holds it open, and edits the plan itself; interruption, resume-once, pause and stop are decided
on those effects, never on an adapter-written state.json or pid; a refusal cell passes only on its specific reason, read as
the discriminator against a crash and accompanied by the harness-verified injection and no new dispatch), a cell it cannot decide from its own evidence or that did not run is never counted as a pass, and `--validate-assessment` checks the
assessment file against what the harness can show. The harness only ever uses fake providers.

## Tests

`python3 -B -m unittest discover -s modules/control-plane-adapters/tests`. Cells that need systemd scopes or the sandbox skip
with a printed reason only when the probe fails (`AIARCH_REQUIRE_RESOURCE=1` and `AIARCH_REQUIRE_SANDBOX=1` make that a failure).
