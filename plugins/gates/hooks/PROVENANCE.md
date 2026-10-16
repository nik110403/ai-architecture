# Provenance

One section per ported unit. Each section uses the `Field: value` lines of the collection's
provenance convention (`Source`, `Source-sha256` = sha256 of the source file as read when it was
ported, `State` = verbatim | adapted | re-authored) followed by a `Deltas:` list. 
`scripts/check_collection.py` validates this file. Sources named
`~/.claude/...` are an author's installed hook files; `claude_improvement/bin/...` are files from the
author's operator repository. Neither is shipped.

Hooks that were bash scripts embedding a single `python3 -c '...'` program were extracted into
standalone `.py` files (same program text); the bash wrapper is replaced by `hooks.json` entries.
Fail-closed and fail-open behaviour is unchanged from the source unless a delta says otherwise. Plugin-wide deltas (stricter than the sources): every hook reads its stdin payload first via `lib/gates_config.py` and fails closed when it cannot be parsed (pre-tool hooks deny, Stop hooks block, the SessionStart hook emits a warning); a user config that is not a regular file, is malformed, wrong-typed or has an unknown top-level key is invalid and is reported in every hook's reply as a `systemMessage` (the destructive guard also denies).

## bash-destructive-guard

Kit file: hooks/bash-destructive-guard.py + hooks/bash-destructive-guard.sh
Source: ~/.claude/hooks/bash-destructive-guard.sh
Source-sha256: eb5c59541bf30610b7250e9745e315b6cd45f399e2aa62132c89227c11a9060a
State: adapted

Deltas:
- rule engine (heredoc stripping, de-escaping, command-position matching, ASK tier, protected-data and secret-read checks, crash-to-deny wrapper) is the source program text; no check was relaxed
- personal protected-root inventory replaced by user-configured `protected_roots` / `protected_containers` / `protected_names`; the built-in floor keeps the secret stores (ssh, gnupg, password-store) and the standard tool credential files
- default HOME fallback replaced by the process HOME (no hard-coded user directory)
- added user-scope `deny_patterns` / `ask_patterns` (same length, nested-quantifier and time-budget safety as the project file); the per-project destructive-patterns file stays additive-only
- missing `python3` now DENIES (wrapper `bash-destructive-guard.sh`); the source logged and let the command pass
- header comments dropped: the incident note, the dated change log and the list of known out-of-scope evasions; limits are described generically in README.md
- ASK-tier pattern list attribution moved to LICENSES.md

## edit-budget-gate

Kit file: hooks/edit-budget-gate.py
Source: ~/.claude/hooks/opus-code-gate.sh
Source-sha256: 0866851e6fa1a848fe23b093fbfc5bf54f531d9808c1016bb1d7b040bd8183fe
State: adapted

Deltas:
- hard-coded model name and 5th-edit rule generalised to config `edit_budget_models` (model substring -> max code edits per session); longest matching key wins; empty by default so the gate is inert
- counter state moved from a fixed home-directory path to `$CLAUDE_HOOK_STATE_DIR` / `$CLAUDE_PLUGIN_DATA` / `~/.cache/ai-architecture/gates`; counter file still opened with O_NOFOLLOW
- override phrase is config `override_phrase` (default `override: inline`); the meta/sidechain/compact-summary spoof filters are unchanged
- UI-file routing to a design builder is now optional config `ui_delegate_role`; delegate names come from config
- subagent bypass, tiny-edit carve-out (with the build-script exclusion), shebang sniffing are unchanged; an unparseable payload now denies (plugin-wide delta above)
- telemetry emitter import removed (the telemetry library is not part of this plugin)

## no-code-guard

Kit file: hooks/no-code-guard.py
Source: ~/.claude/hooks/fable-code-guard.sh
Source-sha256: a46b4024f5089feefb46ca589a496d1f8f4c5c6251128eb944e79df0f11da75d
State: adapted

Deltas:
- hard-coded model name generalised to config `no_code_models` (list of model substrings); when the list is empty the hook is inert
- fail-closed on an undetermined model is kept (transcript model, then the `model` key of the user settings file, else deny) and applies whenever `no_code_models` is non-empty
- sidechain-model check for subagent calls, tiny-edit carve-out with build-script exclusion and the code-extension list are unchanged
- deny messages no longer name a specific agent or policy date; they name config `delegate_role`
- telemetry emitter import removed (the telemetry library is not part of this plugin)

## review-stop-gate

Kit file: hooks/review-stop-gate.py
Source: ~/.claude/hooks/review-stop-gate.sh
Source-sha256: b70dd0487c546f92adcbab174baf2991a44ad982be1e3253426ede2a48e045ca
State: adapted

