# Integration, status, removal

Install: copy `modules/git-automation/`; add `modules/stewardship/` next to it only if you want `import --from-steward`.
Keep the registry outside your source tree and give each host its own copy: the committer host lists repos as `commit`,
every other host lists them as `follow-only`.

A scheduler is optional. `git_auto.py unit` prints a service and timer that run `run --apply` only; install them
yourself (for example with `modules/maintenance`). No unit ever pushes a public repository.

Status: `claimed_support` is `planned` in `descriptor.json`; this module has no recorded live evidence.

Removal: delete `modules/git-automation/` and your registry. Commits it made are ordinary commits.
