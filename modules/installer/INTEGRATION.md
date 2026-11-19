# Integration, status, removal

Install: copy `modules/installer/`. It depends only on the core contracts and Python.

Use: write a manifest next to a `files/` directory, run `plan`, read it, then `apply` with `--target-root` set to a directory you
own. Keep the state directory (default `<target-root>/.installer/<name>`) so `status`, `rollback` and `recover` work later.

Status: `claimed_support` is `planned` in [descriptor.json](descriptor.json).

Removal: `rollback` first if you want the installed files reverted, then delete `modules/installer/` and the state directory.
