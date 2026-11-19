# Integration, status, removal

Install: copy `modules/memory-operations/` together with `modules/memory-backends/`, `modules/memory/` and
`tools/memcheck/`. Nothing else is needed and nothing is installed or wired for you.

Typical flow on one machine:

```bash
R=~/.local/state/ai-arch-ops                              # your receipt directory (0700, created on first use)
python3 -B modules/memory-operations/memops.py snapshot freeze --out "$R" --store ~/.local/share/ai-arch-observations
python3 -B modules/memory-operations/memops.py sync merge --out "$R" --target STORE_A --replica STORE_B          # plan
python3 -B modules/memory-operations/memops.py sync merge --out "$R" --target STORE_A --replica STORE_B --apply
```

Between hosts, move a replica yourself, for example `scp` of a file produced by `sync backup` into `R/sync/backups/`
(prose only: this module ships no remote transfer). Merge it with `--replica <that file>`.

Hooks and schedules are yours to add. Nothing in this module starts or stops a process, so stop your own capture hooks if
you want a merge that drops no captures.

Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json`, not tested in a live session. Rollback: promotion with `promote rollback`, a merge with `sync recover` or
`sync restore --backup ... --expect-sha ...`. Removal: delete `modules/memory-operations/` and the receipt directory you chose.
