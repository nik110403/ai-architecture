"""Recovery table for jobs the dispatcher can no longer settle itself (implements the orchestration README table).

Order per job: decide dispatcher liveness first (R1), only then read result.json and start.json; a dispatcher
seen alive never leads to a dispatcher-gone row in the same pass. Signals go through pidfd with a start-ticks
re-check. Nothing here runs while the ledger lock is held: banking is the only locked step.
"""

import contextlib
import copy
import datetime
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from modules.continuity import jobs
from modules.continuity import ledger as L
from modules.orchestration import dispatch

STUCK_TERM_WAIT = 30.0
KILL_WAIT = 10.0
SWEEP_TIMEOUT = 60.0
SWEEP_PASSES = 10
SWEEP_NOTE = "the marker sweep cannot see an env -i or non-dumpable descendant"
SWEEP_CODE = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.spec_from_file_location('dispatch_sweep', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "killed, unknown = module.sweep_marker(sys.argv[2])\n"
    "print(json.dumps({'killed': sorted(killed), 'unknown': unknown}))\n"
)


# --- "something was done" -------------------------------------------------------------------------------------
# Set when a signal was delivered, a sweep killed something or a worktree was removed. It is per process and reset
# once per command (`continuity.main`), never by a row: once set, no ledger failure may surface as exit 3 or 4
# ("nothing changed"). `continuity.main` enforces that for every command (the rule is per command, at the one entry
# point); `side_effects_guard` and `bank_acted` keep the inner guards so a row can still say `changed`. Inside one job's
# `settle_job` the flag is that job's own (it picks the row text); afterwards it is folded back into the command's.

_ACTED = [False]


def reset_acted():
    _ACTED[0] = False


def acted():
    return _ACTED[0]


# --- /proc primitives -----------------------------------------------------------------------------------

def have_proc():
    return os.path.exists("/proc/self/stat")


def stat_fields(pid):
    """{state, ppid, pgid, sid, ticks} from /proc/<pid>/stat, or None when unreadable."""
    try:
        rest = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"state": rest[0], "ppid": int(rest[1]), "pgid": int(rest[2]), "sid": int(rest[3]), "ticks": int(rest[19])}
    except (OSError, ValueError, IndexError):
        return None


