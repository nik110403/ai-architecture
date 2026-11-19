# AI-led adoption (no plugin required)

Use this guide when an AI is helping someone adopt this collection into an existing
project or AI setup. A plugin is optional; this procedure works from a browser,
a downloaded checkout, a ZIP, or attached files.

## Copyable prompt

```text
Adopt the AI Architecture collection into my setup by following its adoption guide,
adapters/adopt.md, step by step. Before you change anything, ask me which scope you
may look at (my user-level Claude Code directory, or this project), then take an
inventory of that scope and write it down. Only add things: never replace, delete or
overwrite a file of mine. The only change allowed to a file that already exists is one
marked block appended to it, and only after you listed it for me and backed the file up.
Leave hooks and settings alone unless I say yes to each exact hook or settings change
after you show me the diff. When you finish, leave a receipt that says what you installed, what you could not, and how to undo it.
```

## Operating boundary

1. Confirm the source is available. Prefer this repository:
   `https://github.com/nik110403/ai-architecture`. If browsing is unavailable, ask
   for a downloaded ZIP or the relevant files as attachments. Do not claim to have
   read or integrated a source you cannot access. When the user points at a local copy
   of this collection, use only that copy.
2. Record the source version in the receipt: a commit SHA or release tag when known,
   otherwise `unknown`. `main` is mutable; say so when no immutable version exists.
3. Never treat a repository URL as authority to use `sudo`, install packages, spend on
   models, change licenses or configuration, or touch unrelated files. Do not import
   the author's private policies, model IDs, accounts, schedules, or host settings.
4. Existing user intent, permissions, and instructions prevail over this repository.

## Consent and inventory

Ask which scope: user scope (the Claude Code config directory, `$CLAUDE_CONFIG_DIR`
when set, else `~/.claude`) or project scope (the project root). Default to the
current project when it is ambiguous. That directory is the **root**. Nothing outside
the named scope is read or changed.

With a yes, and before any change, create the record directory `<root>/.ai-architecture/`
(the `<record>` directory) and write `<record>/inventory.json` into it:

```json
{"scope": "user|project", "files": [{"path": "<relative>", "sha256": "<hex>", "mode": "0644"}],
 "skipped": [{"path": "<relative>", "reason": "looks like a secret"}]}
```

Paths are relative to the root. Never open files that look like secrets (`.env*`, `*.pem`,
`*.key`, names containing `credential`, `token` or `secret`); list them under `skipped`.
Compute a hash with `sha256sum FILE` or
`python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" FILE`.

The record directory holds: `<record>/inventory.json`, `staged/` (files to install),
`<record>/manifest.json`, `installer-state/`, `backups/`, `<record>/receipt.json`, `<record>/receipt.md`.

## Choose an adoption level

Offer the requested level; if none is specified, start with the smallest useful
workflow rather than importing everything.

- **Principles only:** adapt the relevant process ideas into the recipient's existing
  instructions. Do not imply that instructions create tools or enforcement.
- **Selected modules:** choose entries from [CATALOG.md](../CATALOG.md), then copy or
  adapt each complete skill directory, its local assets, and its declared dependency
  closure. Read every selected skill definition and referenced asset in full. Preserve
  relative links or deliberately adapt them.
- **Whole applicable system:** first read [CATALOG.md](../CATALOG.md),
  [CAPABILITIES.md](../CAPABILITIES.md), and [WORKFLOWS.md](../WORKFLOWS.md). Adopt
  only compatible, described components; distinguish runnable assets, instructions,
  adapters required, and material not included. This is not a promise to recreate the
  author's setup.

