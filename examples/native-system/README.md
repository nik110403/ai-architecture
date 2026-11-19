# Native system journey (offline and fake harness)

One request goes through the Continuity module and the orchestration dispatcher, survives a SIGKILL of the
coordinator, and is finished by a restarted coordinator that reads its own persisted Next:

1. [run.py](run.py) copies [input.txt](input.txt) into `<out>/project` and runs `init`.
2. A coordinator process starts a worker job (the worker role, `researcher`, whose agent file takes a bounded read task with no artifact-and-lens precondition; blocked on a gate file) and writes a checkpoint whose
   Next names the run id and the pending job.
3. The parent kills the coordinator with SIGKILL, records the pid and the signal, and releases the gate.
4. A restarted coordinator with a new session reads `resume show`. It acts only when that Next carries its own
   run id, then calls `resume consume` and acts only when `delivered` is true. It waits for the worker, starts a
   separate checker job (the refuter role, asked to disprove the claimed sum; its `VERDICT: STANDS|REFUTED|UNVERIFIABLE` line is normalized to CONFIRMED/REJECTED, anything else fails closed), and on CONFIRMED writes the answer
   and one line of `<out>/actions.jsonl`. It checkpoints (accepting every recovered failure), runs `ready`, and
   consumes that last checkpoint.
5. The summary and `<out>/receipt.json` are written.

```sh
python3 -B examples/native-system/run.py --offline          # needs no harness
python3 -B examples/native-system/verify_receipt.py         # exit 0 only when every check holds
python3 -B -m unittest discover -s examples/native-system/tests -v
```

`<out>` defaults to `$XDG_STATE_HOME/ai-architecture/native-system` (else `~/.local/state/...`), outside the
source tree. It must not be a symlink or owned by someone else; directories are mode 0700. A second run into an
`<out>` that already holds a receipt goes to `<out>/runs/<run id>/` and `<out>/latest.json` points at it.

## Modes

- `--offline`: local-command seats running [worker.py](worker.py) and [checker.py](checker.py).
- `--fake-harness claude-code|opencode --fake-bin DIR [--gate FILE]`: the dispatcher's PATH is `DIR:/usr/bin:/bin`.
  Refused (exit 2) unless the harness binary resolves inside `DIR` and contains the marker `AI_ARCH_FAKE_HARNESS`.
  The marker check is the only spend guard in this mode: the fake seats run with `allow_spend: true`.
- `--live --harness claude-code|opencode --model M --i-accept-spend`: real, paid, **not run by the tests and not
  run while building this example**. Exit 2 without the flag, without `--model`, when the binary is not on PATH or
  when it carries the fake marker. Only then is spend allowed.
- `--resume [--out DIR] [--session H:ID]` re-enters step 4 under the restart session (the default) or another one.
  Fake and live resumes repeat the spend guard and the binary pin of the first run (the coordinator child repeats
  it too). A live resume needs `--i-accept-spend --model M` with the same `M` as the first run. A harness update
  changes the pin and the resume is refused; recover by starting a new run. `verify_receipt.py` needs
  `<out>/plan.json` next to the receipt.
  Exit 3 when the latest Next is another run's or was delivered elsewhere; nothing is consumed or done.
- Test hooks (offline): `--fail-first checker`, `--fail-always worker`, `--hold worker --wait-timeout S`,
  `--wrong-worker`, `--stop-after kill|action`.

## Sharing and the live check

`<out>/plan.json` and `<out>/calls.jsonl` hold absolute host paths (the run directory, the fake or real harness binary): share
`<out>/receipt.json` only. The receipt itself drops `ready` reasons that contain a `/`, keeps `harness_version` to printable
characters without `/`, and shows an answer that is not a plain integer only as a hash and a length.
The mode in a receipt is self-declared, so close a live run with
`python3 -B examples/native-system/verify_receipt.py --expect-mode live`; the bare command accepts any mode.

## What the receipt proves

`<out>/receipt.json` records the mode, both sessions, the kill, every job (outcome, cause, dispatcher harness,
version, usage, stdout hash), the resume, `ready`, the answer and a hash of the calls log. `verify_receipt.py`
re-checks it against the dispatcher envelopes and the ledger, re-runs `ready`, and requires the checker to have
said CONFIRMED, exactly one action, and the Next consumed by the restart session. A receipt is evidence against
mistakes, not against a same-user forger: that user can rewrite the ledger, the envelopes and the receipt together.

## Limits

- The spend guard (marker, pin, consent, model) is re-proven right before each checker `run`, but a process of the
  same user can still swap the binary between that check and the launch: a same-user race this example does not close.
- `--out` must be empty, or hold an earlier run (`<out>/latest.json` or `<out>/plan.json`), and may not lie below a symlink.
  `<out>/latest.json` must name `.` or `runs/<id>`.
- `--resume` exits 1 when the latest Next was already delivered to this session but the run has no receipt, and 3 when
  another session took the Next between `resume show` and `resume consume`.
- Usage is recorded with `usage record --tokens 0 --source cli`: READY needs a known usage, and this example has
  no real token count for the coordinator. The jobs' own usage comes from the dispatcher envelopes.
- Only 20 checkpoints are kept. A failure accepted in an aged-out checkpoint stops counting as accepted, so
  `--resume` accepts the failures recorded in `<out>/coordinator.json` again when it is handed a completed Next.
- A restart that crashes after consuming its Next but before finishing is not repaired by `--resume`: the Next is
  consumed once. This example does not choose a recovery policy.
- The native live run is **not run**: no real harness has been exercised, so no adapter is verified by this
  example. A live receipt may hold harness ids, so keep it out of commits.
