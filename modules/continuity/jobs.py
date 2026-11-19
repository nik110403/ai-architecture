"""Job lifecycle over dispatch.py: start, run, wait, bank, collect, cancel, drain, external jobs.

Every banking path goes through `bank()`: the "already banked?" check and the write happen in one
exclusive-lock transaction, which is the exactly-once guarantee. The lock is never held while a
process is waited for, signalled or swept.
"""

import calendar
import contextlib
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from modules.continuity import context as contextpack
from modules.continuity import ledger as L
from modules.continuity import usage as U
from modules.orchestration import dispatch

ROOT = L.ROOT
PYTHON = sys.executable
DISPATCH = ROOT / "modules" / "orchestration" / "dispatch.py"
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_STDOUT_BYTES = 1024 * 1024
MAX_EXTERNAL_BYTES = 64 * 1024
MAX_RECORD_BYTES = L.MAX_RECORD_BYTES
MAX_EVIDENCE = 64
MAX_HASH_BYTES = 1024 * 1024
WORKTREE_KEPT = "write job: the branch is the work product, integration is not done here"
CAUSE_KEPT = ("completed", "timed_out")  # a cancel request does not rename these


class Context:
    def __init__(self, state):
        self.state = Path(state)
        self.cfg = L.load_config(self.state)
        self.ledger = L.Ledger(self.state, self.cfg["lock_wait_seconds"])
        self.runs = self.state / "runs"
        self.spool = self.state / "spool"
        self.admit_note = None  # set by `admit` when the session's usage is unknown: the CLI adds it to the output


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def monotonic():
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def sha_file(path):
    """SHA-256 of a regular file of at most 1 MiB, opened without following symlinks or blocking (a worker may plant a FIFO).

    A larger file raises OSError("file too large"): the hash is computed under reconcile.lock, so the work is bounded.
    """
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > MAX_HASH_BYTES:
            raise OSError("file too large")
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_HASH_BYTES:
                raise OSError("file too large")
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def parse_created(text):
    """Epoch seconds of an ISO `...Z` timestamp."""
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def write_private(path, text):
    """Atomic 0600 write whose temporary file is created with O_NOFOLLOW|O_EXCL (a planted symlink is replaced)."""
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def touch(doc, job):
    job["updated_at"] = L.now()
    job["updated_revision"] = doc["revision"] + 1


# --- observations ------------------------------------------------------------------------

def read_start(ctx, job_id):
    """("absent"|"invalid"|"ok", dict|None) for runs/<id>/start.json. Worker-writable: advisory only."""
    path = ctx.runs / job_id / "start.json"
    try:
        data = L.read_file(path, 1 << 20)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "invalid", None
    try:
        start = L.strict_loads(data)
    except (ValueError, RecursionError):
        return "invalid", None
    if not isinstance(start, dict) or start.get("job_id") != job_id:
        return "invalid", None
    return "ok", start


def usage_of(dispatch_part):
    raw = dispatch_part.get("usage") if isinstance(dispatch_part, dict) else None
    if not isinstance(raw, dict):
        return None
    usage = {k: raw[k] for k in dispatch.USAGE_KEYS if k in raw and dispatch.usable(raw[k])}
    return usage or None


stored_size = L.stored_size


def observation_from_envelope(job, envelope, sha, source):
    """Observation for a dispatcher envelope whose record is valid and names this job; else None."""
    if not isinstance(envelope, dict):
        return None
    record = envelope.get("result")
    try:
        dispatch.check_record("result", record, dispatch.result_schema())
    except Exception:  # noqa: BLE001 - the contracts validator raises its own error type
        return None
    if len(record["evidence"]) > MAX_EVIDENCE or stored_size(record) > MAX_RECORD_BYTES:
        return None  # the file is hashed by the caller; its content never enters the ledger
    if record["event_id"] != job["job_id"]:
        return None
    part = envelope.get("dispatch") if isinstance(envelope.get("dispatch"), dict) else {}
    timed_out = part.get("timed_out") is True
    outcome = record["outcome"]
    if outcome == "succeeded" and (timed_out or part.get("exit_code") == -9):
        outcome = "failed"  # contradictory: a killed or timed-out job is not a success
    cause = "refused" if outcome == "unsupported" else ("timed_out" if timed_out else "completed")
    note = part.get("note")
    note = L.cap_text(note) if isinstance(note, str) else None
    reported = part.get("worktree") if isinstance(part.get("worktree"), dict) else None
    plan, worktree = job.get("worktree_plan"), None
    if job["access"] == "write" and plan:  # path and branch are the ledger's own plan, never the worker-writable file's
        worktree = {"path": plan["path"], "branch": plan["branch"], "kept": True, "reason": WORKTREE_KEPT}
        if reported and (reported.get("path"), reported.get("branch")) != (plan["path"], plan["branch"]):
            mismatch = f"worktree reported by result.json differs from the plan ({L.cap_text(reported.get('path'), 120)})"
            note = L.cap_text(f"{note}; {mismatch}" if note else mismatch)
    return {
        "outcome": outcome, "cause": cause, "source": source, "result_sha256": sha, "record": record,
        "exit_code": None, "usage": usage_of(part), "worktree": worktree,
        "evidence": [], "note": note,  # the evidence is in `record`, not stored twice
        "trust": "worker-attested" if job["access"] == "write" else "dispatcher",
        "interrupted": isinstance(note, str) and note.startswith("interrupted by signal"),
    }


