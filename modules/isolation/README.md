# isolation

Optional Linux sandbox/cleanup adapter around bubblewrap. Stdlib only; no root, no setuid helper, no `--privileged`.
Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json` (fixture-tested on one Linux host; not exercised in a live agent session).

```
python3 -B modules/isolation/sandbox.py probe [--format text|json]
python3 -B modules/isolation/sandbox.py run --rw DIR --timeout SECONDS -- CMD [ARG...]
```

`probe` prints `available`, or `unavailable: <reason>` and exits 3. It really starts a sandbox (`bwrap ... -- true`,
10 s cap), because a host can have `bwrap` installed and user namespaces restricted.
`run` executes CMD under `bwrap --unshare-all --die-with-parent --new-session`: `/` read-only, DIR read-write (and the
working directory), a private `/tmp`, `/run` and `/var/run`, a masked home directory, `--proc /proc --dev /dev`, and a
cleared environment (`PATH`, `HOME=/tmp`, `LANG` only). Exit status is the command's; 124 on timeout (the process group
is killed and reaped, and the pid namespace takes every descendant with it, including `setsid` daemons); 2 for refused
input; 3 when unavailable. **It never falls back to running the command unsandboxed.** A bwrap setup failure at run time
is detected through `--json-status-fd` and also exits 3, never with the command's status.

`--rw` refuses `/`, the home directory, any ancestor of it, a symlink and a non-directory (exit 2).

## Exposure (read this before relying on it)

- Everything world-readable under `/` is readable (the root is bound read-only): system files, other projects outside
  your home directory. Your home directory, `/tmp`, `/run` and `/var/run` are masked with empty tmpfs, except DIR.
- Path-based unix sockets outside those masks stay connectable: a read-only mount does not stop `connect()`.
  The masks cover the usual ones (the user bus under `/run`, `docker.sock`, sockets under your home or `/tmp`); a
  socket in another directory (`/srv`, `/mnt`, `/opt`) is reachable.
- No network (loopback included), no host processes visible, no host environment. There is no seccomp filter, no
  CPU/memory limit and no protection against a kernel bug. It is process isolation for a command you chose to run, not a
  boundary against hostile code, and a worktree or role declaration is not a substitute for it.
- Linux only. Any other platform, a missing `bwrap`, or a host that forbids unprivileged user namespaces reports
  `unavailable` (tests: set `AIARCH_REQUIRE_SANDBOX=1` to make that a failure instead of a skip with a reason).
