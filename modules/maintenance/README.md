# maintenance

A machine-readable loop registry with a health report that works without any scheduler, optional
scheduler adapters (`systemd-user`, `cron`), and deterministic changelog and retirement helpers.
Stdlib only; no network, no model. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json` (no live evidence yet).

```
python3 -B modules/maintenance/maintenance.py validate --registry loops.json
python3 -B modules/maintenance/maintenance.py health   --registry loops.json [--host H] [--settings F] [--format json]
python3 -B modules/maintenance/maintenance.py import   --from LOOPS.md [--out loops.json]
python3 -B modules/maintenance/maintenance.py scheduler observe [--format json]
python3 -B modules/maintenance/maintenance.py scheduler plan|apply --registry loops.json --loop ID \
    [--adapter systemd-user --unit-dir DIR | --adapter cron --crontab-file FILE] --calendar C --exec CMD [--enable]
python3 -B modules/maintenance/maintenance.py scheduler report|rollback (--unit-dir DIR | --crontab-file FILE)
python3 -B modules/maintenance/maintenance.py changelog --repo GIT_REPO --since YYYY-MM-DD --out FILE
python3 -B modules/maintenance/maintenance.py retire --registry loops.json --loop ID --reason TEXT [--date D] [--apply]
```

Registry (`<loops.json>`): `{"schema_version":1,"loops":[{"id","hosts":["any"],"checks":[{"kind":"timer|service|hook|file|none","target"}],"status":"live|dead|retired","retired":{"date","reason"}}]}`.
Duplicate keys, unknown check kinds and unknown statuses exit 2, and so does a `timer`/`service` target that is not a plain unit name (`NAME.timer` or `NAME.service`).

- `health` exits 1 when a loop is `dead` or a check is `missing`. A retired loop is listed, never a violation.
  Timer and service checks report `not-observable: no systemd user manager` when `systemctl --user show-environment`
  does not answer; that is not a failure.
- Writers: `scheduler apply`, `scheduler rollback`, `retire --apply` and `changelog --out` only, each limited to the
  path you name. Everything else is read-only. `plan` and `retire` without `--apply` write nothing.
- `scheduler apply` writes unit files only into `--unit-dir` (or a crontab-format file into `--crontab-file`) and records
  `<unit-dir>/.aiarch-manifest.json`. `rollback` removes exactly the files in that manifest. systemctl verbs are
  allowlisted in code (`show-environment`, `show`, `list-timers`, `daemon-reload`, and `enable` only with `--enable`).
  Unit names are validated and always follow `--` in the spawned command; manifest file names must be unit
  names without `/` or `..`.
  The cron adapter only ever runs `crontab -l`; it prints the install command and leaves the install to you.
  Windows and macOS are reported `unsupported` (no Task Scheduler or launchd adapter).
- `import` converts a loop-sync Markdown table; `tools/loop-sync` stays the Markdown entry point.
- `changelog` drafts a week-by-week list from `git log`, drops `autopilot:` subjects, and refuses an `--out` inside the repo.
- `retire` shows a diff by default; `--apply` writes atomically and keeps `<registry>.bak`.