def read_result_file(ctx, job):
    """("absent"|"invalid"|"valid", observation-or-info). `info` for invalid has the sha256 when readable."""
    path = ctx.runs / job["job_id"] / "result.json"
    try:
        data = L.read_file(path, MAX_RESULT_BYTES)
    except FileNotFoundError:
        return "absent", None
    except OSError as error:
        return "invalid", {"sha256": None, "reason": L.cap_text(error)}
    sha = L.sha256_hex(data)
    try:
        envelope = L.strict_loads(data)
    except (ValueError, RecursionError) as error:
        return "invalid", {"sha256": sha, "reason": L.cap_text(f"not strict JSON: {error}")}
    try:
        obs = observation_from_envelope(job, envelope, sha, "file")
    except Exception as error:  # noqa: BLE001 - worker input must never raise out of an observation
        return "invalid", {"sha256": sha, "reason": L.cap_text(f"result.json unusable ({error.__class__.__name__})")}
    if obs is None:
        return "invalid", {"sha256": sha, "reason": "not a valid result record for this job (or over the size limits)"}
    return "valid", obs


def observe_result(ctx, job, file_source):
    """(observation|None, result.json state, info): result.json first, then the dispatcher's stdout envelope.

    The one source order `run` and the reconciler share. A result.json observation carries `file_source`
    (`wait` for `run`, `file` for the reconciler); an envelope observation carries `stdout`.
    """
    state, info = read_result_file(ctx, job)
    if state == "valid":
        return dict(info, source=file_source), state, info
    envelope, sha = read_stdout_envelope(ctx, job["job_id"])
    if envelope is not None:
        try:
            obs = refusal_observation(envelope, sha, "stdout") or observation_from_envelope(job, envelope, sha, "stdout")
        except Exception as error:  # noqa: BLE001 - worker input must never raise out of an observation
            return None, "invalid", {"sha256": sha, "reason": L.cap_text(
                f"stdout envelope unusable ({error.__class__.__name__})")}  # as read_result_file: invalid, with a reason
        if obs is not None:
            return obs, state, info
    return None, state, info


def read_stdout_envelope(ctx, job_id):
    """(envelope|None, sha256|None) of the whole spool/<id>.out parsed as one JSON value, at most 1 MiB."""
    try:
        data = L.read_file(ctx.spool / f"{job_id}.out", MAX_STDOUT_BYTES)
    except OSError:
        return None, None
    try:
        return L.strict_loads(data), L.sha256_hex(data)
    except (ValueError, RecursionError):
        return None, L.sha256_hex(data)


def refusal_observation(envelope, sha, source):
    """Observation for a dispatcher refusal envelope, else None."""
    try:
        record, part = envelope["result"], envelope["dispatch"]
        reason = part["refused"]
        dispatch.check_record("result", record, dispatch.result_schema())
    except Exception:  # noqa: BLE001 - untrusted shapes: anything wrong means "not a refusal envelope"
        return None
    if record["outcome"] != "unsupported" or not isinstance(reason, str):
        return None
    return {"outcome": "unsupported", "cause": "refused", "source": source, "result_sha256": sha, "record": None,
            "exit_code": None, "usage": None, "worktree": None, "evidence": [], "note": L.cap_text(reason),
            "trust": "dispatcher", "interrupted": False}


def synth(outcome, cause, note, *, source="reconcile", worktree=None, evidence=None, sha=None, exit_code=None):
    """An observation the recovery logic produced itself (no dispatcher record behind it)."""
    return {"outcome": outcome, "cause": cause, "source": source, "result_sha256": sha, "record": None,
            "exit_code": exit_code, "usage": None, "worktree": worktree, "evidence": evidence or [], "note": note,
            "trust": "recovery", "interrupted": False}


# --- banking -------------------------------------------------------------------------------