Deltas:
- reviewer and UI-builder agent names come from config (`design_review_role`, `security_review_role`, `ui_builder_roles`) and match after the last `:`
- tool_use/tool_result correlation, denied-call exclusion, no-op-edit exclusion and the stop_hook_active loop guard are unchanged
- anti-spoof name matching is a delta: the source compared the exact name; here names match after the last `:` and a namespaced name counts only when its namespace is in config `trusted_plugin_prefixes` (default `ai-architecture`)
- default `ui_builder_roles` is `["design-builder"]`, as in the source (dispatching it counts as a UI edit)
- messages no longer point at a personal router document
- telemetry emitter import removed (the telemetry library is not part of this plugin)

## clean-state-check

Kit file: hooks/clean-state-check.py
Source: claude_improvement/bin/hooks/clean-state-check.sh
Source-sha256: d070b740ddfb5f0122222db03d0c4b4449cc3328a0a69c0db7cff697fb28eec6
State: adapted

Deltas:
- builder agent names come from config `builder_roles` and match after the last `:`
- the unused `Delivered:` check and its personal wording were removed
- last-worked-tracker state is written through hooks/lib/resume_state.py using the plugin state directory
- tracker discovery, closed-pack exclusion, repo-root containment, regular-file reads and nag logic are unchanged

## resume-inject

Kit file: hooks/resume-inject.py
Source: ~/.claude/hooks/resume-inject.sh
Source-sha256: c299f6e451961013ce2ad6f0334fc4ab182dfcacb607bc7d00f12a7c58c2bfdb
State: adapted

Deltas:
- library path now relative to the hook file; state directory from the plugin config helper
- fencing, control-character stripping before delimiter stripping, override-phrase scrub, exec-capable git config guard are unchanged; an unparseable payload now yields a warning only (SessionStart cannot block)
- internal change-log comments removed; docstring added

## run-hook.sh

Kit file: hooks/run-hook.sh
Source: ~/.claude/hooks/bash-destructive-guard.sh
Source-sha256: eb5c59541bf30610b7250e9745e315b6cd45f399e2aa62132c89227c11a9060a
State: re-authored

Deltas:
- new shared wrapper written for this plugin: runs each gate hook under wall-clock caps (stdin copy plus guard, about 8.2 s worst case) and fails closed (deny / block / systemMessage by hook kind) on timeout, crash, non-JSON output, or missing python3/timeout; the listed source is only the thin wrapper whose fail-closed shape it follows

## lib/trackers.py

Kit file: hooks/lib/trackers.py
Source: ~/.claude/hooks/lib/trackers.py
Source-sha256: 37f91e313760c922e5bfbb1a03804cdcf4267e6b6ad458601415c565f9b40ac3
State: adapted

Deltas:
- docstring references to internal item numbers and paths removed; code unchanged

## lib/resume_state.py

Kit file: hooks/lib/resume_state.py
Source: ~/.claude/hooks/lib/resume_state.py
Source-sha256: 601891f67a335b57e07639138b265e7a31dcec09de73b6c14433fbf3ac5ea926
State: adapted

Deltas:
- state directory taken from `gates_config.state_dir()` instead of a fixed home path; docstring reworded

## tests/run_destructive.py + tests/fixtures/destructive-guard.json

Kit file: tests/run_destructive.py
Source: claude_improvement/bin/tests/destructive-guard-runner.py
Source-sha256: f5c9864fa71b2f51b268905c06a56e0bbf1b2125f0a5035600966ca3e31813ef
State: adapted

Deltas:
- runner points at the plugin hook and a test config instead of an installed hook; fixture commands use `@HOME@` instead of a real home path; personal folder names replaced by generic ones (`vault`, `workspace`, ...) that the test config marks as protected
- fixture table ported 1:1 (143 cases from claude_improvement/bin/tests/fixtures/destructive-guard.json) plus 9 added cases; latency limit relaxed to a sanity bound on wall time

## tests/run_hooks.py, tests/run_config_scope.py, tests/run.sh

Kit file: tests/
Source: claude_improvement/bin/tests/test-destructive-guard.sh
Source-sha256: 8dba6849dd7950d90054739472473a59d041762229b331aada7a1f9404082641
State: re-authored

Deltas:
- new tests written for this plugin (edit budget, no-code, review stop, clean state, resume inject, configuration scope) using recorded payload and transcript fixtures; the listed source is only the thin wrapper whose entry-point shape the test run script follows

## Deltas

- Applies to the whole plugin: no telemetry, no personal paths or names, configuration from user scope only, plus the section-level deltas above. No fail-closed check of a source program was removed; the behaviour change that makes the plugin stricter than its source is the missing-`python3` denial.

## Not ported

- `tribunal-readonly-guard` (read-only agent seat guard, with its test): not shipped. A shell-parsing allowlist cannot make an agent read-only; newline and `\r\n` separators, `|&`, nested `$( $( ) )`, `sed '1e cmd'` and `s///e` were reproduced as bypasses after the last fix round. Use an agent `tools:` allowlist of Read/Grep/Glob instead (see the README).
