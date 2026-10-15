# Context packet dependencies

- Python 3.9 or newer; standard library only. No installation or network access.
- Capability-selected filesystem access: directory descriptors, no-follow opens
  and descriptor-relative stat where supported; otherwise a standard-library
  lexical-component/lstat walk, rejecting symlinks and reparse points. The
  fallback is not raceproof and is not a sandbox. Tested on Linux with missing
  flags simulated; this is not evidence of a native Windows runtime test.
  Unavailable required filesystem operations produce controlled diagnostics.
- Runtime asset: [context_packet.py](scripts/context_packet.py). Input is the
  explicitly selected project's instruction file, not a global configuration.
- Offline checks: [test_context_packet.py](scripts/test_context_packet.py) and
  the [example routing table](fixtures/routed/CLAUDE.md) with its
  [sized input](fixtures/routed/input.txt). Tests create temporary projects and
  copy this context bundle without requiring sibling skills or global state.
- Contract and invocation: [context-packet.md](context-packet.md).

The port follows the inspected manual episode-brief producer: exact task first,
room fallback, ordered lists, byte-size estimate, warn-only budget, and no
consumer integration. Deliberate changes are JSON schema version 1, controlled
exit 2 errors, explicit missing paths, full degraded fields and safer target
admission. No Bash helpers, vendor instructions or native harness hooks are
runtime dependencies. Copying the bundle does not activate a harness.