def bank_in(doc, job_id, obs):
    job = doc["jobs"].get(job_id)
    if job is None:
        raise L.Refused(f"unknown job {job_id}")
    revision = doc["revision"] + 1
    if job["state"] == "banked":
        sha = obs.get("result_sha256")
        known = {job["result"]["result_sha256"], *(e["sha256"] for e in job["post_bank"])}
        if sha is not None and sha not in known:
            job["post_bank"].append({"observed_at": L.now(), "outcome": str(obs["outcome"]), "sha256": sha,
                                     "source": str(obs["source"])})
            del job["post_bank"][:-8]
            touch(doc, job)
        return {"banked_now": False, "outcome": job["result"]["outcome"], "cause": job["result"]["cause"]}
    outcome, cause, note = obs["outcome"], obs["cause"], obs.get("note")
    note = L.cap_text(note) if note is not None else None
    if job["cancel"] is not None:
        if outcome == "succeeded":
            note = "cancel arrived after completion"
        elif cause not in CAUSE_KEPT or obs.get("interrupted"):
            if cause != "cancelled":
                note = L.cap_text(f"original cause {cause}" + (f": {note}" if note else ""))
            cause = "cancelled"
    result = {
        "outcome": outcome, "cause": cause, "source": obs["source"], "result_sha256": obs.get("result_sha256"),
        "record": obs.get("record"), "exit_code": obs.get("exit_code"), "usage": obs.get("usage"),
        "worktree": obs.get("worktree"), "evidence": obs.get("evidence") or [], "note": note,
        "trust": obs["trust"], "banked_at": L.now(), "bank_revision": revision,
    }
    result["usage_total"] = U.usage_total(result["usage"])
    result["budget_state"] = U.budget_state(job["budget"]["tokens"], result["usage_total"])
    try:
        L.check_result(result, "result")
    except L.LedgerInvalid as error:  # the ledger is fine; this one result cannot be stored: not exit 4
        raise L.ResultRefused(f"job {job_id}: the result cannot be banked ({error})") from error
    job["result"] = result
    job["state"] = "banked"
    touch(doc, job)
    return {"banked_now": True, "outcome": job["result"]["outcome"], "cause": job["result"]["cause"]}


def bank(ledger, job_id, obs):
    """The one banking function: check and write under one exclusive lock."""
    with ledger.transaction() as doc:
        return bank_in(doc, job_id, obs)


def exit_for(outcome):
    return {"succeeded": 0, "failed": 1, "unsupported": 2}[outcome]


def brief(job):
    result = job["result"] or {}
    return {"job_id": job["job_id"], "role": job["role"], "state": job["state"], "outcome": result.get("outcome"),
            "cause": result.get("cause"), "owner": job["owner"]}


# --- collect (non-destructive) -----------------------------------------------------------------

def collect_all(ctx, only=None):
    """Bank every job whose result.json exists; record late results of banked jobs. No signals, no sweeps."""
    doc = ctx.ledger.read()
    banked_now = []
    for job_id, job in sorted(doc["jobs"].items()):
        if only is not None and job_id != only or job["kind"] != "dispatch":
            continue
        state, info = read_result_file(ctx, job)
        if state == "absent":
            continue
        try:  # a result the ledger refuses to store leaves that job open; it must not stop the others
            if job["state"] != "banked":
                if state == "valid":
                    if bank(ctx.ledger, job_id, info)["banked_now"]:
                        banked_now.append(job_id)
                continue
            sha = info["result_sha256"] if state == "valid" else info["sha256"]
            known = {job["result"]["result_sha256"], *(e["sha256"] for e in job["post_bank"])}
            if sha is not None and sha not in known:
                late = info if state == "valid" else {"outcome": "invalid", "cause": "invalid_result", "source": "file",
                                                      "result_sha256": sha}
                bank(ctx.ledger, job_id, late)
        except L.ResultRefused:
            continue
    return banked_now


# --- start / run -----------------------------------------------------------------------------------

