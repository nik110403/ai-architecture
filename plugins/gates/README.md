# ai-architecture-gates

Optional hook gates for Claude Code. State: **runnable** (hooks and offline tests ship here).

This is a separate plugin. The main `ai-architecture` plugin does not reference it, and
nothing here is active unless you install it on purpose. Hooks can block tool calls, so read
this page before you enable it.

## What it contains

| Hook | Event | What it does |
| --- | --- | --- |
| `bash-destructive-guard` | PreToolUse (Bash) | Best-effort tripwire against accidental destructive commands (not a security boundary). Denies catastrophic commands (wipe root/home, format a block device, fork bomb), recursive deletes of protected paths, and reads of secret material. Asks for confirmation on recoverable-but-costly commands (force-push, `git reset --hard`, `DROP TABLE`, `kubectl delete`, ...). |
| `no-code-guard` | PreToolUse (Edit/Write) | Denies code-file writes from models listed in `no_code_models`. |
| `edit-budget-gate` | PreToolUse (Edit/Write) | Denies the main session's code edits beyond a per-model budget (`edit_budget_models`) and asks it to delegate. |
| `review-stop-gate` | Stop | Blocks the stop once if a UI file or security-sensitive code was edited and the matching review agent never ran. |
| `clean-state-check` | Stop | In projects with a plan tracker, nudges once to update its `## Resume here` block. |
| `resume-inject` | SessionStart | Loads the active tracker's `## Resume here` block (and a short program-overview file in the plans folder, if present) as fenced, untrusted context. |

## Requirements

- `python3` (3.8+, standard library only) and `git`. There is no `jq` dependency.
- `bash-destructive-guard` **fails closed** when `python3` is missing: every Bash call is denied
  with a "guard error" message until you install `python3` or uninstall the plugin. The other
  hooks cannot tell what they are looking at without `python3`, so for them a missing
  interpreter is a hook error that Claude Code treats as non-blocking.

## Install (explicit choice)

From the marketplace, in one step:

```text
/plugin install ai-architecture-gates --marketplace nik110403/ai-architecture
```

or, after `/plugin marketplace add nik110403/ai-architecture`, `/plugin install ai-architecture-gates@ai-architecture`.

Or from a local clone:

```bash
claude --plugin-dir /path/to/ai-architecture/plugins/gates     # try it for one session
claude plugin validate --strict /path/to/ai-architecture/plugins/gates
```

Hooks use `${CLAUDE_PLUGIN_ROOT}`; nothing is copied into your own settings. Start with the
destructive-command guard alone if you prefer: it is the only hook that is useful with no
configuration. The model gates are inert until you configure them.

## Configuration

`defaults.json` in the plugin root is the baseline. Your overrides live in **user scope only**:

1. the file named by `$AIARCH_CONFIG`, else
2. `~/.config/ai-architecture/gates.json`.

A configuration file inside a project (`.claude/`, the repo root, or anywhere else) is **never
read** for these settings, so a repository you open cannot loosen a gate. Lists in your file are
added to the defaults (you cannot remove a default entry from your file); dictionary values replace
per key. A missing file means defaults. A file that exists but cannot be parsed, is not a JSON object, or has
a known key of the wrong type, or carries an **unknown top-level key** (keys starting with `_` are comments)
is **not** ignored: the baseline stays in force, every hook reports
`ai-architecture-gates: config <path> invalid: ... fix it` (as a `systemMessage` in its reply, and on stderr), and the
destructive guard denies Bash calls until the file is fixed. The config path must be a regular file: a FIFO,
device or `/dev/stdin` is refused as invalid. Every hook reads its input payload first and fails closed
when the payload cannot be parsed (pre-tool hooks deny, Stop hooks block; the SessionStart hook can only warn). `$AIARCH_CONFIG` is ignored when it points inside the current
project directory (a project-level `<project>/.claude/settings.json` can set environment variables); the default
user path is used instead. The home directory itself comes from the account database (`pwd`), not from `$HOME`, so a
project-set `HOME` cannot move the protected roots or hide your user config.

What this does and does not cover: the rule is about this plugin's own config files. Claude Code settings are outside the plugin's control: a project's `<project>/.claude/settings.json` can still set `disableAllHooks`, `enabledPlugins: false`, or `env` values such as `CLAUDE_HOOK_STATE_DIR`.

