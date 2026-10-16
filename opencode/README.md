# opencode/

State: **runnable** (offline tests ship here). Optional; nothing in the main plugin refers to it.

| Path | What it is |
| --- | --- |
| `bridge/` | An OpenCode plugin that runs the guard hooks of `plugins/gates` (same scripts, same user-scope config) and adds a structural check for Bash commands. |
| `agents/opencode-agents-sync` | Prints an OpenCode config `agent` block generated from the role files in `agents/`. |
| `tests/run.sh` | Generator test (golden JSON) and bridge unit tests. |

How to use it and what is left out: [adapters/opencode.md](../adapters/opencode.md).

## Tests

```bash
bash opencode/tests/run.sh
```

Needs `python3`; the bridge unit tests also need [bun](https://bun.sh). Without bun the script prints
`NOT RUN: bridge tests need bun ...` for that part and tests only the generator. The bridge unit tests spawn the real
gates hooks with an empty temporary HOME. The bash structural check needs two packages
(`web-tree-sitter`, `tree-sitter-bash`, pinned in `bridge/package.json`); the tests use a stub parser, so they do
not need them installed. If the packages are missing at run time that one step is skipped and logged: nothing is
auto-approved (OpenCode asks as usual) and the gates hooks still run.