def cmdline(pid):
    try:
        return [a.decode("utf-8", "surrogateescape") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return None


def has_pair(argv, flag, value):
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


def is_dispatcher_argv(argv, job_id, runs):
    """`<DISPATCH> run ... --job-id ID --run-dir RUNS`: the dispatcher path followed by `run`, not just the token pair."""
    path = str(jobs.DISPATCH)
    return (any(argv[i] == path and argv[i + 1] == "run" for i in range(len(argv) - 1))
            and has_pair(argv, "--job-id", job_id) and has_pair(argv, "--run-dir", str(runs)))


def cmdline_matches(pid, job_id, runs):
    argv = cmdline(pid)
    return argv is not None and is_dispatcher_argv(argv, job_id, runs) and dispatch.proc_euid(pid) == os.geteuid()


def liveness(pid, ticks, boot=None, *, job_id=None, runs=None):
    """R1: ("alive"|"gone"|"unknown", reason). `unknown` never leads to a signal or a bank."""
    if not have_proc():
        return "unknown", "no /proc on this platform"
    current_boot = jobs.boot_id()
    if boot is not None and current_boot is not None and boot != current_boot:
        return "gone", "recorded boot id differs"
    if not os.path.exists(f"/proc/{pid}"):
        return "gone", "no such pid"
    fields = stat_fields(pid)
    if fields is None:
        return ("gone", "vanished") if not os.path.exists(f"/proc/{pid}") else ("unknown", "stat unreadable")
    if fields["state"] == "Z":
        return "gone", "zombie"
    if ticks is not None:
        if not L.is_int(ticks):
            return "unknown", "recorded start ticks are not an integer"
        return ("alive", "ticks match") if fields["ticks"] == ticks else ("gone", "start ticks differ")
    if job_id is None:
        return "unknown", "start ticks unrecorded"
    if cmdline(pid) is None:
        return "unknown", "start ticks unrecorded and cmdline unreadable"
    if cmdline_matches(pid, job_id, runs):
        return "alive", "cmdline matches"
    return "unknown", "start ticks unrecorded and cmdline does not match"


def dispatcher_verdict(ctx, job):
    disp = job["dispatcher"]
    return liveness(disp["pid"], disp["start_ticks"], disp["boot_id"], job_id=job["job_id"], runs=ctx.runs)


def submitter_verdict(job):
    sub = job["submitter"]
    verdict, _ = liveness(sub["pid"], sub["start_ticks"])
    return verdict


def signal_pid(pid, verify, number):
    """Deliver `number` while `verify()` still says alive. ("sent"|"gone"|"unresolved", note)."""
    pidfd, note = None, None
    try:
        pidfd = os.pidfd_open(pid)
    except AttributeError:
        note = "signal: no pidfd"
    except ProcessLookupError:
        return "gone", None
    except OSError as error:
        if error.errno != errno.ENOSYS:
            return "unresolved", f"pidfd_open failed: {error.__class__.__name__}"
        note = "signal: no pidfd"
    try:
        verdict = verify()
        if verdict == "gone":
            return "gone", note
        if verdict != "alive":
            return "unresolved", note
        try:
            if pidfd is not None:
                signal.pidfd_send_signal(pidfd, number)
            else:
                os.kill(pid, number)
        except ProcessLookupError:
            return "gone", note
        except PermissionError:
            return "unresolved", "signal refused: not permitted"
        _ACTED[0] = True
        return "sent", note
    finally:
        if pidfd is not None:
            os.close(pidfd)


def signal_dispatcher(ctx, job_id, disp, numbers):
    """Send `numbers` in order to the recorded dispatcher after an R1 check. Returns the last (status, note)."""
    last = ("gone", None)
    for number in numbers:
        last = signal_pid(disp["pid"], lambda: liveness(disp["pid"], disp["start_ticks"], disp["boot_id"],
                                                        job_id=job_id, runs=ctx.runs)[0], number)
        if last[0] != "sent":
            break
    return last


def wait_gone(check, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check() == "gone":
            return True
        time.sleep(0.05)
    return check() == "gone"


# --- R2 stuck --------------------------------------------------------------------------------------------

def parse_iso(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def stuck_verdict(ctx, job):
    """("running"|"stuck"|"unresolved", reason) for a live dispatcher."""
    disp = job["dispatcher"]
    fields = stat_fields(disp["pid"])
    if fields is not None and fields["state"] in ("T", "t"):
        return "stuck", "dispatcher is stopped"
    elapsed = jobs.monotonic() - disp["launched_monotonic"]
    state, start = jobs.read_start(ctx, job["job_id"])
    timeout = None
    if state == "ok":
        try:
            timeout = (parse_iso(start["deadline"]) - parse_iso(start["started_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            timeout = None
    current = jobs.boot_id()
    if disp["boot_id"] is not None and disp["boot_id"] == current and timeout is not None:
        if elapsed > timeout + ctx.cfg["stuck_grace_seconds"]:
            return "stuck", f"alive {elapsed:.0f}s monotonic, over timeout {timeout:g}s plus grace"
        return "running", ""
    if elapsed < ctx.cfg["launch_grace_seconds"]:
        return "running", "within the launch grace"
    return "unresolved", "missing stuck-rule inputs (boot id, start.json or deadline) past the launch grace"


# --- R3 worker, group kill, marker sweep ------------------------------------------------------------------

def worker_verdict(job_id, runs, start):
    """"verified" | "gone" | "unknown" for the worker recorded in start.json (R3)."""
    pid, ticks = start.get("pid"), start.get("worker_start_ticks")
    if not L.is_int(pid) or pid < 1:
        return "unknown", "no worker pid recorded"
    if not have_proc():
        return "unknown", "no /proc"
    if not os.path.exists(f"/proc/{pid}"):
        return "gone", "no such pid"
    fields = stat_fields(pid)
    if fields is None:
        return "unknown", "stat unreadable"
    if fields["state"] == "Z":
        return "gone", "zombie"
    if ticks is None:
        return "unknown", "worker start ticks unrecorded"
    if fields["ticks"] != ticks:
        return "gone", "start ticks differ"
    needle = f"{dispatch.MARKER_VAR}={dispatch.job_marker(runs / job_id)}".encode()
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return "unknown", "environ unreadable"
    if needle not in data.split(b"\0"):
        return "unknown", "environ lacks the job marker"
    after = stat_fields(pid)
    if after is None or after["ticks"] != ticks:
        return "gone", "start ticks changed while reading"
    return "verified", ""


def kill_group_members(start):
    """SIGKILL every same-uid process in the recorded pgid that is at least as new as the worker, via pidfd."""
    pgid, floor, leader = start.get("pgid"), start.get("worker_start_ticks"), start.get("pid")
    if not L.is_int(pgid) or pgid < 1 or floor is None or pgid != leader:
        return 0  # the worker leads its own group; start.json alone never names another group
    worker = stat_fields(leader)
    if worker is None or worker["pgid"] != pgid:
        return 0
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fields = stat_fields(pid)
        if fields and fields["pgid"] == pgid and fields["ticks"] >= floor and dispatch.proc_euid(pid) == os.geteuid():
            members.append((pid, fields["ticks"]))
    sent = 0
    for pid, ticks in members:
        def verify(pid=pid, ticks=ticks):
            fields = stat_fields(pid)
            return "gone" if fields is None or fields["state"] == "Z" or fields["ticks"] != ticks else "alive"
        if signal_pid(pid, verify, signal.SIGKILL)[0] == "sent":
            sent += 1
    return sent


def behind_row(row):
    """The one predicate for "the ledger is behind the world": a row of a job this command acted on that is not banked
    (`settle_job` marks every such row `changed`). A live `running` or an untouched `unresolved` row is not behind."""
    return isinstance(row, dict) and row.get("status") != "banked" and bool(row.get("changed"))


def sweep_failed(swept):
    """True when the marker sweep did not run to completion (timed out or its child failed)."""
    return isinstance(swept["unknown"], str)


def sweep(marker):
    """Bounded marker sweeps in a child process; repeated until a pass kills nothing (at most 10 passes)."""
    killed, unknown = set(), 0
    for _ in range(SWEEP_PASSES):
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", SWEEP_CODE, str(dispatch.HERE / "dispatch.py"), marker],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd="/", text=True)
        try:
            out, _ = proc.communicate(timeout=SWEEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()  # reaps it and closes the pipe
            _ACTED[0] = True  # the child may have killed before it was stopped: unknown counts as done
            return {"killed": len(killed), "unknown": "unknown: timed out", "note": SWEEP_NOTE}
        try:
            data = json.loads(out)
            found, unknown = set(data["killed"]), data["unknown"]
        except (ValueError, KeyError, TypeError):
            _ACTED[0] = True  # its output is lost, so are its kills
            return {"killed": len(killed), "unknown": "unknown: sweep child failed", "note": SWEEP_NOTE}
        killed |= found
        if found:
            _ACTED[0] = True
        if not found:
            break
    return {"killed": len(killed), "unknown": unknown, "note": SWEEP_NOTE}


# --- worktree rule W ----------------------------------------------------------------------------------------------

def git_ok(workdir, *argv):
    """(returncode, stdout) via dispatch.git; any git trouble is (None, reason): the worktree is kept.

    `core.fsmonitor=false` on every call: the worker may have planted a hook in the repository's config, and
    `status` as well as `worktree remove` would run it.
    """
    try:
        done = dispatch.git(workdir, "-c", "core.fsmonitor=false", *argv)
    except dispatch.Refusal as refusal:
        return None, refusal.reason
    return done.returncode, done.stdout


def keep(plan, reason):
    return {"path": plan["path"], "branch": plan["branch"], "kept": True, "reason": reason}


REMOVAL_FILE = "rule_w.json"


def well_formed(planned):
    return isinstance(planned, dict) and isinstance(planned.get("path"), str) and isinstance(planned.get("branch"), str)


def corroborated(workdir, plan):
    """True only when git itself agrees the planned branch and path are gone (any doubt is False)."""
    code, _ = git_ok(workdir, "rev-parse", "--verify", "--quiet", f"refs/heads/{plan['branch']}")
    if code is None or code == 0:
        return False
    code, text = git_ok(workdir, "worktree", "list", "--porcelain")
    if code != 0:
        return False
    listed = {os.path.realpath(line.partition(" ")[2]) for line in text.splitlines() if line.startswith("worktree ")}
    return os.path.realpath(plan["path"]) not in listed


def persisted_removal(run_dir, plan, workdir):
    """The removal record a failed bank left behind (so a retry still reports it), or None.

    The file is worker-writable: it counts only when git confirms the branch and the worktree are gone.
    """
    if run_dir is None:
        return None
    try:
        record = L.strict_loads(L.read_file(Path(run_dir) / REMOVAL_FILE, 1 << 16))
    except (OSError, ValueError, RecursionError):
        return None
    if (isinstance(record, dict) and set(record) == {"path", "branch", "kept", "reason"} and record["kept"] is False
            and record["path"] == plan["path"] and record["branch"] == plan["branch"]
            and isinstance(record["reason"], str)):
        return dict(record, reason=L.cap_text(record["reason"])) if corroborated(workdir, plan) else None
    return None


def rule_w(job, start, run_dir=None):
    """Remove a half-created worktree and branch only when provably empty; else keep and say why."""
    plan = job["worktree_plan"]
    planned = start.get("worktree") if isinstance(start, dict) else None
    if planned is None and plan is None:
        return None
    if plan is not None and not well_formed(plan):
        return keep({"path": "?", "branch": "?"}, "the ledger's worktree plan is malformed")
    if planned is not None and not well_formed(planned):
        return keep(plan or {"path": "?", "branch": "?"}, "start.json worktree entry is malformed")
    if plan is None or planned is None or planned["path"] != plan["path"] or planned["branch"] != plan["branch"]:
        # only the ledger's own plan is ever banked: start.json is worker-writable
        return keep(plan or {"path": "?", "branch": "?"}, "start.json and the ledger disagree about the worktree")
    workdir, path, branch = job["workdir"], plan["path"], plan["branch"]
    if not workdir:
        return keep(plan, "no main repository recorded")
    if not os.path.exists(path):
        record = persisted_removal(run_dir, plan, workdir)  # an earlier pass removed it (a failed bank is retried)
        if record is not None:
            return record
        code, _ = git_ok(workdir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if code != 1:  # the branch exists (0) or git said nothing usable (None, 128): keep on any doubt
            return keep(plan, "the planned path is absent but its branch may still exist")
        return None  # nothing was created
    steps = (
        ("rev-parse", path, ("rev-parse", "--show-toplevel")),
        ("list", workdir, ("worktree", "list", "--porcelain")),
        ("symbolic-ref", path, ("symbolic-ref", "HEAD")),
        ("only-head", path, ("rev-list", "HEAD", "--not", "--branches")),
        ("unique", workdir, ("rev-list", "--count", branch, "--not", f"--exclude={branch}", "--branches")),
        ("status", path, ("status", "--porcelain", "--ignored", "--untracked-files=all")),
    )
    out = {}
    for name, where, argv in steps:
        code, text = git_ok(where, *argv)
        if code != 0:
            return keep(plan, f"git {name} failed or was unavailable")
        out[name] = text
    if os.path.realpath(out["rev-parse"].strip()) != os.path.realpath(path):
        return keep(plan, "worktree top level differs from the planned path")
    listed = False
    block = {}
    for line in out["list"].splitlines() + [""]:
        if line:
            key, _, value = line.partition(" ")
            block[key] = value
        else:
            if block.get("worktree") and os.path.realpath(block["worktree"]) == os.path.realpath(path):
                listed = block.get("branch") == f"refs/heads/{branch}"
            block = {}
    if not listed:
        return keep(plan, "git does not list the planned path on the planned branch")
    if out["symbolic-ref"].strip() != f"refs/heads/{branch}":
        return keep(plan, "HEAD is not on the planned branch")
    if out["only-head"].strip() or out["unique"].strip() != "0":
        return keep(plan, "the branch holds a commit that no other branch has")
    if out["status"].strip():
        return keep(plan, "the worktree has untracked, ignored or modified files")
    code, text = git_ok(workdir, "worktree", "remove", path)
    if code != 0:
        return keep(plan, f"worktree remove failed: {str(text)[:200]}")
    code, text = git_ok(workdir, "branch", "-D", branch)
    if code != 0:
        return keep(plan, f"worktree removed but branch -D failed: {str(text)[:200]}")
    _ACTED[0] = True
    removed = {"path": path, "branch": branch, "kept": False, "reason": "half-created worktree was clean and empty: removed"}
    if run_dir is not None:  # best effort: a bank that fails now must not lose the fact on the retry
        try:
            jobs.write_private(Path(run_dir) / REMOVAL_FILE, json.dumps(removed))
        except OSError:
            pass
    return removed


# --- the table ---------------------------------------------------------------------------------------------------------

class SideEffectsDone(L.LedgerError):
    """Processes were killed or a worktree removed, then the bank did not commit: the ledger is unchanged, the world is not.

    `own` is False when it was another job in the same run that acted, not the job the error is raised for."""
    own = True


@contextlib.contextmanager
def side_effects_guard():
    """After any signal, sweep kill or worktree removal in this process, a ledger failure is `SideEffectsDone`."""
    try:
        yield
    except (L.LedgerBusy, L.LedgerInvalid, L.LedgerStorageError) as error:
        if not _ACTED[0]:
            raise
        raise SideEffectsDone(f"{error}; processes were already killed or a worktree removed, the ledger was not "
                              f"updated: run reconcile again") from error


def report(job_id, status, action, **extra):
    return {"job_id": job_id, "status": status, "action": action, **extra}


def banked(ctx, job_id, obs, action):
    done = jobs.bank(ctx.ledger, job_id, obs)  # outcome and cause come from the committing transaction, no re-read
    return report(job_id, "banked", action, banked_now=done["banked_now"], outcome=done["outcome"], cause=done["cause"])


def bank_acted(ctx, job_id, obs, action, acted):
    """`banked`, but a ledger failure after kills or a worktree removal is `SideEffectsDone`, not "nothing changed"."""
    try:
        return banked(ctx, job_id, obs, action)
    except (L.LedgerBusy, L.LedgerInvalid, L.LedgerStorageError) as error:
        if not (acted or _ACTED[0]):
            raise
        raise SideEffectsDone(f"{error}; the bank did not commit, but processes were already killed or a worktree "
                              f"removed: run reconcile again") from error


def match_dispatcher(pid, job_id, runs):
    """(pid, start_ticks) when `pid` carries this job's dispatcher argv and leads its own session, else None.

    Read in the order ticks, cmdline, ticks: equal ticks on both sides, so one process was looked at throughout.
    A forked child before exec has the argv but not pid == pgid == sid.
    """
    before = stat_fields(pid)
    if before is None or before["state"] == "Z" or not cmdline_matches(pid, job_id, runs):
        return None
    after = stat_fields(pid)
    if after is None or after["state"] == "Z" or after["ticks"] != before["ticks"]:
        return None
    if not pid == after["pgid"] == after["sid"]:
        return None
    return pid, before["ticks"]


def find_dispatcher_processes(ctx, job_id):
    out = []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != os.getpid():
            found = match_dispatcher(int(entry), job_id, ctx.runs)
            if found:
                out.append(found)
    # A worker in its pre-exec window carries the dispatcher's argv and leads its own session too; its parent is the
    # dispatcher. Prefer the match whose parent is not itself a match. More than one left means ambiguous.
    pids = {pid for pid, _ in out}
    top = [m for m in out if (stat_fields(m[0]) or {}).get("ppid") not in pids]
    return sorted(top)


class NotAdopted(Exception):
    pass


def adopt_launching(ctx, job_id):
    """The `launching` cmdline-lookup row: record a live process carrying this job's argv as the dispatcher.

    Returns the adopted pid, or None. The (pid, ticks) captured at the match is what is stored, after a re-check of
    cmdline and ticks inside the transaction; a pending cancel signals the committed record, never a fresh one.
    """
    if not have_proc():
        return None
    found = find_dispatcher_processes(ctx, job_id)
    if len(found) != 1:  # none, or ambiguous: signal and record nothing
        return None
    pid, ticks = found[0]
    try:
        with ctx.ledger.transaction() as doc:
            job = doc["jobs"][job_id]
            if job["state"] != "launching":
                return pid
            again = stat_fields(pid)
            if (not cmdline_matches(pid, job_id, ctx.runs) or again is None or again["state"] == "Z"
                    or again["ticks"] != ticks):
                raise NotAdopted()
            job["dispatcher"] = {"pid": pid, "start_ticks": ticks, "launched_monotonic": jobs.monotonic(),
                                 "boot_id": jobs.boot_id()}
            job["state"] = "launched"
            jobs.touch(doc, job)
            committed, cancel = dict(job["dispatcher"]), job["cancel"] is not None
    except NotAdopted:
        return None
    if cancel:
        signal_dispatcher(ctx, job_id, committed, (signal.SIGCONT, signal.SIGTERM))
    return pid


def external_row(ctx, job, dry):
    job_id, disp = job["job_id"], job["dispatcher"]
    verdict, why = liveness(disp["pid"], disp["start_ticks"], disp["boot_id"])
    if verdict == "unknown":
        return report(job_id, "unresolved", "owner liveness unknown", reason=why)
    if verdict == "alive":
        return report(job_id, "running", "external owner alive; never signalled")
    if dry:
        return report(job_id, "planned", "bank failed/orphaned_external (owner process gone)")
    session = L.session_key(job["owner"])
    obs = jobs.synth("failed", "orphaned_external",
                     f"owner process gone; inspect the native session {session} manually", source="reconcile")
    return banked(ctx, job_id, obs, "owner gone")


def launching_rows(ctx, job, dry, skip_grace=False):
    """Returns (report, None) when settled here, or (None, True) after adopting a dispatcher (continue as launched)."""
    job_id = job["job_id"]
    if submitter_verdict(job) != "gone":
        return report(job_id, "running", "submitter alive or unknown; leaving the launching job"), None
    if not have_proc():
        return report(job_id, "unresolved", "no /proc: cannot look for the dispatcher process"), None
    found = find_dispatcher_processes(ctx, job_id)
    if len(found) > 1:
        return report(job_id, "unresolved", "more than one process carries this job's dispatcher argv: not adopted"), None
    if found:
        if dry:
            return report(job_id, "planned", f"adopt process {found[0][0]} as the dispatcher"), None
        if adopt_launching(ctx, job_id) is None:
            return report(job_id, "unresolved", "the dispatcher process changed while adopting it: not adopted"), None
        return None, True
    rd = ctx.runs / job_id
    if not rd.exists():
        created = jobs.parse_created(job["created_at"])
        if not skip_grace and time.time() - created < ctx.cfg["launch_grace_seconds"]:
            return report(job_id, "running", "within the launch grace; no process, no job directory yet"), None
        if dry:
            return report(job_id, "planned", "bank failed/not_launched"), None
        return banked(ctx, job_id, jobs.synth("failed", "not_launched", "the submitter died before the dispatcher "
                                              "started"), "not launched"), None
    # runs/<id> exists, no process carries the argv: the dispatcher is gone if start.json agrees.
    state, start = jobs.read_start(ctx, job_id)
    if state == "ok" and L.is_int(start.get("dispatcher_pid")):
        ticks = start.get("dispatcher_start_ticks")
        verdict, _ = liveness(start["dispatcher_pid"], ticks) if ticks is None or L.is_int(ticks) else ("unknown", "")
        if verdict != "gone":
            return report(job_id, "unresolved", "start.json names a dispatcher that is not provably gone"), None
    return gone_rows(ctx, job, dry), None


def gone_rows(ctx, job, dry, acted=False):
    """`acted`: a signal was already sent for this job, so a bank that does not commit is `SideEffectsDone`."""
    job_id = job["job_id"]
    # once more, from disk: the first row wins. Source order is result.json, then the dispatcher's stdout envelope,
    # the same order `run` uses, so both bank the same outcome.
    obs, state, info = jobs.observe_result(ctx, job, "file")
    if obs is not None:
        if obs["source"] == "file":
            action = "result.json present"
        else:
            action = "refusal envelope" if obs["record"] is None else "stdout envelope"
        if dry:
            return report(job_id, "planned", f"bank {action}")
        return bank_acted(ctx, job_id, obs, action, acted)
    if state == "invalid":
        if dry:
            return report(job_id, "planned", "bank failed/invalid_result")
        return bank_acted(ctx, job_id, jobs.synth("failed", "invalid_result", f"result.json invalid: {info['reason']}",
                                              sha=info["sha256"]), "invalid result.json", acted)
    rd = ctx.runs / job_id
    if not rd.exists():
        if dry:
            return report(job_id, "planned", "bank failed/not_launched")
        return bank_acted(ctx, job_id, jobs.synth("failed", "not_launched", "dispatcher gone and no job directory"),
                      "not launched", acted)
    sstate, start = jobs.read_start(ctx, job_id)
    if sstate == "absent":
        if dry:
            return report(job_id, "planned", "bank failed/not_launched")
        return bank_acted(ctx, job_id, jobs.synth("failed", "not_launched", "job directory without start.json"),
                      "not launched", acted)
    if sstate == "invalid":
        if dry:
            return report(job_id, "planned", "bank failed/invalid_result (no signals)")
        return bank_acted(ctx, job_id, jobs.synth("failed", "invalid_result", "start.json unparseable or names another "
                                              "job; nothing was signalled"), "invalid start.json", acted)
    marker = dispatch.job_marker(rd)
    if start.get("state") == "starting":
        if dry:
            return report(job_id, "planned", "marker sweep, then bank lost_in_launch or not_launched, then rule W")
        swept = sweep(marker)
        if swept["killed"]:
            worktree = jobs_worktree_kept(job, "a process from the launch window was found and killed")
            obs = jobs.synth("failed", "lost_in_launch", f"swept {swept['killed']} process(es); {swept['note']}",
                             worktree=worktree)
            return bank_acted(ctx, job_id, obs, "dispatcher died in starting", True)
        if sweep_failed(swept):  # a sweep that did not finish proves nothing: no rule W, the worktree stays
            return report(job_id, "unresolved", f"marker sweep did not finish ({swept['unknown']}): worktree kept",
                          changed=True)  # the timed-out sweep may have killed before it was stopped
        worktree = rule_w(job, start, rd)
        obs = jobs.synth("failed", "not_launched", f"dispatcher died while starting; {swept['note']}",
                         worktree=worktree)
        return bank_acted(ctx, job_id, obs, "dispatcher died in starting", acted or bool(worktree and not worktree["kept"]))
    if start.get("state") == "running":
        verdict, why = worker_verdict(job_id, ctx.runs, start)
        if verdict == "unknown":
            return report(job_id, "unresolved", "worker identity unknown: not killed", reason=why)
        if dry:
            return report(job_id, "planned", "kill the worker group" if verdict == "verified" else "marker sweep")
        if verdict == "verified":
            verdict, why = worker_verdict(job_id, ctx.runs, start)  # re-verify right before listing the group
            killed = verdict == "verified"
            fate, label = (("killed", "killed") if killed else
                           ("identity unknown on re-check, not killed", "identity unknown") if verdict == "unknown"
                           else ("already gone", "already gone"))
            if killed:
                kill_group_members(start)
                signal_pid(start["pid"], lambda: worker_verdict(job_id, ctx.runs, start)[0] == "verified" and "alive"
                           or "gone", signal.SIGKILL)
            swept = sweep(marker)
            if sweep_failed(swept):  # the worker is dead but the sweep did not finish: not banked, sweep again later
                return report(job_id, "unresolved", f"worker {fate}; marker sweep "
                              f"did not finish ({swept['unknown']}): "
                              "worktree kept; a marked descendant may still be alive", changed=True)
            note = f"dispatcher gone; worker {fate}; swept {swept['killed']}; {swept['note']}"
            return bank_acted(ctx, job_id, jobs.synth("failed", "orphaned", note,
                                                      worktree=jobs_worktree_kept(job, "orphaned write job")),
                              f"orphaned worker {label}",
                              True)
        swept = sweep(marker)
        if sweep_failed(swept):  # as in the starting branch: a sweep that did not finish proves nothing, sweep again later
            return report(job_id, "unresolved", f"marker sweep did not finish ({swept['unknown']}): worktree kept",
                          changed=True)
        evidence, unhashed = [], ""
        stdout = rd / "stdout.txt"
        if stdout.is_file():
            try:
                evidence = [{"kind": "stdout-copy-unverified", "reference": str(stdout), "sha256": jobs.sha_file(stdout)}]
            except OSError as error:
                unhashed = f"; stdout.txt not hashed ({error})"
        note = f"dispatcher and worker gone; swept {swept['killed']}; stdout.txt is an unverified copy{unhashed}"
        return bank_acted(ctx, job_id, jobs.synth("failed", "lost", note, evidence=evidence,
                                                  worktree=jobs_worktree_kept(job, "lost write job")), "lost",
                          acted or bool(swept["killed"]))
    if start.get("state") == "finished":
        if dry:
            return report(job_id, "planned", "bank failed/result_missing")
        return bank_acted(ctx, job_id, jobs.synth("failed", "result_missing", "start.json says finished, result.json "
                                              "is missing"), "result missing", acted)
    if dry:
        return report(job_id, "planned", "bank failed/invalid_result")
    return bank_acted(ctx, job_id, jobs.synth("failed", "invalid_result", "start.json state unknown"), "invalid start.json", acted)


def jobs_worktree_kept(job, reason):
    plan = job["worktree_plan"]
    return keep(plan, reason) if plan else None


def launched_rows(ctx, job, dry, escalate, kill_now):
    job_id, disp = job["job_id"], job["dispatcher"]
    escalate = escalate or job["cancel"] is not None  # a recorded cancel request is honoured, not left to the stuck timeout
    verdict, why = dispatcher_verdict(ctx, job)  # 1. liveness first
    notes = []  # the signal notes (no pidfd, ...), reported in the row
    acted = False  # set once any signal reaches the dispatcher: from then on a failed bank is not "nothing changed"
    state, info = jobs.read_result_file(ctx, job)  # 2. only then the files
    if state == "valid":
        return banked(ctx, job_id, info, "result.json present") if not dry else report(
            job_id, "planned", "bank result.json")
    if verdict == "unknown":
        return report(job_id, "unresolved", "dispatcher identity unknown: not signalled", reason=why)
    if verdict == "alive":
        if state == "invalid":
            return report(job_id, "running", "result.json present but invalid; dispatcher alive: left alone")
        sverdict, swhy = ("stuck", "cancel escalation") if escalate else stuck_verdict(ctx, job)
        if sverdict == "running":
            return report(job_id, "running", "dispatcher alive", reason=swhy)
        if sverdict == "unresolved":
            return report(job_id, "unresolved", swhy)
        if dry:
            return report(job_id, "planned", f"stuck ({swhy}): SIGCONT, SIGTERM, then SIGKILL")

        def current():
            return dispatcher_verdict(ctx, job)[0]
        if not kill_now:
            status, note = signal_dispatcher(ctx, job_id, disp, (signal.SIGCONT, signal.SIGTERM))
            notes.append(note)
            acted = acted or status == "sent"
            if status == "unresolved":
                return report(job_id, "unresolved", f"could not signal the dispatcher ({note})", changed=acted,
                              notes=[n for n in notes if n])
            end = time.monotonic() + STUCK_TERM_WAIT
            while status == "sent" and time.monotonic() < end:
                found, got = jobs.read_result_file(ctx, job)
                if found == "valid":
                    return bank_acted(ctx, job_id, got, "result.json after SIGTERM", acted)
                if current() == "gone":
                    break
                time.sleep(0.1)
        if current() == "alive":
            status, note = signal_dispatcher(ctx, job_id, disp, (signal.SIGKILL,))
            notes.append(note)
            acted = acted or status == "sent"
            if status == "unresolved":
                return report(job_id, "unresolved", f"could not SIGKILL the dispatcher ({note})", changed=acted,
                              notes=[n for n in notes if n])
            if not wait_gone(current, KILL_WAIT):
                return report(job_id, "unresolved", "alive after SIGKILL", changed=acted, notes=[n for n in notes if n])
    row = gone_rows(ctx, job, dry, acted)
    if any(notes):
        row["notes"] = [n for n in notes if n]
    return row


def reconcile_job(ctx, job_id, dry=False, escalate=False, kill_now=False):
    job = ctx.ledger.read()["jobs"].get(job_id)
    if job is None:
        raise L.Refused(f"unknown job {job_id}")
    if job["state"] == "banked":
        return report(job_id, "banked", "already banked")
    if job["kind"] == "external":
        return external_row(ctx, job, dry)
    if job["state"] == "launching":
        row, continued = launching_rows(ctx, job, dry, skip_grace=escalate)
        if row is not None:
            return row
        job = ctx.ledger.read()["jobs"][job_id]
        if job["state"] == "banked":
            return report(job_id, "banked", "banked while adopting")
    return launched_rows(ctx, job, dry, escalate, kill_now)


@contextlib.contextmanager
def reconcile_lock(ctx, wait=0.0):
    """Yield True with `reconcile.lock` held, or False when another reconciler still has it after `wait` seconds."""
    L.require_platform()
    fd = os.open(ctx.state / "reconcile.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise L.LedgerStorageError("reconcile.lock is not a regular file owned by the effective user")
        end = time.monotonic() + wait
        while True:
            try:
                L.fcntl.flock(fd, L.fcntl.LOCK_EX | L.fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


def settle_job(ctx, job_id, dry=False, own_acted=False, **kwargs):
    """`reconcile_job`, but anything unexpected about ONE job (a hostile start.json, say) leaves that job unresolved.

    Ledger errors still propagate: they are not about one job (after a signal or removal: as `SideEffectsDone`).
    No traceback; `reconcile_all` continues with the other jobs. `own_acted`: this command already signalled for this
    job before calling (cancel's SIGTERM), so the job's own flag starts set.
    """
    outer = _ACTED[0]
    _ACTED[0] = bool(own_acted)  # this job's own flag: another job's kill must not rewrite this job's row text
    try:
        try:
            with side_effects_guard():
                row = reconcile_job(ctx, job_id, dry, **kwargs)
            if _ACTED[0] and row["status"] != "banked" and not row.get("changed"):
                row["changed"] = True  # this job acted: whatever it leaves open, the ledger is behind the world
            return row
        except (L.LedgerBusy, L.LedgerInvalid, L.LedgerStorageError) as error:
            if outer:  # an earlier job acted: the command did change things
                done = SideEffectsDone(f"{error}; another job in this run had already killed processes or removed a "
                                       f"worktree, the ledger was not updated: run reconcile again")
                done.own = False
                raise done from error
            raise
        except L.ResultRefused as error:  # this job's result cannot be stored; the ledger and the other jobs are fine
            return _settle_failed(job_id, error, f"the result cannot be banked ({error}); use --force-bank")
        except L.LedgerError:
            raise
        except Exception as error:  # noqa: BLE001 - one job's bad input must not stop the others
            return _settle_failed(job_id, error)
    finally:
        _ACTED[0] = _ACTED[0] or outer


def _settle_failed(job_id, error, action="internal error while reconciling this job; use --force-bank"):
    if acted():  # this job already killed or removed something: not "unresolved, nothing changed"
        raise SideEffectsDone(f"internal error ({error.__class__.__name__}) after processes were killed or a "
                              f"worktree removed: run reconcile again") from error
    return report(job_id, "unresolved", action, reason=error.__class__.__name__)


def reconcile_all(ctx, dry=False, only=None):
    doc = ctx.ledger.read()
    pending = sorted(j for j, job in doc["jobs"].items() if job["state"] != "banked" and only in (None, j))
    with reconcile_lock(ctx) as got:
        if not got:
            return 5, {"skipped": "another reconcile is running", "pending": pending}
        rows, failed = [], None
        for job_id in pending:
            try:
                rows.append(settle_job(ctx, job_id, dry))
            except SideEffectsDone as error:  # this job changed the world, not the ledger: say so and go on
                failed = error
                text = ("processes were killed or a worktree removed, the bank did not commit: run reconcile again"
                        if error.own else "another job in this run acted and the ledger failed for this job: run "
                        "reconcile again")
                rows.append(report(job_id, "unresolved", text, changed=True, error=str(error)))
    still = [r["job_id"] for r in rows if r["status"] != "banked"]
    if failed is not None or any(behind_row(r) for r in rows):  # the acted rule
        error = str(failed) if failed is not None else ("processes were killed or a worktree removed and a job is "
                                                        "still unresolved: run reconcile again")
        return 5, {"dry_run": dry, "results": rows, "pending": still, "changed": True, "error": error}
    return 0, {"dry_run": dry, "results": rows, "pending": still}


def force_bank_blocker(ctx, job, allow_unknown=False, overridden=None):
    """Why `--force-bank` must not close this unbanked job, else None: only a dispatcher proven gone passes.

    An alive or unknown verdict refuses (R1: unknown never leads to a bank). A job with no dispatcher record
    (`launching`) is refused while a process carries its dispatcher argv or /proc cannot be searched.
    """
    if job["dispatcher"]:
        verdict, why = dispatcher_verdict(ctx, job)
        if verdict == "gone" or (verdict == "unknown" and allow_unknown):  # the operator vouches for an unknown one
            if verdict == "unknown" and overridden is not None:
                overridden.append(why)
            return None
        return f"dispatcher {verdict} ({why})" + ("; --allow-unknown-liveness overrides an unknown verdict" if verdict == "unknown" else "")
    if not have_proc():
        return "no /proc: cannot look for the dispatcher process"
    if find_dispatcher_processes(ctx, job["job_id"]):
        return "dispatcher alive (a process carries this job's dispatcher argv)"
    return None


def force_bank(ctx, job_id, reason, allow_unknown=False):
    if not isinstance(reason, str) or not reason.strip():
        raise L.Refused("--force-bank needs --reason")
    job = ctx.ledger.read()["jobs"].get(job_id)
    overridden = []
    if job is not None and job["state"] != "banked":
        blocker = force_bank_blocker(ctx, job, allow_unknown, overridden)
        if blocker is not None:
            raise L.Refused(L.cap_text(f"job {job_id}: {blocker}; --force-bank only closes a job whose dispatcher is "
                                       "provably gone", 400))
    row = settle_job(ctx, job_id, dry=True)
    refused = bank_refusal(ctx, job_id)  # a result the ledger refuses to store leaves the job open: the operator closes it
    if row["status"] != "unresolved" and refused is None:
        raise L.Refused(f"job {job_id} is not unresolved (status {row['status']}): {row['action']}")
    audit = " (unknown liveness overridden)" if overridden else ""
    if refused is not None:
        obs = jobs.synth("failed", "invalid_result", L.cap_text(f"operator decision: {reason.strip()[:300]}{audit}; "
                                                                f"the job's result was refused ({refused})"))
    else:
        obs = jobs.synth("failed", "abandoned", f"operator decision: {reason.strip()[:300]}{audit}")
    return banked(ctx, job_id, obs, "force-banked; no process was touched")


_ASSESS_WARNED = set()


def bank_refusal(ctx, job_id):
    """The refusal text when banking this job's observed result would be refused, else None. Commits nothing."""
    job = ctx.ledger.read()["jobs"].get(job_id)
    if job is None or job["state"] == "banked" or job["kind"] != "dispatch":
        return None
    obs, _, _ = jobs.observe_result(ctx, job, "file")
    if obs is None:
        return None
    try:
        jobs.bank_in(copy.deepcopy(ctx.ledger.read()), job_id, obs)
    except L.ResultRefused as error:
        return L.cap_text(str(error), 200)
    except Exception as error:  # noqa: BLE001 - only a refusal opens the force-bank path; fail closed, but say why
        if job_id not in _ASSESS_WARNED:  # once per process and job (wait polls every 0.2 s); never stdout
            _ASSESS_WARNED.add(job_id)
            with contextlib.suppress(OSError, ValueError):  # a closed or broken stderr must not turn into a traceback
                if sys.stderr is not None:
                    print(L.cap_text(f"bank_refusal: could not assess job {job_id}: {error.__class__.__name__}", 200),
                          file=sys.stderr)
        return None
    return None