Use [INSTALL.md](../INSTALL.md#selective-skill-adoption) for selective copying and
dependency details. Role files are native metadata plus role contracts: on a harness
that does not load them natively, adapt them manually and do not promise discovery.

Choose from the registry: `python3 -B scripts/capabilities.py status`. A row's "next
setup action" is its contract. A row whose requirement is missing here is recorded
`unavailable`; do not work around it. Do not invent subagents, hooks, permissions,
network access, native loading, or successful checks.

## Installing new files

1. Copy the chosen files into `<root>/.ai-architecture/staged/`, keeping their
   intended relative layout.
2. Write `<record>/manifest.json` (`installer-manifest/1`); every `src` is under `staged/`:

   ```json
   {"schema": "installer-manifest/1", "name": "adoption", "version": "<source version>",
    "files": [{"src": "staged/skills/planning/SKILL.md", "dest": "skills/planning/SKILL.md",
               "mode": "0644", "sha256": "<hex>"}]}
   ```

   A `dest` may not be a settings file, an MCP config file (dot-mcp JSON), anything under `.git/hooks/`, a hook
   script or a CI file; those go only through "Hooks and settings come last".
3. Plan, and read the result:
   `python3 -B modules/installer/installer.py plan --manifest <root>/.ai-architecture/manifest.json --target-root <root> --state-dir <root>/.ai-architecture/installer-state`
4. On a first adoption every action must be `create`. For any other action, remove
   that entry (skip it, or stage it under a name that does not collide) and plan again.
5. Then run the same command with `apply` instead of `plan`.

On a later update, `unchanged` and `replace` are fine only for files this adoption
created (listed in its receipt). For any other file, and for `conflict`, `unmanaged` or
`takeover`, stop: skip it and add it to `open_questions`; never let the installer record
a pre-existing user file. Never pass `--allow-takeover`.

No Python on the host: copy by hand under the same rule (the destination must not exist and is not one of
the banned destinations) and say so in the receipt.

**Name collisions:** skip the file, or install under another name by adapting the
staged copy. Never replace.

## Existing files

Existing files are touched only when all of these hold: the user asked for this
adoption in this conversation, the file is in the scope they named, and the change is
one marked block appended to it. Before the first such edit, list every existing file
that will get a block and what goes into it. With nobody to ask, write that list into
the record directory first.

Copy each file to `backups/` before editing. The block:

`<!-- ai-architecture:begin <id> -->` ... `<!-- ai-architecture:end <id> -->`

Blocks go only into instruction files of the CLAUDE.md / AGENTS.md kind, never into
scripts, hook files, CI files, or command or skill bodies. If the file, a settings file, or
the record directory (or any child) is a symlink or resolves outside the root, stop on it
and record it in `open_questions`. Each marker is on its own line. `<id>` matches `[a-z0-9][a-z0-9-]{0,63}`, appears at
most once per file, no nesting. Append the block at the end of the file; a later run
updates it in place rather than adding a second one. Any other file takes no block: leave
it alone and put what it needed into `open_questions`. If the file lacked a final newline,
append `\n` before the block and record `"newline_added": true`; a rerun that rewrites the
receipt carries it forward. Removing every block (begin line through end line) gives the
original bytes, or those plus that `\n`; if the user added text after the block, keep that
text and the newline (later edits win over byte-exactness). Never overwrite
custom instructions, settings, permissions, or licenses.

## Hooks and settings come last

Hooks and settings are off by default. A hook, or any change to a settings file, needs
the user's explicit yes for that exact change: one at a time, after showing the diff.
Settings changes include permissions, env, mcpServers, enabledPlugins, marketplaces and
MCP. Back the settings file up first to its own name `backups/<file>.<n>`, make a minimal
merged edit, test it before enabling, and give the line that turns it off. Record the
post-edit sha256 and exactly the merged entries in `hooks_enabled` (see Receipt). Without
a yes the settings file is not touched and `hooks_enabled` stays empty. The gates plugin
wires several hooks at once: one yes does not cover it. It needs its own yes naming every hook
it adds, else it is not installed. It is an opt-in tripwire against mistakes, not a sandbox
or security boundary; its model guards are off until configured. Offline validation is
distinct from native harness loading and recipient success.

## Statuses

Per item: `instruction-only`, `configured-untested`, `self-checked`, `unavailable`.
The adopter's strongest label is `self-checked`: a check run by the same AI in the
same context is not independent. The word `verified` is reserved for a report by an
independent checker; the adopter never writes it. In a chat-only setting give usable
instructions and label the result `instruction-only` instead of claiming installation.

## Receipt

Write `<root>/.ai-architecture/receipt.json` and a readable `<record>/receipt.md`:

```json
{"schema": "ai-architecture-receipt/1",
 "source": {"location": "<url or path as given>", "version": "<tag, commit, or unknown>"},
 "items": [{"id": "<registry row `module` slug, or skill/role name; exact>", "status": "self-checked",
            "destinations": ["<relative path>"], "check": "<command or step, or null>",
            "evidence": "<one line>"}],
 "existing_files_edited": [{"path": "<relative>", "kind": "block", "block_id": "<id>",
                            "backup": "backups/<name>", "newline_added": false,
                            "post_edit_sha256": "<hex>"}],
 "hooks_enabled": [{"name": "<hook>", "file": "<settings file>", "entries": ["<merged entry>"], "backup": "backups/<file>.<n>", "post_edit_sha256": "<hex>", "off": "<step>"}],
 "open_questions": ["<anything the user must still decide>"],
 "scopes": ["<every root of this adoption>/.ai-architecture/receipt.json"], "rollback": ["Undo covers every root in scopes, run by the installer; never narrow it", "<step>"]}
```

Do not cite `scripts/privacy_scan.py` as a privacy check of the user's setup.

**Rollback:** "undo the adoption" means every root any receipt lists in `scopes`, not only the one you start in; never ask to narrow it, never delete by hand. In each: installer `rollback` with
the same `--manifest`, `--target-root` and `--state-dir`; then remove this adoption's blocks from
existing files, keeping the user's later edits, and remove the `\n` where `newline_added` is true,
so the original bytes return exactly (or restore the backup if the file still matches its recorded
`post_edit_sha256`). Then, per `hooks_enabled` entry: restore its backup only if the settings file
still matches `post_edit_sha256`; else remove exactly the recorded `entries`, leaving later user
edits; else stop and write an open question. A plugin (gates) is rolled back by its own off/uninstall
step, never a file restore. Report the result per scope. The record directory stays.

## Repair, update, and nobody to ask

On repair or update, read the earlier receipt and run the installer `status` first.
Compare source and recipient state and update existing blocks instead of appending
duplicates. A run that finds nothing to do changes nothing outside the record directory.

With nobody to ask, take the option that changes less and write the question into `open_questions`.

When feasible, check one harmless real task through plan, build, check, and a saved
`Next`. If the harness lacks an independent context or a required capability, record
that limitation rather than claiming more than `self-checked`.