def admit(ctx, opts):
    """Step 1: refuse or record the job in `launching` and write its prompt file."""
    roles = dispatch.load_roles()
    role = opts.role
    if not isinstance(role, str) or not dispatch.NAME.fullmatch(role) or role not in roles:
        raise L.Refused(f"unknown role {role!r} (intent words resolve in `dispatch.py plan`; --role is required)")
    access = dispatch.role_access(roles[role]["tools"])
    job_id = opts.job_id or L.new_job_id()
    if not isinstance(job_id, str) or not dispatch.NAME.fullmatch(job_id):
        raise L.Refused(f"invalid job id {job_id!r}")
    if getattr(opts, "prompt_file", None):
        try:
            prompt = L.read_file(opts.prompt_file, 4 * 1024 * 1024).decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise L.Refused(f"prompt file unreadable: {error.__class__.__name__}")
    elif getattr(opts, "intent", None):
        prompt = opts.intent
    else:
        raise L.Refused("give --intent or --prompt-file")
    context_record = None
    if getattr(opts, "context", None):
        if getattr(opts, "prompt_file", None):
            raise L.Refused("--context goes with --intent, not --prompt-file")
        prompt, context_record = contextpack.build(opts.context, opts.intent, ctx.ledger.read()["project_root"])
    owner = L.parse_session(opts.session) if getattr(opts, "session", None) else {"harness": "cli", "session_id": None}
    budget = getattr(opts, "budget_tokens", None)
    if budget is None:
        budget = ctx.cfg["child_budget_tokens"]
    if budget is not None and (not L.is_int(budget) or budget < 0):
        raise L.Refused("--budget-tokens must be an integer >= 0")
    workdir = os.path.realpath(getattr(opts, "workdir", None) or ".")
    plan = None
    if access == "write":
        try:
            config = dispatch.load_config(opts.config, roles)
            if config.get("worktree_root"):
                plan = dispatch.plan_worktree(config, role, job_id)
        except dispatch.Refusal:
            plan = None  # the dispatcher will refuse; a refusal envelope is banked from the spool
        if plan and len(plan["path"]) > L.MAX_PATH:
            raise L.Refused(f"worktree_root is too long: the planned worktree path exceeds {L.MAX_PATH} characters")
    me = os.getpid()
    record = {
        "job_id": job_id, "kind": "dispatch", "role": role, "access": access, "owner": owner, "state": "launching",
        "created_at": L.now(), "updated_at": L.now(), "updated_revision": 0,
        "submitter": {"pid": me, "start_ticks": dispatch.proc_start_ticks(me)}, "dispatcher": None,
        "workdir": workdir, "worktree_plan": plan, "prompt_sha256": L.sha256_hex(prompt.encode("utf-8")),
        "budget": {"tokens": budget}, "cancel": None, "result": None, "post_bank": [], "acknowledged_revision": None,
        "context": context_record,
    }
    spool_prompt = ctx.spool / f"{job_id}.prompt"
    wrote = False
    try:
        with ctx.ledger.transaction() as doc:
            if doc["drain"] is not None:
                raise L.Refused(f"draining since {doc['drain']['since']}: {doc['drain']['reason']}")
            active = sum(1 for j in doc["jobs"].values() if j["state"] != "banked")
            if active >= ctx.cfg["max_active_jobs"]:
                raise L.Refused(f"{active} jobs are not banked yet (max_active_jobs {ctx.cfg['max_active_jobs']})")
            if job_id in doc["jobs"] or (ctx.runs / job_id).exists():
                raise L.Refused(f"job id {job_id!r} already exists")
            ctx.admit_note = U.admit_check(doc, ctx.cfg, L.session_key(owner), budget)
            write_private(spool_prompt, prompt)
            wrote = True
            record["updated_revision"] = doc["revision"] + 1
            doc["jobs"][job_id] = record
    except L.LedgerDurabilityUnknown:
        raise  # committed: the job exists and its prompt must stay
    except Exception:
        if wrote:
            try:
                os.unlink(spool_prompt)
            except OSError:
                pass
        raise
    return job_id


def spawn(ctx, opts, job_id):
    """Step 2: the dispatcher as its own process in its own session. Raises OSError if it cannot start."""
    argv = [PYTHON, "-B", str(DISPATCH), "run", "--config", os.path.abspath(opts.config), "--role", opts.role,
            "--prompt-file", str(ctx.spool / f"{job_id}.prompt"), "--run-dir", str(ctx.runs), "--job-id", job_id,
            "--workdir", os.path.realpath(getattr(opts, "workdir", None) or ".")]
    if getattr(opts, "model", None):
        argv += ["--model", opts.model]
    if getattr(opts, "allow_spend", False):
        argv.append("--allow-spend")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    out, err = os.open(ctx.spool / f"{job_id}.out", flags, 0o600), os.open(ctx.spool / f"{job_id}.err", flags, 0o600)
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
    finally:
        os.close(out)
        os.close(err)


def dispatcher_record(pid):
    return {"pid": pid, "start_ticks": dispatch.proc_start_ticks(pid), "launched_monotonic": monotonic(),
            "boot_id": boot_id()}


def record_dispatcher(ctx, job_id, pid):
    """Step 3. Returns the committed dispatcher record when a cancel request is pending, else None.

    The caller signals that record (its pid and start ticks) after the commit, never a freshly built one.
    """
    with ctx.ledger.transaction() as doc:
        job = doc["jobs"][job_id]
        if job["state"] != "launching":
            return None
        job["dispatcher"] = dispatcher_record(pid)
        job["state"] = "launched"
        touch(doc, job)
        return dict(job["dispatcher"]) if job["cancel"] is not None else None


