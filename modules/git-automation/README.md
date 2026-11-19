# git-automation

Registry-based commit, follow and approved push for private Git repositories. Stdlib only; the only network
use is what the registered remote URL needs, and git may use only the `file`, `ssh` and `https` transports
(`GIT_ALLOW_PROTOCOL=file:ssh:https`; never `git://` or `ext::`). Narrow it per repo with the optional registry
field `allow_protocols`, a colon-joined subset such as `"file"`. No model. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json` (no live evidence yet).

```
python3 -B modules/git-automation/git_auto.py validate --registry R
python3 -B modules/git-automation/git_auto.py status   --registry R [NAME]
python3 -B modules/git-automation/git_auto.py run      --registry R --host H [--apply] [NAME]
python3 -B modules/git-automation/git_auto.py follow   --registry R [--host H] [--apply] [NAME]
python3 -B modules/git-automation/git_auto.py push NAME --registry R --approve-head <40-hex HEAD>
python3 -B modules/git-automation/git_auto.py import --from-steward STEWARD_REGISTRY --host H [--out R]
python3 -B modules/git-automation/git_auto.py unit   --registry R --host H     # prints a unit; installs nothing
```

Registry: `{"schema_version":1,"committer_host":H,"repos":[{"name","path","mode":"commit|follow-only|off","visibility":"private|public","remote"}]}`.
`visibility` is required (a missing value exits 2). `remote` must equal `git remote get-url origin` exactly
(optional `remote_name` replaces `origin`), otherwise `run`, `follow` and `push` exit 3 `remote mismatch`.

- Single committer. `run --apply --host H` works only when `H` equals `committer_host` (exit 3 otherwise). It stages the
  changed non-ignored files, commits `autopilot: N files` and pushes the branch with a plain `git push`.
- Status and dry-run are the default. `status` and `run` without `--apply` use `git ls-remote` only; with `--apply`
  a non-forced `fetch` into `refs/remotes/*` is the only other ref change.
- `run` stops (exit 1, nothing committed) on: a merge, rebase or cherry-pick in progress, a detached HEAD, conflict
  markers, an unreachable remote, a remote that is behind/diverged/rewritten, and any changed file named `.env`
  (except `.env.example`) or holding a private key block. It never merges, rebases or pulls.
- `follow --apply` fetches and runs `git merge --ff-only`. It refuses (exit 1, clone unchanged) when the clone has
  unpushed commits, when the histories diverged, or when local changes overlap incoming files.
- A `public` repository is never pushed by `run` (`public: push requires --approve-head`). `push NAME` needs the
  exact current HEAD in `--approve-head` and a typed repo name read from the controlling terminal; without a terminal
  it exits 3. The flag guards against stale approvals; it is not a boundary against an agent that can read `HEAD`
  and a terminal. Generated units and the steward import never contain `push`.
- `import --from-steward` lists the stewardship roots as `mode: off`, `visibility: public`; edit the registry to
  opt a repo in. The stewardship registry is only read.
- Every git call goes through one function. It refuses `--force`, `-f`, `--force-with-lease`, `--mirror`, `--all`,
  `--delete`, `--tags`, `+refspec`, `reset --hard`, `clean`, `checkout --`, `restore`, `stash drop`, `branch -D`,
  `rebase` and `pull` with exit 4 before spawning, and drops every `GIT_*` variable of the caller (so
  `GIT_CONFIG_*`, `GIT_DIR` and `GIT_SSH*` cannot redirect a push), then pins `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_TERMINAL_PROMPT=0` and `GIT_ALLOW_PROTOCOL`. The registry URL must be the only fetch and push URL of the
  remote (`remote get-url --all` and `--push --all`), else exit 3. `push` pushes the approved SHA
  (`<sha>:refs/heads/<branch>`), re-reads HEAD after the prompt and refuses if it moved.
