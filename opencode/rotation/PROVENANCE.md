# Provenance

Same `Field: value` format as the other provenance files in this repository (`Source-sha256` = sha256 of the source file
as read when it was ported). Sources named `~/.config/opencode/...` or `private operator repository, bin/...` are an author's
installed OpenCode file and operator-repository test file; they are not shipped. `scripts/check_collection.py`
validates this file.

## model-rotate.ts

Kit file: rotation/model-rotate.ts
Source: ~/.config/opencode/lib/model-rotate.ts
Source-sha256: a7a2130571c91d9a9e7223cd84d28b3c4e258086aa96de774294c14d52602e5e
State: adapted

Deltas:
- RNG seam added: `ROTATION_SEED` selects a seeded generator (sha256-derived mulberry32); default stays `node:crypto` `randomInt`
- fail safe is total: the whole hook body is wrapped, so it never throws. The source deliberately threw to reject an unknown or disabled `pin_arm`, to redirect a `claude:*` pin and to refuse unknown alias names; here an unusable pin is ignored (the weighted draw runs, reason logged) and nothing blocks a call
- the pool read is stricter: a symlinked, oversized or wrong-`version` file, or any malformed entry in a role's list, means no rotation for that call/role; names and integer weights are validated, and nothing reaches a shell
- pool schema reduced to placeholder models and weights (`{model, weight, enabled}` per role, optional `independent_roles`); the `harness:model` arm syntax, harness allowlists, `claude_task` redirects, quota/usage-cache reads and the personal default paths are removed
- removed: alias bypass (`<role>--<slug>` names pass through untouched), the model self-check boilerplate stripper, charge-id extraction, the default decision-log path (the log is opt-in via `ROTATION_LOG`)
- variant-agent switching uses `<role>--<slug>` (the variant must exist in the OpenCode config or the call is unchanged). Both `pin_arm` and `builder_arm` are header-only (before the first blank line, at most 15 lines), with CRLF normalisation. Independent roles exclude the builder; duplicate `builder_arm:` lines anywhere skip rotation. Pins equal to the builder or independent-role pins without a builder header are ignored.
- the variant-agent generator the author uses is not included; variants are written by hand (see README.md)
- permission neutrality matches the staged hook semantics: only arguments change, no policy decision or worker startup; no new external-worker gate is needed

## plugin.ts

Kit file: rotation/plugin.ts
Source: ~/.config/opencode/plugins/claude-hooks.ts
Source-sha256: 44bad7fa79e39311459b1f6258b09417d377aebb1cd1f1bdabffb1536852c8d5
State: re-authored

Deltas:
- a four-line entry that exposes only the rotation module's `tool.execute.before` hook; the source plugin wires many other personal modules, none of which is included

## rotation tests

Kit file: rotation/tests/rotation.test.ts and rotation/tests/run.sh
Source: private operator repository, bin/tests/test-opencode-rotation.sh
Source-sha256: 97883b27dd46121e09467fb1fb8e995cad473d9516959887b5273c8266aa600b
State: re-authored

Deltas:
- the source's module checks (slug, pin and builder parsing, weighted pick with an injected generator, header-only pin, builder exclusion, fail-open on invalid pool) are rewritten against the reduced module; its second half (variant-agent generator) is not ported
- new cases: seeded determinism and 3:1 weights, fail-safe table for invalid pools and config, an unlisted model is never produced, no throw on odd input
- exact output-shape checks preserve absent/ask/deny policy decisions and input metadata; a tripwire plugin client verifies routing does not request jobs even with forged identity
- the test runner runs the Claude Code hook tests first and the module tests when `bun` is present (prints `NOT RUN` otherwise)