def reconcile_acted():
    from modules.continuity import reconcile
    return reconcile.acted()


def signal_cancel(ctx, job_id, record):
    from modules.continuity import reconcile
    reconcile.signal_dispatcher(ctx, job_id, record, (signal.SIGCONT, signal.SIGTERM))


def honour_cancel(ctx, job_id):
    """After a step 3 whose durability is unknown the commit happened: a cancel request recorded with it is still signalled.

    True once the ledger was read (whether or not there was anything to signal); False when the read failed, so the
    caller can retry. The request stays in the ledger either way: `cancel` or the reconciler's escalation acts on it.
    """
    try:
        job = ctx.ledger.read()["jobs"][job_id]
    except (L.LedgerError, KeyError):
        return False
    if job["state"] == "launched" and job["cancel"] is not None and job["dispatcher"]:
        signal_cancel(ctx, job_id, job["dispatcher"])
    return True


def not_launched(ctx, job_id, error):
    obs = synth("failed", "not_launched", f"dispatcher could not be started: {error.__class__.__name__}: {error}")
    bank(ctx.ledger, job_id, obs)
    return 1, {"job_id": job_id, "state": "banked", "outcome": "failed", "cause": "not_launched"}


def start_job(ctx, opts):
    job_id = admit(ctx, opts)
    try:
        proc = spawn(ctx, opts, job_id)
    except OSError as error:
        return not_launched(ctx, job_id, error)
    try:
        pending = record_dispatcher(ctx, job_id, proc.pid)
    except L.LedgerDurabilityUnknown:
        honour_cancel(ctx, job_id)
        return 7, {"job_id": job_id, "state": "launched", "dispatcher_pid": proc.pid, "committed": True,
                   "durable": "unknown"}
    except L.LedgerError:
        return 6, {"job_id": job_id, "state": "launching", "dispatcher_pid": proc.pid}
    if pending:
        signal_cancel(ctx, job_id, pending)
        if reconcile_acted() and ctx.ledger.read()["jobs"][job_id]["state"] != "banked":
            return 5, {"job_id": job_id, "state": "launched", "changed": True,
                       "note": "a pending cancel was signalled; the job is not banked yet: run reconcile"}
    return 0, {"job_id": job_id, "state": "launched"}


def try_record(ctx, job_id, proc):
    """Step 3 for `run`: (recorded, retry_cancel). `recorded` once the dispatcher is recorded (or known committed);
    `retry_cancel` when a pending cancel could not be looked up yet."""
    try:
        pending = record_dispatcher(ctx, job_id, proc.pid)
    except L.LedgerDurabilityUnknown:
        return True, not honour_cancel(ctx, job_id)
    except L.LedgerError:
        return False, False
    if pending:
        signal_cancel(ctx, job_id, pending)
    return True, False


def wait_dispatcher(ctx, job_id, proc):
    """Wait as the dispatcher's parent. Step 3 is retried while waiting, so a cancel request is honoured."""
    recorded, retry = False, False
    while True:
        if not recorded:
            recorded, retry = try_record(ctx, job_id, proc)
        elif retry:
            retry = not honour_cancel(ctx, job_id)  # until its read succeeds
        try:
            return proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            continue


def run_job(ctx, opts):
    job_id = admit(ctx, opts)
    try:
        proc = spawn(ctx, opts, job_id)
    except OSError as error:
        return not_launched(ctx, job_id, error)
    pid = proc.pid
    result_path = str(ctx.runs / job_id / "result.json")
    record = dispatcher_record(pid)  # read while the child is unreaped: its pid cannot have been recycled yet
    code = wait_dispatcher(ctx, job_id, proc)
    try:
        return finish_run(ctx, job_id, pid, code, record)
    except L.LedgerDurabilityUnknown:
        return 7, {"job_id": job_id, "committed": True, "durable": "unknown"}
    except L.LedgerError:  # once launched, run never exits 3 or 4
        from modules.continuity import reconcile
        return 6, {"job_id": job_id, "state": "launched", "dispatcher_pid": pid, "result_path": result_path,
                   "changed": reconcile.acted()}


