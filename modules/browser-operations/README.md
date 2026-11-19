# browser-operations

Lifecycle tooling on top of `../browser` (read-only here): a real local browser backend (headless Chrome, optional),
opt-in profiles, takeover handover and resume, a no-resubmit watchdog, scoped pruning with a preview, a local cost
status and desktop/extension adapter declarations. Stdlib only. Descriptor claim: `planned` (see `descriptor.json`); per-capability state is in `../../capabilities/registry.json`. 127.0.0.1
only: never a real site, a real account or your real browser profile. No model is launched.

## Commands (`ops.py`)

| Command | What it does | Exit |
| --- | --- | --- |
| `status [--json]` | chrome `available`/`unavailable` from `PATH` or `AIARCH_CHROME` only (no process, no socket) | 0 |
| `run [--ops-out DIR] [--profile-dir D] <survey\|propose\|review\|approve\|act\|verify-receipt> ... --backend fixture\|chrome` | passes the `../browser/browser.py` subcommand through; writes `DIR/runs/<id>/events.jsonl` (and the browser pid record) | browser.py's, plus 5 |
| `handover --out DIR --approval A` | after `act` stopped with `takeover`: records the handover; you log in yourself, the module never sees credentials | 0, 2, 4 |
| `resume --out DIR --approval A [--backend ...]` | re-surveys; keeps the approval only when `(revision, dom_sha256)` equals the approval pin, otherwise writes `DIR/invalidated/<sha>` and spends the approval | 0, 3, 4 |
| `watchdog --out DIR [--timeout S] [--json]` | kills a recorded browser older than S seconds (pid, start time and the `--aiarch-token` in its command line must all match); reports `outcome_unknown` approvals and orphaned temp profiles; never submits, never launches, never touches the state root | 0 |
| `prune --out DIR [--days N] [--apply]` | preview by default; `--apply` deletes only real `runs/<id>` directories older than N days (never `used/`, the state root, symlinks or sibling files) | 0 |
| `cost --out DIR [--json]` | local counts from the run events: `surveys, acts, receipts, stops_by_reason, browser_seconds`. Reads only `DIR`; no network, no pricing API, no transcripts | 0 |
| `adapters [--json]` | `extension` and `desktop` are declared `unsupported` with their gap and license note; nothing is installed or driven | 0 |

Exit 5 = `outcome_unknown`: the approved POST was released to Chrome and something failed afterwards (Chrome died,
timeout, SIGTERM). The approval stays spent, the stop is recorded, and nothing is ever re-sent. Before that point
a failure is exit 3 and the approval is released.

## Real backend

Chrome or Chromium is found on `PATH` (or `AIARCH_CHROME`), started per run in its own session with a fresh temp profile
(mode 0700), a parent-death signal and a `finally` that kills the process group and removes the profile. SIGTERM and SIGHUP
raise `SystemExit` so that cleanup runs. Without Chrome the backend is `unavailable`; setup line: `install Chrome or
Chromium and put it on PATH, or set AIARCH_CHROME=/path/to/chrome`. A sandbox failure is reported as unavailable; there
is no `--no-sandbox` fallback.

Exact launch flags (also asserted by the tests):

```
--headless=new --user-data-dir=<udd> --no-first-run --no-default-browser-check --disable-extensions
--disable-component-extensions-with-background-pages --disable-background-networking --disable-sync
--disable-component-update --disable-default-apps --disable-domain-reliability --disable-breakpad --no-pings
--password-store=basic --proxy-server=http://127.0.0.1:<sink>
--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1
--force-webrtc-ip-handling-policy=disable_non_proxied_udp --remote-debugging-port=0 --aiarch-token=<token>
```

Never passed (asserted on `/proc/<pid>/cmdline`): `--remote-allow-origins`, `--remote-debugging-address`, `--no-sandbox`,
`--disable-setuid-sandbox`, `--disable-web-security`, `--allow-file-access-from-files`, `--single-process`,
`--no-zygote`. The DevTools port is chosen by Chrome (`--remote-debugging-port=0`), read from `DevToolsActivePort` in the
profile and accepted only as `ws://127.0.0.1:<that port>/devtools/browser/<id>`; the minimal RFC 6455 client sends no
`Origin`, masks its frames, checks `Sec-WebSocket-Accept`, joins continuation frames and caps messages at 1 MB. The pipe
transport (`--remote-debugging-pipe`) is not used (Deviation recorded in the build report).
Chrome's environment is an allow-list: `PATH`, `LANG`, `TMPDIR`, and `HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME` pointing into
the profile (no D-Bus, display or proxy variables).

Confinement, in layers: page scripts never run (`Emulation.setScriptExecutionDisabled`; the survey is the served DOM, as
with the fixture backend), because WebSocket handshakes and beacons to other 127.0.0.1 ports are not requests the Fetch
domain can gate. Preconnect hints (`<link rel=preconnect>`, the `Link` header) are browser-process work that Fetch never
sees and that opens bare TCP connections to other loopback ports (measured in real Chrome); the profile's `Preferences` get
`net.network_prediction_options=2` before every launch, and `test_u10f4` asserts zero connections and zero requests for
preconnect, speculation rules, prefetch, meta refresh and `a[ping]`. Worker targets are closed, not resumed, while scripts
are off. Every other target (popup, worker, service worker) is held at start by `Target.setAutoAttach` and
resumed only after it has the same gate. Every request the Fetch domain sees is failed unless it is a `GET` that passes
`backends.check_url` on the page's own origin; the only `POST` released is the single approved one (url equal to the
resolved same-origin action, body equal to the URL-encoded values, issued from the pinned frame, armed once), triggered
by a `fetch` from an isolated world with the values passed as arguments (never formatted into script source); Chrome's own
non-loopback traffic (it tries Google endpoints at start-up) goes to a built-in 127.0.0.1 sink proxy that counts and refuses
it. `file://` is refused by this backend (stricter than the fixture backend). Page content is parsed by
`backends.snapshot()` from `document.documentElement.outerHTML`, so `dom_sha256` is comparable with the fixture backend.

Profiles are opt-in: `--profile-dir D` must be absolute, free of symlinked components, and not equal to, inside or an
ancestor of the output directory, the single-use state root, or a default Chrome/Chromium user-data directory; an
existing non-empty directory needs this module's `.aiarch-profile` marker, and a directory with a live
`SingletonLock` is refused (exit 2). It is created 0700. No flag accepts a credential.

## Not ported / gaps

- "panel" (a live multi-seat model vote) is not ported: it needs model launches. The independent reviewer step of
  `../browser` is the review.
- Registry row 32 (desktop/extension integrations) stays unmet: the adapters are declarations only.
- Single-use claiming, receipts and verification are `../browser`'s, unchanged.

Run the tests: `python3 -B -m unittest discover -s modules/browser-operations/tests -v` (real-Chrome tests skip with a
printed reason when Chrome is missing or cannot start; `AIARCH_REQUIRE_BROWSER=1` turns that skip into a failure).
