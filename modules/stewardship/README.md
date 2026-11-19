# stewardship

Bootstrap, register, verify, resurvey and lint for projects you name explicitly. Stdlib only; it never
scans your home directory, uses the network, or starts a model. Descriptor claim: `planned`; per-capability state is in `../../capabilities/registry.json` (no live evidence yet).

```
python3 -B modules/stewardship/steward.py bootstrap --project <project> --registry <registry.json> [--topic main]
python3 -B modules/stewardship/steward.py register  --project <project> --registry <registry.json>
python3 -B modules/stewardship/steward.py verify    --project <project> --registry <registry.json>
python3 -B modules/stewardship/steward.py resurvey  --registry <registry.json> [--format json]
python3 -B modules/stewardship/steward.py lint --root <project> [--root <other>] [--format json]
```

- `bootstrap` needs an existing `<project>/AGENTS.md` or `<project>/CLAUDE.md` and refuses a symlinked project or
  target (exit 2, nothing written). It creates only missing files of the plan scaffold in `<project>/plans/<topic>/`
  with exclusive create; existing files are reported as `kept` and left byte-identical. It then registers the project.
- The registry is one JSON file you choose, written atomically and idempotent per project root.
- `resurvey` reads only the registered roots: `ok`, `missing`, `changed` or `scaffold-missing`; exit 1 if any is not ok.
- `lint` is read-only. Finding codes: `instructions-missing`, `instructions-dead-link`, `agent-frontmatter`,
  `workflow-shape`, `plan` (blocking findings of `tools/plancheck/plancheck`). Exit 1 when there are findings.