def finish_run(ctx, job_id, pid, code, record=None):
    """Bank what the dispatcher left. The outcome reported comes from the transaction that banked it.

    `record` is the dispatcher record taken before the child was reaped; a reaped pid is never read for its ticks.
    """
    from modules.continuity import reconcile
    with ctx.ledger.transaction() as doc:
        job = doc["jobs"][job_id]
        if job["state"] == "launching":
            job["dispatcher"] = dict(record) if record else dispatcher_record(pid)
            job["state"] = "launched"
        obs = None
        if job["state"] != "banked":
            obs, _, _ = observe_result(ctx, job, "wait")
            if obs is not None:
                obs["exit_code"] = code
                bank_in(doc, job_id, obs)
        if job["state"] == "banked":
            return exit_for(job["result"]["outcome"]), brief(job)
        gone = dict(job)
    # Neither a valid result.json nor a valid envelope: nothing is synthesised here. The reconciler's rows kill what
    # the dispatcher left behind (worker group, marker sweep), apply rule W, and only then bank.
    with reconcile.reconcile_lock(ctx, ctx.cfg["lock_wait_seconds"]) as got:
        if not got:
            return 5, {"job_id": job_id, "state": "launched", "pending": True, "changed": reconcile.acted(),
                       "note": "no result and the dispatcher is gone: left for the reconciler (reconcile.lock is busy)"}
        try:
            row = reconcile.gone_rows(ctx, gone, False)
        except L.LedgerError:
            raise
        except Exception as error:  # noqa: BLE001 - one job's bad input: left for the reconciler, no traceback
            return 5, {"job_id": job_id, "state": "launched", "pending": True, "changed": reconcile.acted(),
                       "note": f"internal error while settling ({error.__class__.__name__}): left for the reconciler"}
    if row["status"] != "banked":
        return 5, {"job_id": job_id, "state": "launched", "pending": True, "changed": reconcile.acted(), "reconcile": row}
    return exit_for(row["outcome"]), {"job_id": job_id, "role": gone["role"], "state": "banked",
                                      "outcome": row["outcome"], "cause": row["cause"], "owner": gone["owner"]}


# --- wait / status / collect -------------------------------------------------------------------------

def wait_job(ctx, job_id, timeout=None):
    from modules.continuity import reconcile
    end = None if timeout is None else time.monotonic() + timeout
    stable = False
    while True:
        job = ctx.ledger.read()["jobs"].get(job_id)
        if job is None:
            raise L.Refused(f"unknown job {job_id}")
        if job["state"] != "banked":
            collect_all(ctx, only=job_id)
            job = ctx.ledger.read()["jobs"][job_id]
        if job["state"] != "banked":
            code, out = reconcile.reconcile_all(ctx, only=job_id)  # banks it if the dispatcher is gone
            if out.get("changed"):
                raise reconcile.SideEffectsDone(out["error"])
            job = ctx.ledger.read()["jobs"][job_id]
            row = next((r for r in out.get("results") or [] if r.get("job_id") == job_id), None)
            open_row = (job["state"] != "banked" and row is not None and row["status"] == "unresolved"
                        and not (job["dispatcher"] and reconcile.dispatcher_verdict(ctx, job)[0] == "alive"))
            if open_row:  # only a dispatcher that is gone or verified dead: a live job keeps waiting
                refused = reconcile.bank_refusal(ctx, job_id)
                if refused is not None:  # waiting cannot change this: only the operator can close it
                    raise L.Refused(L.cap_text(f"job {job_id}: its result is refused ({refused}); "
                                               "close it with reconcile --force-bank", 400))
                if end is None and not row.get("changed"):
                    if stable:  # the same untouched unresolved row twice running: nothing here will change it
                        raise L.Refused(L.cap_text(f"job {job_id} is unresolved and nothing is acting on it; close "
                                                   "it with reconcile or reconcile --force-bank", 400))
                    stable = True
                else:
                    stable = False
            else:
                stable = False
        if job["state"] == "banked":
            return exit_for(job["result"]["outcome"]), brief(job)
        if end is not None and time.monotonic() >= end:
            return 5, {"job_id": job_id, "state": job["state"], "pending": True}
        time.sleep(0.2)


def summary(doc, job_id=None):
    jobs = {j: brief(job) for j, job in sorted(doc["jobs"].items()) if job_id in (None, j)}
    counts = {"launching": 0, "launched": 0, "banked": 0, "succeeded": 0, "failed": 0, "unsupported": 0}
    for job in doc["jobs"].values():
        counts[job["state"]] += 1
        if job["result"]:
            counts[job["result"]["outcome"]] += 1
    return {"storage": "ok", "revision": doc["revision"], "drain": doc["drain"], "counts": counts, "jobs": jobs}


def status(ctx, job_id=None):
    try:
        ctx.ledger.read()
    except L.LedgerInvalid as error:
        return 4, {"storage": "invalid", "reason": str(error)}
    if job_id is not None and job_id not in ctx.ledger.read()["jobs"]:
        raise L.Refused(f"unknown job {job_id}")
    collect_all(ctx)
    return 0, summary(ctx.ledger.read(), job_id)


