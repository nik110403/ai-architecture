# Integration, status, removal

Install: copy `modules/isolation/` and install bubblewrap (`bwrap`) from your distribution. Wrap an agent's
command with `sandbox.py run`; the gates plugin never calls it, so enforcement stays your explicit choice.

Status: `claimed_support` is `planned` in `descriptor.json`; this module has no recorded live evidence.

Removal: delete `modules/isolation/`. It writes nothing outside the directory you pass as `--rw`.
