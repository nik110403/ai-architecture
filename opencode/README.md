# opencode/

State: **runnable** (offline tests ship here). Optional; nothing in the main plugin refers to it.

| Path | What it is |
| --- | --- |
| `bridge/` | An OpenCode plugin that runs the guard hooks of `plugins/gates` (same scripts, same user-scope config) and adds a structural check for Bash commands. |
| `agents/opencode-agents-sync` | Prints an OpenCode config `agent` block generated from the role files in `agents/`. |
| `runtime/` | Opt-in continuity runtime: usage records, resume injection, per-session freeze, HANDOFF gate and a read-only background hub over `modules/continuity`. Tests: `bash opencode/runtime/tests/run.sh` (bun). |
| `tests/run.sh` | Generator test (golden JSON) and bridge unit tests. |

How to use it and what is left out: [adapters/opencode.md](../adapters/opencode.md).

## Tests

```bash
bash opencode/tests/run.sh
```

Needs `python3`; the bridge unit tests also need [bun](https://bun.sh). Without bun the script prints
`NOT RUN: bridge tests need bun ...` for that part and tests only the generator. The bridge unit tests spawn the real
gates hooks with an empty temporary HOME. The bash structural check needs two packages
(`web-tree-sitter` 0.25.10, `tree-sitter-bash` 0.25.0). The bridge tests use a stub parser, but the structural
acceptance (`tests/u11-structural.test.ts`, the only component allowed to auto-approve) needs the real ones, kept
OUTSIDE the source tree. One line installs them into a directory of your choice:
`bun add --cwd "$AIARCH_PARSER_DIR" web-tree-sitter@0.25.10 tree-sitter-bash@0.25.0`, then run
`AIARCH_PARSER_DIR=<that dir> bash opencode/tests/run.sh`. With the variable set, a missing or wrong-version package
FAILS the run. Without it the acceptance is skipped with a loud warning and the last line reads
`OPENCODE TESTS PASSED WITH STRUCTURAL PARSER ACCEPTANCE SKIPPED`: nothing verified the classifier. At run time, if
the packages are missing that one step is skipped and logged: nothing is
auto-approved (OpenCode asks as usual) and the gates hooks still run.