def collect(ctx):
    collect_all(ctx)
    doc = ctx.ledger.read()
    arrivals = [j for j in doc["jobs"].values() if j["state"] == "banked" and j["acknowledged_revision"] is None]
    arrivals.sort(key=lambda j: j["result"]["bank_revision"], reverse=True)
    items = [dict(brief(j), banked_at=j["result"]["banked_at"], bank_revision=j["result"]["bank_revision"],
                  late_results=len(j["post_bank"])) for j in arrivals[:50]]
    return 0, {"arrivals": items, "total": len(arrivals)}


# --- cancel ------------------------------------------------------------------------------------------------

def cancel_job(ctx, job_id, session=None, force=False):
    from modules.continuity import reconcile
    by = L.parse_session(session) if session else {"harness": "cli", "session_id": None}
    with ctx.ledger.transaction() as doc:
        job = doc["jobs"].get(job_id)
        if job is None:
            raise L.Refused(f"unknown job {job_id}")
        if job["state"] == "banked":
            return 0, {"job_id": job_id, "already_banked": True}
        owner = L.session_key(job["owner"])
        if owner is not None and not force and L.session_key(by) != owner:
            raise L.Refused(f"job {job_id} is owned by session {owner}; cancel it from there or use --force")
        if job["cancel"] is None:
            job["cancel"] = {"requested_at": L.now(), "by": by}
            touch(doc, job)
    with reconcile.side_effects_guard():  # from here on a signal may have been sent: no exit 3 or 4 after it
        return _cancel_acting(ctx, job_id, force)


def _cancel_acting(ctx, job_id, force):
    from modules.continuity import reconcile
    job = ctx.ledger.read()["jobs"][job_id]
    if job["kind"] == "external":
        return 0, {"job_id": job_id, "cancel": "requested", "signalled": False}
    if job["state"] == "launching":
        verdict = reconcile.submitter_verdict(job)
        if verdict != "gone":
            return 0, {"job_id": job_id, "state": "launching", "cancel": "requested"}  # the submitter signals
        with reconcile.reconcile_lock(ctx, ctx.cfg["lock_wait_seconds"]) as got:
            if not got:
                return _cancel_busy(job_id)
            adopted = reconcile.adopt_launching(ctx, job_id)
            if adopted is None:
                row = reconcile.settle_job(ctx, job_id, escalate=True, own_acted=reconcile.acted())
                return _cancel_report(ctx, job_id, row)
        job = ctx.ledger.read()["jobs"][job_id]
    if job["state"] == "launched":
        verdict, _ = reconcile.dispatcher_verdict(ctx, job)
        if verdict == "alive":
            reconcile.signal_dispatcher(ctx, job_id, job["dispatcher"], (signal.SIGCONT, signal.SIGTERM))
        end = time.monotonic() + ctx.cfg["cancel_wait_seconds"]
        while time.monotonic() < end:
            collect_all(ctx, only=job_id)
            job = ctx.ledger.read()["jobs"][job_id]
            if job["state"] == "banked" or reconcile.dispatcher_verdict(ctx, job)[0] != "alive":
                break
            time.sleep(0.1)
        job = ctx.ledger.read()["jobs"][job_id]
        if job["state"] != "banked":
            with reconcile.reconcile_lock(ctx, ctx.cfg["lock_wait_seconds"]) as got:
                if not got:
                    return _cancel_busy(job_id)
                row = reconcile.settle_job(ctx, job_id, escalate=True, kill_now=force,
                                           own_acted=reconcile.acted())
            return _cancel_report(ctx, job_id, row)
    return _cancel_report(ctx, job_id, None)


def _cancel_busy(job_id):
    """The request is recorded; another reconciler holds the lock the kill and rule-W rows need."""
    from modules.continuity import reconcile
    if reconcile.acted():
        return 5, {"job_id": job_id, "cancel": "requested", "pending": True, "changed": True,
                   "note": "reconcile.lock is busy: a signal was already sent; run reconcile or cancel again"}
    return 5, {"job_id": job_id, "cancel": "requested", "pending": True, "changed": False,
               "note": "reconcile.lock is busy: nothing was signalled yet; run reconcile or cancel again"}


def _cancel_report(ctx, job_id, row):
    job = ctx.ledger.read()["jobs"][job_id]
    report = brief(job)
    if row is not None:
        report["reconcile"] = row
    return 0, report


# --- drain ------------------------------------------------------------------------------------------------------