Claude Code lets a call through when a hook times out. Every gate hook therefore runs through `hooks/run-hook.sh`, which holds wall-clock caps (coreutils `timeout`: stdin copy, then the guard; about 8.2 s worst case in total, inside the 10 s hook timeout) that CPU starvation cannot defeat: expiry, a crash, non-JSON output, or a missing python3/timeout is a deny (pre-tool), a block (Stop; silent when `stop_hook_active`) or a warning (SessionStart). The destructive guard also enforces its own caps, as a secondary layer, and denies when one is hit:
a command longer than 8192 bytes is denied before any scanning; the protected-path and secret-read scan has a 3 s wall-clock cap (hitting it is a deny, not an allow); newline runs are collapsed before matching, and a secondary process-CPU-time deadline of 6 s (not wall-clock; the wrapper's wall-clock caps are the primary bound) also ends in a deny;
project and configured regexes run only AFTER the built-in, protected-path and secret checks, at most 32 project patterns are accepted (more is a deny with a config error), and all
project/configured regex matching shares a total budget of about 1 s (exhausted is a deny).

The one project-level file the destructive guard reads is `<project>/.claude/destructive-patterns.json`:
`{"deny": ["regex"], "ask": ["regex"]}`. It can only **add** rules. Patterns that are too long or have
nested quantifiers are skipped, and each pattern has a 0.2 s match budget inside the shared 1 s total.

| Key | Meaning |
| --- | --- |
| `protected_roots` | Directories whose deletion (and an ancestor's) is denied. `~` is expanded. |
| `protected_containers` | Directories whose direct children are protected (each project, repository or history folder under them). |
| `protected_names` | Folder names protected wherever they appear. |
| `deny_patterns`, `ask_patterns` | Extra regexes for the destructive guard. |
| `edit_budget_models` | `{ "model-substring": max_code_edits }` per session, for the main session. The next edit is denied. |
| `no_code_models` | List of model substrings whose sessions may not write code files. |
| `override_phrase` | Phrase that, typed by the user in a real message, lifts the edit budget (default `override: inline`). |
| `delegate_role`, `ui_delegate_role` | Agent named in the denial message. |
| `builder_roles`, `ui_builder_roles` | Agents whose dispatch counts as "code was written" for the Stop hooks. |
| `design_review_role`, `security_review_role` | Agents `review-stop-gate` waits for. |
| `trusted_plugin_prefixes` | Plugin namespaces trusted by the Stop hooks (default `["ai-architecture"]`): only `<prefix>:<role>` or a bare `<role>` counts as a reviewer or builder. |

Always-on floor, not removable by config: `~/.ssh`, `~/.gnupg`, `~/.password-store`, the standard
credential files of common tools, `.env*`, `*.pem` and private keys.

Role and agent names match after the last `:`, so `ai-architecture:verifier` equals `verifier`. The review
and clean-state Stop hooks only accept a bare name or a namespace in `trusted_plugin_prefixes`.

**Removed keys.** The keys `readonly_agents` and `sandbox_run_paths` were removed and are now rejected as
unknown keys. If your own `~/.config/ai-architecture/gates.json` still contains either of them, the destructive guard denies every
Bash call until you delete them (see the invalid-config rule above).

Example `~/.config/ai-architecture/gates.json`:

```json
{
  "protected_roots": ["~/documents", "~/code"],
  "protected_containers": ["~/code"],
  "protected_names": ["documents"],
  "edit_budget_models": {"large-model": 4},
  "no_code_models": ["orchestrator-model"]
}
```

## Behaviour you should know about

- **Best-effort tripwire, not a security boundary.** The destructive guard pattern-matches bash text to catch accidental destructive commands; it is not a defence against an adversarial agent, and it cannot parse every bash form. The guards read the literal command string. Obfuscation and indirect
  execution can get past static text analysis. Where a destructive or secret-reading command touches
  something the guard cannot resolve (variables, command substitution, globs in directory
  components) in a **delete**, it denies rather than guessing. Secret **reads** through shell variables
  (for example `X=~/.ssh; cat $X/<key file>`) are NOT caught: the guard does not resolve variables. For real isolation use OS-level sandboxing as well.
- **Accepted false positives.** A quoted non-shell argument (a commit message, echoed text) that spells
  out a blocked command is denied too. Heredoc bodies are skipped only when the heredoc is a plain
  `cat`/`tee` file sink.
- **Fail-closed vs fail-open.** Any exception inside the destructive guard's checks, and an
  undetermined model while `no_code_models` is set, deny. So does a hook payload that cannot be parsed
  (every hook once its payload is read; a failure while importing, before that point, is NOT covered; the SessionStart hook can only warn). A missing transcript for the edit budget lets the call through. An uncaught exception in any Python hook is answered by a
  shared crash guard: pre-tool hooks deny, Stop hooks block once, the SessionStart hook warns (an unwritable state directory therefore denies, instead of exiting 1, which Claude Code treats as allow).
- **Edit counter state** is kept under `$CLAUDE_HOOK_STATE_DIR`, else `$CLAUDE_PLUGIN_DATA`, else
  `~/.cache/ai-architecture/gates/`.
- **Bash writes are not covered** by the code-edit gates (they watch the Edit/Write tools only).

## Known limitations

Known limits of the destructive guard (short list):

- Secret reads through shell variables are not caught (the guard does not resolve variables).
- Constructs it cannot parse are denied or may be missed (for example unusual heredoc or line-continuation forms; a heredoc opener line ending in a backslash is denied outright).
- Wall-clock limit of about 8.2 s per hook call: under very heavy CPU load, long commands may be denied and Stop hooks may block.
- A project's Claude Code settings can disable hooks (`disableAllHooks`, `enabledPlugins: false`).

- **Payload shapes are not type-checked.** Only the step that parses the hook payload is checked. A
  payload that parses as JSON but has the wrong shape (for example `tool_input` as a string, `cwd` as a
  number, or `command` as a list) can make a hook raise; the crash guard then answers deny (pre-tool) or block (Stop) instead of exiting 1.
  Claude Code does not send these shapes today.

## Described, not shipped: read-only reviewer seats

An earlier version of this plugin tried to make reviewer agents read-only by allowing only a list of
read commands in Bash. That is not shipped: a guard that parses shell text cannot make an agent
read-only. Newline and `\r\n` separators, `|&`, nested `$( $( ) )`, `sed '1e cmd'` and `s///e` all got
past it. The recommended mechanism is to give reviewer agents no Bash tool at all: set the agent's
`tools:` allowlist to `Read, Grep, Glob`.

## Transcript-format fragility

`edit-budget-gate`, `no-code-guard`, `review-stop-gate` and `clean-state-check` learn the active model
and what the session did by reading the session transcript (`transcript_path`, a JSONL file). That
file's layout is an implementation detail of Claude Code and has changed between versions: the
assistant-line `message.model` field, the `isSidechain`/`isMeta`/`isCompactSummary` flags,
`tool_use` / `tool_result` pairing by id, and the `toolDenialKind` marker may move or disappear.
What happens then:

- model not found: `no-code-guard` denies (fail closed, only when `no_code_models` is set), including for a subagent call whose sidechain model cannot be found.
  Subagent detection assumes the subagent's (`isSidechain`) assistant lines appear in the MAIN transcript file; if a Claude Code version writes them elsewhere, subagent writes are denied whenever `no_code_models` is set;
  `edit-budget-gate` allows (it cannot tell which model is running);
- tool calls not found: `review-stop-gate` and `clean-state-check` see no edits and stay quiet.

After a Claude Code upgrade, run the test script described below (it replays recorded payloads and
transcript fixtures) and re-record the fixtures if the format changed.

## Tests

```bash
HOME=$(mktemp -d) bash plugins/gates/tests/run.sh
```

Runs offline against recorded hook payloads and transcripts with an empty `HOME`: the destructive
guard fixture table, each model/Stop/SessionStart gate, and the
configuration-scope checks (including a project-scope file that tries to disable the guard).

## Provenance and licence

Each hook's origin, source hash and deltas are in [hooks/PROVENANCE.md](hooks/PROVENANCE.md).
Licence notes, including the origin of the confirmation tier's pattern list, are in
[LICENSES.md](LICENSES.md).
