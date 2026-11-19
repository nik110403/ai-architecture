# installer

A small, generic, manifest-driven install primitive: install, upgrade, roll back and recover a set of files under a target root
you name. Stdlib only, Python 3.9 or later. Descriptor claim: `planned` (see [descriptor.json](descriptor.json)); per-capability state is in `../../capabilities/registry.json`. It is not a package manager
and has no default target: `--target-root` is required, and nothing is ever written to your home directory implicitly.

```
python3 -B modules/installer/installer.py plan|apply|status|rollback|recover --manifest M --target-root R [--state-dir S] [--allow-takeover]
```

The manifest ([schema](schemas/manifest.json), [example](example/manifest.json)) lists `name`, `version` and files, each with a
source, a destination relative to the root, a mode and a sha256.

- `plan` and `status` write nothing.
- `apply` writes a journal first, backs up every file it will replace, writes atomically, and records the installed hashes.
  Applying the same manifest again is a no-op ("no change", exit 0). A target whose bytes differ from both the recorded installed
  hash and the new one is a user change: exit 1, left byte-for-byte as it was.
- A destination that already exists, was never recorded by this installer and differs from the manifest in bytes or mode is
  `unmanaged`: `plan` says so and `apply` refuses (exit 1, `unmanaged-file`) before writing anything, so even files it could have
  created stay uncreated. `--allow-takeover` on `plan` and `apply` turns those entries into `takeover` (backup, replace; `rollback`
  restores bytes and mode). The flag is never stored; the next run without it refuses again. An identical unrecorded file stays
  `unchanged` and `rollback` leaves it in place.
- `rollback` restores the backups (bytes and mode) and removes the files this installer created.
- `recover` finishes or reverts an interrupted transaction from the journal.
- Refused with exit 2: `..`, an absolute destination, a symlinked path component, a foreign owner, a destination outside the root,
  a source hash mismatch.

[PATTERNS.md](PATTERNS.md) maps the historical installer kinds onto this primitive and names what is deliberately left out.

## Tests

`python3 -B -m unittest discover -s modules/installer/tests`. Everything runs under a temporary root and a temporary HOME. A crash
seam (`AIARCH_INSTALLER_CRASH_AFTER`) ends the process mid-transaction so `recover` is tested on real interrupted state.
