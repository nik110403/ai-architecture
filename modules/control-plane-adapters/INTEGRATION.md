# Integration, status, removal

Install: copy `modules/control-plane-adapters/`. The [autopilot](../autopilot/README.md) module is a declared dependency; the
resource adapter itself needs only Python and, for the scope backend, a systemd user manager that delegates cpu, memory and pids.

Use the adapter by prefixing a command: `resources.py run --backend systemd-scope ... -- CMD`. A detached autopilot run can be
placed inside it with `--resource-limits FILE` on `kickoff`. Nothing is wired in by default.

To trial another control plane, install it yourself, then run `conformance.py --adapter ABSOLUTE_PATH --allow-adapter` and read
the cells. A pass of all 14 cells moves it to `supported` only after a human edits the assessment.

Status: `claimed_support` is `planned` in [descriptor.json](descriptor.json).

Removal: delete `modules/control-plane-adapters/`. A scope leaves no unit behind; `resources.py stop --unit UNIT` ends a running one.
