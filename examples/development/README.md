# development example

```bash
python3 -B examples/development/run.py --offline [--out DIR] [--skills-dir DIR]
python3 -B modules/development/development.py verify-receipt "$OUT/runs/<id>/receipt.json"
```

`--offline` is required. The default output directory is `${XDG_STATE_HOME:-~/.local/state}/ai-architecture/development`; each run writes `runs/<id>/` (repos, dispatch job files, worktrees, five artifact files and a receipt) and a latest pointer file. The directory is created 0700; a symlinked or foreign-owned `--out` is refused. Git is required, because the write roles run in `git worktree`s.

Five journeys run through `../../modules/orchestration/dispatch.py run`, one dispatch job per role step:

| Journey | Jobs (role) | Fixture |
|---|---|---|
| debug | builder (reproduce, red commit, green commit), verifier in a fresh worktree of the builder HEAD | `fixtures/bug` |
| domain | surveyor (finding), refuter (echoes the finding sha) | `fixtures/domain` |
| secure-build | builder (adds the guards), security-reviewer | `fixtures/secure` |
| security-audit | surveyor (map), one refuter per finding | `fixtures/audit` |
| incident | surveyor (facts), tribune (ranking), builder per change | `fixtures/incident` |

Every seat is a `local-command` seat with an absolute `sys.executable`; the workers (`workers/*.py`) read fixtures only. Neither this directory nor `modules/development` imports a network module or starts `claude`, `opencode`, `curl` or `wget`. All git calls run with no global or system config, no hooks, no signing and pinned author and dates, so artifacts are reproducible (`stable_sha256` is equal across runs; it excludes only job ids, pids, timestamps and the worktree branch).

Without `--skills-dir`, one `DEGRADED: <journey> upstream <id> unavailable` line is printed per journey and every artifact records `upstream: not exercised`.

## Read this before trusting a pass

- The workers are authored stand-ins; no model judgement is exercised. The run shows that the contracts hold and that the checker catches broken evidence, not that a model reviewed well.
- The `local-command` seat is not a sandbox (see the dispatch README threat model).
- The fixtures are intentionally vulnerable (an unguarded file server, a SQL-injectable app, a service with a bad cache setting); never import them. Fixture symlinks are created at runtime in temp directories, never committed.
- A receipt is evidence against mistakes, not against a same-user forger.
