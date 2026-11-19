"""Continuity ledger: one JSON document under an flock, replaced atomically (stdlib only, Python 3.9+).

The ledger is the coordinator's record, not a security boundary: every same-user process, workers
included, can write it. Nothing here signals a process or waits for one while the lock is held.
"""

import contextlib
import datetime
import json
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # not POSIX: every command refuses, see require_platform()
    fcntl = None

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.contracts import contracts  # noqa: E402
from modules.orchestration import dispatch  # noqa: E402

SCHEMA = "ai-architecture-continuity-ledger"
VERSION = 1
MAX_BYTES = 16 * 1024 * 1024
STATE_NAME = Path(".ai-continuity") / "ledger"
NAME = dispatch.NAME
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
HARNESSES = ("cli", "claude-code", "opencode")
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_CONFIG = {
    "lock_wait_seconds": 10, "max_active_jobs": 8, "launch_grace_seconds": 30,
    "stuck_grace_seconds": 180, "cancel_wait_seconds": 30,
    "handoff_reserve_tokens": 20000, "handoff_cap_tokens": None, "prepare_fraction": 0.8, "context_limits": {},
    "child_budget_tokens": None, "child_budget_pool_tokens": None, "resume_max_bytes": 4096,
}
TIMING_KEYS = ("lock_wait_seconds", "max_active_jobs", "launch_grace_seconds", "stuck_grace_seconds",
               "cancel_wait_seconds")
SOURCES = ("claude-transcript", "opencode-event", "cli")
MAX_SESSIONS = 64
MAX_CHECKPOINTS = 20
MAX_RESUME_BYTES = 16384
UNSUPPORTED = "unsupported platform: the ledger needs fcntl.flock"


class LedgerError(Exception):
    pass


class Refused(LedgerError):
    """A refusal: bad usage or a rule said no. Nothing changed (exit 2)."""


class ResultRefused(Refused):
    """A new result that `check_result` refuses at bank time: the ledger is healthy, only this result cannot be stored."""


class LedgerBusy(LedgerError):
    """The lock was not obtained in time (exit 3)."""


class LedgerInvalid(LedgerError):
    """The stored ledger does not validate (exit 4)."""


class LedgerStorageError(LedgerError):
    """A write failed before it replaced the ledger: the previous revision is intact (exit 4)."""


class LedgerDurabilityUnknown(LedgerError):
    """The new revision is visible but the directory fsync failed (exit 7). Never retry the mutation."""


