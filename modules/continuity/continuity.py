#!/usr/bin/env python3
"""Durable job ledger CLI (stdlib only): start, run, wait, status, collect, cancel, drain, reconcile, external,
usage, checkpoint, ready, handoff, resume.

Every command prints one JSON object. Exit: 0 ok, 1 job failed, 2 refused or bad usage, 3 ledger busy,
4 ledger invalid or storage failure (nothing changed; `ready` reports an invalid ledger as not ready, exit 1), 5 still pending at timeout or after side effects whose
bank failed (processes killed or a worktree removed: reconcile again), 6 dispatcher launched but
its record or result not banked (never retry), 7 committed, durability unknown (never retry).
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.continuity import freeze, jobs, receipts, reconcile, resume  # noqa: E402
from modules.continuity import ledger as L  # noqa: E402
from modules.continuity import usage as U  # noqa: E402


def emit(code, payload):
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return code


def cmd_init(args):
    state, root, created = L.init_project(args.project)
    note = ("Add .ai-continuity/ to your .gitignore: it holds job state, prompts and outputs, "
            "and init never edits .gitignore.")
    return 0, {"state": str(state), "project_root": root, "created": created, "reminder": note}


def add_launch_options(parser):
    parser.add_argument("--config", required=True)
    parser.add_argument("--role", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--intent")
    group.add_argument("--prompt-file")
    parser.add_argument("--model")
    parser.add_argument("--allow-spend", action="store_true")
    parser.add_argument("--workdir")
    parser.add_argument("--session")
    parser.add_argument("--budget-tokens", type=int)
    parser.add_argument("--job-id")
    parser.add_argument("--context", help="context packet (context_packet.py JSON); needs --intent")


def add_freeze_commands(sub):
    fz = sub.add_parser("freeze", help="per-session freeze: deny edits outside a root and Bash for one session")
    fsub = fz.add_subparsers(dest="freeze_action", required=True)
    for name in ("set", "clear", "status", "check"):
        p = fsub.add_parser(name)
        p.add_argument("--session")
        p.add_argument("--harness")
        if name == "set":
            p.add_argument("--root", required=True)
            p.add_argument("--bash", default="deny")
        if name == "check":
            p.add_argument("--tool")
            p.add_argument("--path")


def add_receipt_options(parser, session_required):
    parser.add_argument("--state", dest="state_text", required=True, metavar="TEXT")
    parser.add_argument("--next", required=True)
    parser.add_argument("--evidence", action="append", required=True)
    parser.add_argument("--tracker")
    parser.add_argument("--accept-failed", nargs="+", action="extend", default=[])
    parser.add_argument("--session", required=session_required)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, **kw):
        p = sub.add_parser(name, **kw)
        if name in ("init", "checkpoint", "handoff"):  # --state is the checkpoint text there, so the project is named
            p.add_argument("--project", required=True)
        else:
            where = p.add_mutually_exclusive_group(required=True)
            where.add_argument("--project")
            where.add_argument("--state")
        return p

    command("init")
    add_freeze_commands(sub)
    add_launch_options(command("start"))
    add_launch_options(command("run"))
    p = command("wait")
    p.add_argument("--job", required=True)
    p.add_argument("--timeout", type=float)
    p = command("status")
    p.add_argument("--job")
    p.add_argument("--session")
    command("collect")
    p = command("cancel")
    p.add_argument("--job", required=True)
    p.add_argument("--session")
    p.add_argument("--force", action="store_true")
    p = command("drain")
    p.add_argument("action", choices=("start", "status", "end", "wait"))
    p.add_argument("--reason")
    p.add_argument("--timeout", type=float)
    p = command("reconcile")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--job")
    p.add_argument("--force-bank")
    p.add_argument("--allow-unknown-liveness", action="store_true")
    p.add_argument("--reason")
    p = command("external")
    p.add_argument("action", choices=("register", "bank"))
    p.add_argument("--job-id", required=True)
    p.add_argument("--role")
    p.add_argument("--access", choices=("read", "write"))
    p.add_argument("--session")
    p.add_argument("--owner-pid", type=int)
    p.add_argument("--outcome", choices=("succeeded", "failed"))
    p.add_argument("--cause", choices=("completed", "cancelled", "orphaned_external"))
    p.add_argument("--result-file")
    p = command("usage")
    p.add_argument("action", choices=("record",))
    p.add_argument("--session", required=True)
    p.add_argument("--tokens", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--model")
    p.add_argument("--source", default="cli", choices=L.SOURCES)
    add_receipt_options(command("checkpoint"), session_required=False)
    p = command("ready")
    p.add_argument("--session", required=True)
    add_receipt_options(command("handoff"), session_required=True)
    p = command("resume")
    p.add_argument("action", choices=("show", "consume"))
    p.add_argument("--session")
    p.add_argument("--cwd")
    p.add_argument("--source", choices=resume.SOURCES)
    return parser


def dispatch_command(args):
    if args.command == "init":
        return cmd_init(args)
    if args.command == "freeze":
        return freeze.cli(args)
    name = args.command
    ctx = jobs.Context(L.open_state(args.project, None if name in ("checkpoint", "handoff") else args.state))
    if name in ("start", "run"):
        code, out = (jobs.start_job if name == "start" else jobs.run_job)(ctx, args)
        if ctx.admit_note and isinstance(out, dict):
            out["note"] = ctx.admit_note
        return code, out
    if name == "wait":
        return jobs.wait_job(ctx, args.job, args.timeout)
    if name == "status":
        key = receipts.session_arg(args.session)
        code, out = jobs.status(ctx, args.job)
        if key is not None and code == 0:
            out.update(U.session_view(ctx.ledger.read(), ctx.cfg, key))
        return code, out
    if name == "usage":
        return U.record(ctx, receipts.session_arg(args.session, True), U.parse_tokens(args.tokens), args.limit,
                        args.model, args.source)
    if name == "checkpoint":
        return receipts.checkpoint(ctx, args)
    if name == "handoff":
        return receipts.handoff(ctx, args)
    if name == "ready":
        return receipts.ready(ctx, receipts.session_arg(args.session, True))
    if name == "resume":
        if args.action == "show":
            return resume.show(ctx, receipts.session_arg(args.session))
        if args.session is None or args.cwd is None or args.source is None:
            raise L.Refused("resume consume needs --session, --cwd and --source")
        return resume.consume(ctx, receipts.session_arg(args.session), args.cwd, args.source)
    if name == "collect":
        return jobs.collect(ctx)
    if name == "cancel":
        return jobs.cancel_job(ctx, args.job, args.session, args.force)
    if name == "drain":
        if args.action == "start":
            return jobs.drain_start(ctx, args.reason)
        if args.action == "end":
            return jobs.drain_end(ctx)
        if args.action == "status":
            return jobs.drain_status(ctx)
        return jobs.drain_wait(ctx, args.timeout)
    if name == "reconcile":
        if args.allow_unknown_liveness and not args.force_bank:
            raise L.Refused("--allow-unknown-liveness only applies together with --force-bank")
        if args.force_bank:
            return 0, reconcile.force_bank(ctx, args.force_bank, args.reason, args.allow_unknown_liveness)
        if args.job and args.job not in ctx.ledger.read()["jobs"]:
            raise L.Refused(f"unknown job {args.job}")
        return reconcile.reconcile_all(ctx, args.dry_run, args.job)
    if args.action == "register":
        if args.role is None or args.access is None or args.session is None or args.owner_pid is None:
            raise L.Refused("external register needs --role, --access, --session and --owner-pid")
        return jobs.external_register(ctx, args.job_id, args.role, args.access, args.session, args.owner_pid)
    if args.outcome is None or args.cause is None:
        raise L.Refused("external bank needs --outcome and --cause")
    return jobs.external_bank(ctx, args.job_id, args.outcome, args.cause, args.result_file)


def _open_rows(payload):
    rows = list(payload.get("results") or [])
    if isinstance(payload.get("reconcile"), dict):
        rows.append(payload["reconcile"])
    return [r for r in rows if reconcile.behind_row(r)]


def enforce_acted(code, payload):
    """The acted rule, once, after the command returns: a command that signalled or removed something and leaves a
    row `behind_row` counts (not banked and marked `changed` by `settle_job`) exits 5 with `changed: true`, never 0.
    An untouched unresolved or live running row is not behind. A code-5 payload with such a row and no `changed`
    gets it. Exit 7 and exceptions are handled in `main`."""
    if reconcile.acted() and code != 7 and _open_rows(payload) and not (code == 5 and "changed" in payload):
        return 5, dict(payload, changed=True)
    return code, payload


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    try:
        L.require_platform()
        reconcile.reset_acted()  # once per command: a signal sent anywhere in it makes a later ledger failure exit 5
        code, payload = dispatch_command(args)
        code, payload = enforce_acted(code, payload)
        return emit(code, payload)
    except L.LedgerDurabilityUnknown as error:
        return emit(7, {"error": str(error), "committed": True, "durable": "unknown"})
    except reconcile.SideEffectsDone as error:
        return emit(5, {"error": str(error), "changed": True})
    except (L.LedgerBusy, L.LedgerInvalid, L.LedgerStorageError) as error:
        if reconcile.acted():  # the acted rule, at the one entry point: a signal or removal happened, so never "nothing changed"
            return emit(5, {"error": f"{error}; processes were already killed or a worktree removed: run reconcile again",
                            "changed": True})
        if isinstance(error, L.LedgerBusy):
            return emit(3, {"error": str(error)})
        return emit(4, {"error": str(error), "changed": False})
    except L.Refused as error:
        if reconcile.acted():  # e.g. a ResultRefused after cancel's own signal: the world changed, never a plain refusal
            return emit(5, {"error": f"{error}; processes were already killed or a worktree removed: run reconcile again",
                            "changed": True})
        return emit(2, {"error": str(error)})


if __name__ == "__main__":
    sys.exit(main())
