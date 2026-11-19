# browser module

A stdlib browser protocol with a backend abstraction. Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`; only the local
fixture backend runs. A real browser backend is NOT RUN.

## Protocol

survey -> proposal -> independent review -> approval bound to the exact state -> act -> receipt.

| Command | What it does | Exit |
| --- | --- | --- |
| `status [--json]` | probe each backend (reads configuration, opens no socket) | 0 |
| `survey --mode read\|form\|agent --url U --out F [--backend fixture\|cdp]` | snapshot: url, revision, forms, `dom_sha256`, `takeover`, `untrusted_text` | 0 |
| `propose --survey F --facts J --form ID --out P` | values come only from the facts file; a field not in the form is refused | 0, 3 takeover, 4 |
| `review --survey F --proposal P --facts J --out R --reviewer-cmd ARGV...` | a separate process re-surveys the URL and returns APPROVE or REJECT (`--reviewer-cmd` goes last) | 0, 2 |
| `approve --mode form\|agent --review R [--user-confirm SHA] --out A` | form mode needs the user's confirmation of the proposal hash; agent mode is reviewer-only and refuses sensitive fields (`needs_user`) | 0, 2, 4 |
| `act --approval A --out RECEIPT [--backend ...]` | re-surveys, checks takeover, then revision, then DOM, claims the approval once, submits, writes the receipt | 0, 2, 3, 4 |
| `verify-receipt RECEIPT` | loads approval, proposal, survey and review by hash and checks them against the receipt | 0, 1 |

Exit codes: 0 ok; 1 receipt invalid; 2 refused before anything (missing backend, tool or reviewer, forbidden URL,
wrong mode); 3 state changed or takeover, stopped before the action; 4 approval or review invalid.

Read mode surveys only: it has no approve or act path. The SPEC calls the user-approved mode "drive"; this module
calls it `form` (Deviation recorded in the build report).

## Safety properties

- Only `http://127.0.0.1[:port]/` and `file://` fixtures under the shipped fixture directories are reachable.
  Redirects are not followed off the origin; the environment proxy is ignored; a form `action` must be same-origin.
- Page text is untrusted data. It is parsed, hashed and capped (4 KB) under `untrusted_text` and never interpreted;
  proposals take values only from the facts file.
- An approval is bound to the surveyed url, revision, DOM hash, proposal and review, and is single use. The claim is
  created (exclusively) before the form is submitted, so two concurrent acts cannot both submit.
- A receipt is evidence against mistakes, not against a same-user forger: anyone who can write your state
  directory can rewrite every file in it.

## Layout

`backends.py` (Backend, FixtureBackend, CdpBackend probe), `browser.py` (CLI), `tests/` (unittest, loopback
server only). Each command that writes a file also stores a content-addressed copy in `artifacts/` beside it so
later steps can load the chain by hash. Stop records go to `stops.jsonl` beside the receipt. Single use: an approval is spent by an exclusive marker
named by its hash under `${XDG_STATE_HOME:-$HOME/.local/state}/ai-architecture/browser/used/` (no flag moves
it; a symlinked or foreign-owned directory is refused). The guarantee holds per user and state root: another user,
or a wiped state root, is not covered. Older markers in `used/` beside the approval or `--out` still refuse.

Run the tests: `python3 -B -m unittest discover -s modules/browser/tests -v`. The example is in
`examples/browser-local/`.
