# productivity

Mail, calendar and drive interfaces behind explicit task authority, a read-only morning brief, and a
memory-import preview/apply/rollback. Stdlib only, offline, no credential storage. It shares connector
handling with the `documents` module.

## Commands

    python3 modules/productivity/productivity.py connector status --connectors C --name N --allow-connector
    python3 modules/productivity/productivity.py mail draft --payload P --out FILE
    python3 modules/productivity/productivity.py mail send --target ID --payload P --store S --connector N \
        --allow-side-effects --authority A.json [--connectors C --allow-connector]
    python3 modules/productivity/productivity.py calendar add|move|delete ... [--base-revision R]
    python3 modules/productivity/productivity.py drive upload|share ...
    python3 modules/productivity/productivity.py morning --store S --date YYYY-MM-DD --out FILE
    python3 modules/productivity/productivity.py import preview --from X.json --memory-dir M
    python3 modules/productivity/productivity.py import apply --from X.json --memory-dir M --allow-write [--replace]
    python3 modules/productivity/productivity.py import rollback --memory-dir M

Every command takes `--format text|json`. Exit codes: 0 ok, 2 refused or invalid input, 3 revision
conflict, existing target or nothing to restore, 4 connector unavailable, operation not offered or
authority already used. No command has a `--force`, `--yes` or confirmation-bypass option.

## Side effects and authority

Each of `mail send`, `calendar add|move|delete` and `drive upload|share` needs `--allow-side-effects` and an
authority file:

    {"action": "mail.send", "target": "draft-1", "payload_sha256": "<hex>", "expires": "2030-01-01T00:00:00Z",
     "authority_sha256": "<hash of the other four fields>"}

The authority must match the request exactly (action, target, hash of the payload bytes), be unexpired, and
is single use: a claim under `$XDG_STATE_HOME/ai-architecture/productivity/used/` is made before the side
effect and released if nothing happened (a stale revision, an unavailable connector). `calendar move` and
`calendar delete` also need `--base-revision`; a stale one exits 3.

The authority file proves binding, expiry and single use, not that a human issued it. Whatever can write
the file can seal one, so every receipt carries `authority_origin: "unverified"`. For `mail send` and
`drive share` the `target` is the draft or file id; recipients live only in the payload, which
`payload_sha256` binds. Authority files, receipts and connector files never hold addresses. Receipts carry
`payload_sha256`, never the body.

The built-in `sim` store records ids and hashes, not message bodies. An external connector is declared,
observed with `--describe` and spawned only with `--allow-connector`, exactly as in `documents`.

## Morning brief

`morning` reads the store and writes a brief: events for the date in time order, the unread count and the
unread subjects, tasks due on or before the date. Mail text is JSON-quoted and placed in a quote block with
a line saying it is data. The command never writes the store and has no send path, so a mail that tells the
assistant to send or delete something changes nothing.

## Memory import

The import file is `{"version": 1, "files": {"name.md": "text", "sub/other.txt": "text"}}`. Names are relative,
end in `.md` or `.txt` and cannot leave the directory. The file is refused (exit 2, memory directory
untouched) for credential-looking keys, token-shaped values, email addresses, home-directory paths,
duplicate JSON keys or more than 256 KB.

`preview` writes nothing. `apply` needs `--allow-write`, backs up files it will overwrite into
`.import-backup/` first, then writes atomically; a conflicting file stays untouched unless `--replace`. A
second `apply` before a `rollback` exits 3. `rollback` deletes the files `apply` created, restores the
overwritten ones and removes the backup, so the tree matches its state before the first apply; with no
backup it exits 3.

## Limits

The credential and token checks are pattern based, not a secret scanner. This module never contacts a
provider itself.
