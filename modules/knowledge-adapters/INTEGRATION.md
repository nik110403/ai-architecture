# Integration, status, removal

Install: copy `modules/knowledge-adapters/` together with `modules/memory-backends/` (it reuses the config loader of
`../memory-backends/recall.py` by path). Install a graph backend such as graphify yourself; this module never installs software.

Example graph config:

```json
{"kind": "graphify", "graph": "/path/to/graph.json", "argv": ["/path/to/graphify"], "timeout_s": 20,
 "name": "graphify", "version": "0.8", "license": "MIT", "network": false}
```

To record a network-denied run, start the command under a network namespace (for example `unshare -rn`, where your system
allows it) or a socket-denying wrapper you have verified, and pass `--isolation netns` or `--isolation socket-denied`.

Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json`, not tested in a live session. Removal: delete `modules/knowledge-adapters/` and the receipt directory
you chose.