def require_platform():
    if fcntl is None:
        raise Refused(UNSUPPORTED)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def cap_text(value, limit=500):
    """`value` as text of at most about `limit` characters; a truncated value carries the sha256 of the whole."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    marker = f"... [truncated {len(text)} chars, sha256 {sha256_hex(text.encode('utf-8', 'replace'))}]"
    return text[:max(0, limit - len(marker))] + marker


def parse_session(value):
    """`HARNESS:ID` -> owner object."""
    if not isinstance(value, str) or ":" not in value:
        raise Refused("--session must look like HARNESS:ID")
    harness, session_id = value.split(":", 1)
    if harness not in HARNESSES or not SESSION_ID.fullmatch(session_id):
        raise Refused(f"--session: harness must be one of {list(HARNESSES)} and the id match [A-Za-z0-9_-]{{1,128}}")
    return {"harness": harness, "session_id": session_id}


def session_key(owner):
    return f"{owner['harness']}:{owner['session_id']}" if owner and owner.get("session_id") else None


# --- strict JSON ------------------------------------------------------------

def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def strict_loads(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def read_file(path, limit=MAX_BYTES):
    """Bytes of a regular file opened without following symlinks, at most `limit`; OSError otherwise.

    O_NONBLOCK: a worker-planted FIFO opens at once (and is then refused as not regular) instead of blocking the caller.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > limit:
            raise OSError("file too large")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise OSError("file too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# --- validation ----------------------------------------------------------------

def bad(message):
    raise LedgerInvalid(message)


def keys(obj, expected, where):
    if not isinstance(obj, dict):
        bad(f"{where}: must be an object")
    if set(obj) != set(expected):
        extra, missing = sorted(set(obj) - set(expected)), sorted(set(expected) - set(obj))
        bad(f"{where}: unknown keys {extra}" if extra else f"{where}: missing keys {missing}")


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def need_int(value, where, minimum=0, nullable=False):
    if value is None and nullable:
        return
    if not is_int(value) or value < minimum:
        bad(f"{where}: must be an integer >= {minimum}")


def need_str(value, where, nullable=False, pattern=None):
    if value is None and nullable:
        return
    if not isinstance(value, str) or (pattern is not None and not pattern.fullmatch(value)):
        bad(f"{where}: invalid string")


def need_iso(value, where, nullable=False):
    need_str(value, where, nullable, ISO)


def check_owner(owner, where):
    keys(owner, ("harness", "session_id"), where)
    if owner["harness"] not in HARNESSES:
        bad(f"{where}.harness: unknown")
    need_str(owner["session_id"], f"{where}.session_id", True, SESSION_ID)


MAX_TEXT = 1024   # a reason, note, branch, kind or hash: worker text is capped before it gets here
MAX_PATH = 4096
MAX_RECORD_BYTES = 8 * 1024  # a dispatcher result record is a few hundred bytes; more is not one. Measured as stored.


def stored_size(obj):
    """Bytes of `obj` as the ledger stores it (ASCII-escaped, indented): a lone surrogate or U+0080 cannot make it lie."""
    return len(json.dumps(obj, sort_keys=True, allow_nan=False, indent=1))


def need_len(value, where, limit=MAX_TEXT):
    if isinstance(value, str) and len(value) > limit:
        bad(f"{where}: longer than {limit} characters")


RESULT_KEYS = ("outcome", "cause", "source", "result_sha256", "record", "exit_code", "usage", "worktree",
               "evidence", "note", "trust", "banked_at", "bank_revision")
RESULT_OPTIONAL = ("usage_total", "budget_state")  # added by U04b: absent in older records, read as null


def check_result(result, where):
    keys(result, RESULT_KEYS + (tuple(k for k in RESULT_OPTIONAL if k in result) if isinstance(result, dict) else ()),
         where)
    if "usage_total" in result:
        need_int(result["usage_total"], f"{where}.usage_total", 0, True)
    if "budget_state" in result and result["budget_state"] not in ("within", "exceeded", "unknown", "unbounded"):
        bad(f"{where}.budget_state: unknown")
    if result["outcome"] not in ("succeeded", "failed", "unsupported"):
        bad(f"{where}.outcome: unknown")
    if result["cause"] not in ("completed", "refused", "cancelled", "timed_out", "orphaned", "lost", "not_launched",
                               "lost_in_launch", "invalid_result", "result_missing", "abandoned",
                               "orphaned_external"):
        bad(f"{where}.cause: unknown")
    if result["source"] not in ("wait", "file", "stdout", "reconcile", "external"):
        bad(f"{where}.source: unknown")
    need_str(result["result_sha256"], f"{where}.result_sha256", True, SHA)
    if result["record"] is not None:
        try:
            contracts.check_record("result", result["record"], dispatch.result_schema())
        except Exception:  # noqa: BLE001 - the contracts validator raises its own error type
            bad(f"{where}.record: not a valid result record")
        if stored_size(result["record"]) > MAX_RECORD_BYTES:
            bad(f"{where}.record: larger than {MAX_RECORD_BYTES} bytes as stored")
    need_int(result["exit_code"], f"{where}.exit_code", -1000, True)
    if result["usage"] is not None:
        if not isinstance(result["usage"], dict) or set(result["usage"]) - set(dispatch.USAGE_KEYS):
            bad(f"{where}.usage: invalid")
        if not all(dispatch.usable(v) for v in result["usage"].values()):
            bad(f"{where}.usage: invalid number")
    if result["worktree"] is not None:
        keys(result["worktree"], ("path", "branch", "kept", "reason"), f"{where}.worktree")
        if not (isinstance(result["worktree"]["kept"], bool) and isinstance(result["worktree"]["reason"], str)):
            bad(f"{where}.worktree: invalid")
        need_len(result["worktree"]["path"], f"{where}.worktree.path", MAX_PATH)
        need_len(result["worktree"]["branch"], f"{where}.worktree.branch")
        need_len(result["worktree"]["reason"], f"{where}.worktree.reason")
    check_evidence(result["evidence"], f"{where}.evidence")
    need_str(result["note"], f"{where}.note", True)
    need_len(result["note"], f"{where}.note")
    if result["trust"] not in ("dispatcher", "worker-attested", "recovery"):
        bad(f"{where}.trust: unknown")
    need_iso(result["banked_at"], f"{where}.banked_at")
    need_int(result["bank_revision"], f"{where}.bank_revision")


def check_evidence(evidence, where):
    if not isinstance(evidence, list):
        bad(f"{where}: must be a list")
    for index, item in enumerate(evidence):
        keys(item, ("kind", "reference", "sha256"), f"{where}[{index}]")
        if not all(isinstance(item[k], str) and item[k] for k in item):
            bad(f"{where}[{index}]: invalid")
        need_len(item["kind"], f"{where}[{index}].kind")
        need_len(item["reference"], f"{where}[{index}].reference", MAX_PATH)
        need_len(item["sha256"], f"{where}[{index}].sha256")


JOB_KEYS = ("job_id", "kind", "role", "access", "owner", "state", "created_at", "updated_at", "updated_revision",
            "submitter", "dispatcher", "workdir", "worktree_plan", "prompt_sha256", "budget", "cancel", "result",
            "post_bank", "acknowledged_revision")
# Keys a later sub-item added without a version bump: optional on read (absent = the default), written by every
# transaction after the upgrade (`upgrade`). Unknown keys are still refused.
OPTIONAL_JOB_KEYS = {"context": None}


def check_context(context, where):
    if context is None:
        return
    keys(context, ("packet_sha256", "files", "estimated_tokens", "budget_tokens"), where)
    need_str(context["packet_sha256"], f"{where}.packet_sha256", pattern=SHA)
    need_int(context["estimated_tokens"], f"{where}.estimated_tokens")
    need_int(context["budget_tokens"], f"{where}.budget_tokens")
    if not isinstance(context["files"], list):
        bad(f"{where}.files: must be a list")
    for index, item in enumerate(context["files"]):
        keys(item, ("path", "sha256", "bytes"), f"{where}.files[{index}]")
        need_str(item["path"], f"{where}.files[{index}].path")
        need_len(item["path"], f"{where}.files[{index}].path", MAX_PATH)
        need_str(item["sha256"], f"{where}.files[{index}].sha256", pattern=SHA)
        need_int(item["bytes"], f"{where}.files[{index}].bytes")


def upgrade(doc):
    """Fill the documented default of every optional job key an older writer left out (after `validate`)."""
    for job in doc["jobs"].values():
        for name, default in OPTIONAL_JOB_KEYS.items():
            job.setdefault(name, default)


def check_job(job_id, job):
    where = f"jobs.{job_id}"
    keys(job, JOB_KEYS + tuple(k for k in OPTIONAL_JOB_KEYS if isinstance(job, dict) and k in job), where)
    if job["job_id"] != job_id or not NAME.fullmatch(job_id):
        bad(f"{where}: job_id mismatch")
    if job["kind"] not in ("dispatch", "external"):
        bad(f"{where}.kind: unknown")
    need_str(job["role"], f"{where}.role", pattern=NAME)
    if job["access"] not in ("read", "write"):
        bad(f"{where}.access: unknown")
    check_owner(job["owner"], f"{where}.owner")
    if job["state"] not in ("launching", "launched", "banked"):
        bad(f"{where}.state: unknown")
    need_iso(job["created_at"], f"{where}.created_at")
    need_iso(job["updated_at"], f"{where}.updated_at")
    need_int(job["updated_revision"], f"{where}.updated_revision")
    keys(job["submitter"], ("pid", "start_ticks"), f"{where}.submitter")
    need_int(job["submitter"]["pid"], f"{where}.submitter.pid", 1)
    need_int(job["submitter"]["start_ticks"], f"{where}.submitter.start_ticks", 0, True)
    if job["dispatcher"] is not None:
        disp = job["dispatcher"]
        keys(disp, ("pid", "start_ticks", "launched_monotonic", "boot_id"), f"{where}.dispatcher")
        need_int(disp["pid"], f"{where}.dispatcher.pid", 1)
        need_int(disp["start_ticks"], f"{where}.dispatcher.start_ticks", 0, True)
        if not dispatch.usable(disp["launched_monotonic"]):
            bad(f"{where}.dispatcher.launched_monotonic: invalid")
        need_str(disp["boot_id"], f"{where}.dispatcher.boot_id", True)
    need_str(job["workdir"], f"{where}.workdir", True)
    if job["worktree_plan"] is not None:
        keys(job["worktree_plan"], ("path", "branch"), f"{where}.worktree_plan")
        if not all(isinstance(v, str) and v for v in job["worktree_plan"].values()):
            bad(f"{where}.worktree_plan: invalid")
    need_str(job["prompt_sha256"], f"{where}.prompt_sha256", True, SHA)
    keys(job["budget"], ("tokens",), f"{where}.budget")
    need_int(job["budget"]["tokens"], f"{where}.budget.tokens", 0, True)
    if job["cancel"] is not None:
        keys(job["cancel"], ("requested_at", "by"), f"{where}.cancel")
        need_iso(job["cancel"]["requested_at"], f"{where}.cancel.requested_at")
        check_owner(job["cancel"]["by"], f"{where}.cancel.by")
    if not isinstance(job["post_bank"], list) or len(job["post_bank"]) > 8:
        bad(f"{where}.post_bank: must be a list of at most 8")
    for index, entry in enumerate(job["post_bank"]):
        keys(entry, ("observed_at", "outcome", "sha256", "source"), f"{where}.post_bank[{index}]")
        need_iso(entry["observed_at"], f"{where}.post_bank[{index}].observed_at")
        need_str(entry["outcome"], f"{where}.post_bank[{index}].outcome")
        need_str(entry["sha256"], f"{where}.post_bank[{index}].sha256", True, SHA)
        need_str(entry["source"], f"{where}.post_bank[{index}].source")
    need_int(job["acknowledged_revision"], f"{where}.acknowledged_revision", 0, True)
    if (job["state"] == "banked") != (job["result"] is not None):
        bad(f"{where}: result must be set exactly when the state is banked")
    if job["state"] == "launched" and job["dispatcher"] is None:
        bad(f"{where}: a launched job needs a dispatcher record")
    if job["result"] is not None:
        check_result(job["result"], f"{where}.result")
    if job["kind"] == "external" and job["access"] != "read":
        bad(f"{where}: an external job must be read access")
    check_context(job.get("context"), f"{where}.context")


def validate(doc):
    keys(doc, ("schema", "version", "project_root", "revision", "updated_at", "drain", "jobs", "checkpoints",
               "sessions"), "ledger")
    if doc["schema"] != SCHEMA or doc["version"] != VERSION:
        bad("ledger: unknown schema or version")
    need_str(doc["project_root"], "project_root")
    need_int(doc["revision"], "revision")
    need_iso(doc["updated_at"], "updated_at")
    if doc["drain"] is not None:
        keys(doc["drain"], ("since", "reason"), "drain")
        need_iso(doc["drain"]["since"], "drain.since")
        need_str(doc["drain"]["reason"], "drain.reason")
    if not isinstance(doc["jobs"], dict):
        bad("jobs: must be an object")
    for job_id, job in doc["jobs"].items():
        check_job(job_id, job)
    check_sessions(doc["sessions"])
    check_checkpoints(doc["checkpoints"])


def check_sessions(sessions):
    if not isinstance(sessions, dict) or len(sessions) > MAX_SESSIONS:
        bad(f"sessions: must be an object of at most {MAX_SESSIONS}")
    for key, rec in sessions.items():
        where = f"sessions.{key}"
        keys(rec, ("harness", "session_id", "usage_tokens", "limit_tokens", "model", "observed_at", "source"), where)
        if rec["harness"] not in HARNESSES:
            bad(f"{where}.harness: unknown")
        need_str(rec["session_id"], f"{where}.session_id", False, SESSION_ID)
        if key != f"{rec['harness']}:{rec['session_id']}":
            bad(f"{where}: key does not match the record")
        need_int(rec["usage_tokens"], f"{where}.usage_tokens", 0, True)
        need_int(rec["limit_tokens"], f"{where}.limit_tokens", 1, True)
        need_str(rec["model"], f"{where}.model", True)
        need_len(rec["model"], f"{where}.model")
        need_iso(rec["observed_at"], f"{where}.observed_at")
        if rec["source"] not in SOURCES:
            bad(f"{where}.source: unknown")


def check_ids(ids, where):
    if not isinstance(ids, list) or not all(isinstance(i, str) and NAME.fullmatch(i) for i in ids):
        bad(f"{where}: must be a list of job ids")


def check_checkpoints(checkpoints):
    if not isinstance(checkpoints, list) or len(checkpoints) > MAX_CHECKPOINTS:
        bad(f"checkpoints: must be a list of at most {MAX_CHECKPOINTS}")
    last = 0
    for index, cp in enumerate(checkpoints):
        where = f"checkpoints[{index}]"
        keys(cp, ("revision", "created_at", "session", "state", "next", "evidence", "tracker", "covered_jobs",
                  "accepted_failed", "pending_jobs", "jobs_digest", "ready_at_checkpoint", "resume",
                  "receipt_sha256"), where)
        need_int(cp["revision"], f"{where}.revision", last + 1)
        last = cp["revision"]
        need_iso(cp["created_at"], f"{where}.created_at")
        if cp["session"] is not None:
            parse_session_key(cp["session"], f"{where}.session")
        for name, top in (("state", 2000), ("next", 1000)):
            if not isinstance(cp[name], str) or not 1 <= len(cp[name]) <= top:
                bad(f"{where}.{name}: must be 1..{top} characters")
        evidence = cp["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 32:
            bad(f"{where}.evidence: must hold 1..32 items")
        for n, item in enumerate(evidence):
            keys(item, ("ref", "sha256", "bytes"), f"{where}.evidence[{n}]")
            if not isinstance(item["ref"], str) or not 1 <= len(item["ref"]) <= MAX_PATH:
                bad(f"{where}.evidence[{n}].ref: invalid")
            need_str(item["sha256"], f"{where}.evidence[{n}].sha256", True, SHA)
            need_int(item["bytes"], f"{where}.evidence[{n}].bytes", 0, True)
            if (item["sha256"] is None) != (item["bytes"] is None):
                bad(f"{where}.evidence[{n}]: sha256 and bytes go together")
        if cp["tracker"] is not None:
            keys(cp["tracker"], ("path", "sha256"), f"{where}.tracker")
            need_len(cp["tracker"]["path"], f"{where}.tracker.path", MAX_PATH)
            need_str(cp["tracker"]["path"], f"{where}.tracker.path")
            need_str(cp["tracker"]["sha256"], f"{where}.tracker.sha256", False, SHA)
        for name in ("covered_jobs", "accepted_failed", "pending_jobs"):
            check_ids(cp[name], f"{where}.{name}")
        need_str(cp["jobs_digest"], f"{where}.jobs_digest", False, SHA)
        ready = cp["ready_at_checkpoint"]
        keys(ready, ("ready", "reasons"), f"{where}.ready_at_checkpoint")
        if not isinstance(ready["ready"], bool) or not isinstance(ready["reasons"], list) \
                or not all(isinstance(r, str) and len(r) <= MAX_TEXT for r in ready["reasons"]):
            bad(f"{where}.ready_at_checkpoint: invalid")
        keys(cp["resume"], ("consumed_by", "consumed_at"), f"{where}.resume")
        if (cp["resume"]["consumed_by"] is None) != (cp["resume"]["consumed_at"] is None):
            bad(f"{where}.resume: consumed_by and consumed_at go together")
        if cp["resume"]["consumed_by"] is not None:
            parse_session_key(cp["resume"]["consumed_by"], f"{where}.resume.consumed_by")
            need_iso(cp["resume"]["consumed_at"], f"{where}.resume.consumed_at")
        need_str(cp["receipt_sha256"], f"{where}.receipt_sha256", False, SHA)


def parse_session_key(value, where):
    try:
        parse_session(value)
    except Refused:
        bad(f"{where}: must look like HARNESS:ID")


# --- config -----------------------------------------------------------------------

def load_config(state):
    try:
        raw = strict_loads(read_file(Path(state) / "config.json", 65536))
    except (OSError, ValueError) as error:
        raise Refused(f"config.json unreadable: {error}")
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_CONFIG):
        raise Refused(f"config.json: unknown keys {sorted(set(raw) - set(DEFAULT_CONFIG)) if isinstance(raw, dict) else '?'}")
    config = {**DEFAULT_CONFIG, **raw}
    for name in TIMING_KEYS:
        if not dispatch.is_number(config[name]) or config[name] <= 0:
            raise Refused(f"config.json: {name} must be a positive number")
    config["max_active_jobs"] = int(config["max_active_jobs"])
    if not is_int(config["handoff_reserve_tokens"]) or config["handoff_reserve_tokens"] < 0:
        raise Refused("config.json: handoff_reserve_tokens must be an integer >= 0")
    for name in ("handoff_cap_tokens", "child_budget_tokens", "child_budget_pool_tokens"):
        if config[name] is not None and (not is_int(config[name]) or config[name] < 1):
            raise Refused(f"config.json: {name} must be null or an integer >= 1")
    if not dispatch.is_number(config["prepare_fraction"]) or not 0 < config["prepare_fraction"] <= 1:
        raise Refused("config.json: prepare_fraction must be a number above 0 and at most 1")
    limits = config["context_limits"]
    if not isinstance(limits, dict) or not all(isinstance(k, str) and is_int(v) and v >= 1 for k, v in limits.items()):
        raise Refused("config.json: context_limits must map model names to integers >= 1")
    if not is_int(config["resume_max_bytes"]) or not 512 <= config["resume_max_bytes"] <= MAX_RESUME_BYTES:
        raise Refused(f"config.json: resume_max_bytes must be an integer from 512 to {MAX_RESUME_BYTES}")
    return config


# --- storage ------------------------------------------------------------------------

def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Ledger:
    """A ledger directory: `transaction()` for writes, `read()` for a shared-lock snapshot. Not reentrant."""

    def __init__(self, state, lock_wait=None):
        require_platform()
        self.state = Path(state)
        self.path = self.state / "ledger.json"
        self.tmp = self.state / "ledger.json.tmp"
        self.lock_path = self.state / "ledger.lock"
        self.lock_wait = lock_wait if lock_wait is not None else load_config(self.state)["lock_wait_seconds"]
        self._depth = 0

    # locking
    def _acquire(self, mode):
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise LedgerStorageError(f"lock file unusable: {error.__class__.__name__}")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise LedgerStorageError("lock file is not a regular file owned by the effective user")
            end = time.monotonic() + self.lock_wait
            while True:
                try:
                    fcntl.flock(fd, mode | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise LedgerBusy(f"ledger busy: lock not obtained within {self.lock_wait:g}s")
                    time.sleep(0.01)
        except BaseException:
            os.close(fd)
            raise

    @contextlib.contextmanager
    def _locked(self, mode):
        if self._depth:
            raise RuntimeError("ledger transaction()/read() is not reentrant")
        self._depth += 1
        try:
            fd = self._acquire(mode)
            try:
                yield
            finally:
                os.close(fd)  # closing the descriptor drops the flock
        finally:
            self._depth -= 1

    # reading
    def _load(self):
        try:
            data = read_file(self.path)
        except FileNotFoundError:
            raise LedgerInvalid("ledger.json is missing (run init)")
        except OSError as error:
            raise LedgerInvalid(f"ledger.json unreadable: {error}")
        try:
            doc = strict_loads(data)
        except (ValueError, RecursionError) as error:
            raise LedgerInvalid(f"ledger.json is not strict JSON: {error}")
        validate(doc)
        upgrade(doc)
        return doc

    def read(self):
        with self._locked(fcntl.LOCK_SH):
            return self._load()

    # writing
    @contextlib.contextmanager
    def transaction(self):
        with self._locked(fcntl.LOCK_EX):
            doc = self._load()
            yield doc  # an exception here propagates and nothing is written
            validate(doc)
            doc["revision"] += 1
            doc["updated_at"] = now()
            self._write(doc)

    def create(self, doc):
        """First write (init): under the lock, only when no ledger exists."""
        with self._locked(fcntl.LOCK_EX):
            if self.path.exists() or self.path.is_symlink():
                raise Refused("ledger already exists")
            validate(doc)
            self._write(doc)

    def _write(self, doc):
        try:
            data = json.dumps(doc, sort_keys=True, allow_nan=False, indent=1).encode("utf-8")
        except (ValueError, RecursionError) as error:
            raise LedgerStorageError(f"ledger write failed: {error.__class__.__name__}: {error}")
        if len(data) > MAX_BYTES:  # a write that `_load` would refuse next time must never replace a readable ledger
            raise LedgerStorageError(f"ledger write refused: {len(data)} bytes exceed the {MAX_BYTES} byte ceiling")
        try:
            try:
                os.unlink(self.tmp)
            except FileNotFoundError:
                pass
            fd = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(self.tmp, self.path)
        except (OSError, ValueError) as error:
            with contextlib.suppress(OSError):
                if self.tmp.is_file():
                    os.unlink(self.tmp)
            raise LedgerStorageError(f"ledger write failed: {error.__class__.__name__}: {error}")
        try:
            fsync_dir(self.state)
        except OSError as error:
            raise LedgerDurabilityUnknown(f"directory fsync failed after replace: {error.__class__.__name__}")


def new_doc(project_root):
    return {"schema": SCHEMA, "version": VERSION, "project_root": project_root, "revision": 0, "updated_at": now(),
            "drain": None, "jobs": {}, "checkpoints": [], "sessions": {}}


# --- state directory ------------------------------------------------------------------

def no_symlinks(path):
    """lstat each component of `path`; none may be a symlink (the caller chose where the walk starts)."""
    for current in reversed((path, *path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise Refused(f"redirected path component: {current}")


def check_dir(path, what):
    try:
        info = path.lstat()
    except OSError as error:
        raise Refused(f"{what} unusable: {error.__class__.__name__}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused(f"{what} must be a real directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refused(f"{what} must be owned by the effective user and not group- or other-writable")


def project_state(project):
    root = Path(os.path.abspath(project))
    try:
        info = root.lstat()
    except OSError as error:
        raise Refused(f"project unusable: {error.__class__.__name__}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused("project must be a real directory")
    return root, root / STATE_NAME


def make_private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    check_dir(path, f"state directory {path.name}")


def init_project(project):
    require_platform()
    root, state = project_state(project)
    real_root = os.path.realpath(root)
    make_private_dir(root / ".ai-continuity")
    make_private_dir(state)
    no_symlinks(state)  # from the project root down (the root itself is checked above)
    for sub in ("runs", "spool"):
        make_private_dir(state / sub)
    config = state / "config.json"
    if not config.exists():
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(DEFAULT_CONFIG, handle, indent=1, sort_keys=True)
            handle.write("\n")
    ledger = Ledger(state)
    if ledger.path.exists():
        existing = ledger.read()
        if existing["project_root"] != real_root:
            raise Refused(f"state directory belongs to another project root: {existing['project_root']}")
        created = False
    else:
        ledger.create(new_doc(real_root))
        created = True
    return state, real_root, created


def open_state(project=None, state=None):
    """The checked state directory for --project or --state (exactly one)."""
    require_platform()
    if (project is None) == (state is None):
        raise Refused("give exactly one of --project or --state")
    if project is not None:
        root, path = project_state(project)
        for part in (root / ".ai-continuity", path):
            check_dir(part, f"state directory {part.name}")
        no_symlinks(path)
    else:
        path = Path(os.path.abspath(state))
        check_dir(path, "state directory")
    if not (path / "ledger.json").exists():
        raise LedgerInvalid("ledger.json is missing (run init)")
    for sub in ("runs", "spool"):
        make_private_dir(path / sub)
    return Path(os.path.realpath(path))


def new_job_id():
    return "j" + uuid.uuid4().hex[:12]