def drain_start(ctx, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise L.Refused("drain start needs --reason")
    with ctx.ledger.transaction() as doc:
        if doc["drain"] is None:
            doc["drain"] = {"since": L.now(), "reason": reason.strip()[:500]}
        return 0, {"drain": doc["drain"]}


def drain_end(ctx):
    with ctx.ledger.transaction() as doc:
        doc["drain"] = None
    return 0, {"drain": None}


def drain_status(ctx):
    doc = ctx.ledger.read()
    pending = sorted(j for j, job in doc["jobs"].items() if job["state"] != "banked")
    return 0, {"drain": doc["drain"], "pending": pending}


def drain_wait(ctx, timeout=None):
    from modules.continuity import reconcile
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        code, out = reconcile.reconcile_all(ctx)  # a skipped reconcile changes nothing: the jobs below stay pending
        if out.get("changed"):
            raise reconcile.SideEffectsDone(out["error"])
        collect_all(ctx)
        doc = ctx.ledger.read()
        pending = sorted(j for j, job in doc["jobs"].items() if job["state"] != "banked")
        if not pending:
            return 0, {"state": "DRAINED", "drain": doc["drain"]}
        if end is not None and time.monotonic() >= end:
            return 5, {"state": "PENDING", "pending": pending, "drain": doc["drain"]}
        time.sleep(0.3)


# --- external jobs ---------------------------------------------------------------------------------------------------

def external_register(ctx, job_id, role, access, session, owner_pid):
    if access != "read":
        raise L.Refused("an external job must be read access: writers go through dispatch.py worktrees")
    if not dispatch.NAME.fullmatch(job_id or "") or not dispatch.NAME.fullmatch(role or ""):
        raise L.Refused("invalid job id or role")
    owner = L.parse_session(session)
    if not L.is_int(owner_pid) or owner_pid < 1:
        raise L.Refused("--owner-pid must be a positive integer")
    me = os.getpid()
    with ctx.ledger.transaction() as doc:
        if doc["drain"] is not None:
            raise L.Refused(f"draining since {doc['drain']['since']}: {doc['drain']['reason']}")
        if job_id in doc["jobs"]:
            raise L.Refused(f"job id {job_id!r} already exists")
        active = sum(1 for j in doc["jobs"].values() if j["state"] != "banked")
        if active >= ctx.cfg["max_active_jobs"]:
            raise L.Refused(f"{active} jobs are not banked yet (max_active_jobs {ctx.cfg['max_active_jobs']})")
        doc["jobs"][job_id] = {
            "job_id": job_id, "kind": "external", "role": role, "access": "read", "owner": owner,
            "state": "launched", "created_at": L.now(), "updated_at": L.now(), "updated_revision": doc["revision"] + 1,
            "submitter": {"pid": me, "start_ticks": dispatch.proc_start_ticks(me)},
            "dispatcher": dispatcher_record(owner_pid), "workdir": None, "worktree_plan": None, "prompt_sha256": None,
            "budget": {"tokens": None}, "cancel": None, "result": None, "post_bank": [],
            "acknowledged_revision": None, "context": None,
        }
    return 0, {"job_id": job_id, "state": "launched"}


def external_bank(ctx, job_id, outcome, cause, result_file=None):
    if outcome not in ("succeeded", "failed") or cause not in ("completed", "cancelled", "orphaned_external"):
        raise L.Refused("outcome must be succeeded|failed and cause completed|cancelled|orphaned_external")
    if (outcome == "succeeded") != (cause == "completed"):
        raise L.Refused("succeeded goes with cause completed, and only with it")
    job = ctx.ledger.read()["jobs"].get(job_id)
    if job is None or job["kind"] != "external":
        raise L.Refused(f"{job_id} is not a registered external job")
    evidence, sha = [], None
    if result_file:
        try:
            data = L.read_file(result_file, MAX_EXTERNAL_BYTES)
        except OSError as error:
            raise L.Refused(f"result file unreadable: {error}")
        sha = L.sha256_hex(data)
        stored = ctx.spool / f"{job_id}.external.txt"
        write_private(stored, data.decode("utf-8", "replace"))
        evidence = [{"kind": "external-result", "reference": str(stored), "sha256": sha}]
    try:
        record = dispatch.make_result(job_id, outcome, evidence)
    except Exception as error:  # noqa: BLE001
        raise L.Refused(f"result record invalid: {error}")
    obs = {"outcome": outcome, "cause": cause, "source": "external", "result_sha256": sha, "record": record,
           "exit_code": None, "usage": None, "worktree": None, "evidence": evidence, "note": None,
           "trust": "recovery" if cause == "orphaned_external" else "dispatcher", "interrupted": False}
    done = bank(ctx.ledger, job_id, obs)
    job = ctx.ledger.read()["jobs"][job_id]
    return 0, dict(brief(job), banked_now=done["banked_now"])
