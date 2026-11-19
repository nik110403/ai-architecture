#!/usr/bin/env python3
"""First native request -> check -> restart -> resume journey (offline and fake harnesses; live needs consent).

Read README.md first. `--offline` needs no harness at all. `--live` is refused unless `--i-accept-spend` is given
and a real, unmarked harness binary is on PATH; it is the separate step that closes the journey on a real harness.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTINUITY = ROOT / "modules" / "continuity" / "continuity.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.orchestration import dispatch  # noqa: E402

MARKER = "AI_ARCH_FAKE_HARNESS"
EXECUTABLE = {"claude-code": "claude", "opencode": "opencode"}
SCHEMA = "native-system-receipt/1"
WORKER_ROLE = "researcher"  # agents/researcher.md: answer the stated question, no artifact-and-lens precondition
WORKER_PROMPT = "Read input.txt in the working directory and reply with only the sum of the numbers in it."
CHECKER_PROMPT = ("Try to disprove this claim: the sum of the numbers {numbers} is {claim}.\n"
                  "Numbers: {numbers}\nClaimed sum: {claim}\n")
NEXT_RE = re.compile(r"native-system (\S+): (.*)", re.S)
CHECK_RE = re.compile(r"check worker job (\S+) and publish answer")
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


# --- files ---------------------------------------------------------------------------------------------------

def write_atomic(path, text):
    """Write beside the target through an exclusively created (O_EXCL, never followed) random temp file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


MAX_JSON = 64 * 1024


def regular(path):
    """True for a regular file that is not a symlink: never a FIFO, a device or a link to one."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def read_json_bytes(path):
    """At most MAX_JSON bytes of a regular file, opened without following a link or blocking on a FIFO and
    checked on the open descriptor (no check-then-open race). ValueError for anything else or an oversize file."""
    name = Path(path).name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError(f"{name} is not a regular file of at most {MAX_JSON} bytes")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{name} is not a regular file of at most {MAX_JSON} bytes")
        data = b""
        while len(data) <= MAX_JSON:
            chunk = os.read(fd, MAX_JSON + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_JSON:
        raise ValueError(f"{name} is larger than {MAX_JSON} bytes")
    return data


def load(path):
    """Parse a small JSON file; a non-regular file or one over MAX_JSON bytes is refused."""
    try:
        data = read_json_bytes(path)
    except ValueError as error:
        raise SystemExit(refuse(str(error)))
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise SystemExit(refuse(f"{Path(path).name} is not valid JSON"))


def save(path, data):
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def no_link_below(out, sub):
    """Refuse if `runs` or `runs/<id>` under `out` is a symlink (O_NOFOLLOW only covers the last path component)."""
    for part in dict.fromkeys((Path("runs"), Path(sub))) if Path(sub).parts else ():
        if (Path(out) / part).is_symlink():
            raise SystemExit(refuse(f"{part} under --out is a symlink"))


def private_dir(path, exist_ok=True):
    Path(path).mkdir(mode=0o700, exist_ok=exist_ok)


# --- the one continuity call site ------------------------------------------------------------------------------

def child_env(plan):
    env = dict(os.environ)
    if plan.get("child_path"):
        env["PATH"] = plan["child_path"]
    return env


def cont(rd, plan, who, *args):
    """Run `continuity.py <command> --project <rd>/project ...`; log it to calls.jsonl; return (exit, json)."""
    argv = [args[0], "--project", str(Path(rd) / "project"), *args[1:]]
    proc = subprocess.run([sys.executable, "-B", str(CONTINUITY), *argv], capture_output=True, text=True,
                          env=child_env(plan), timeout=900, stdin=subprocess.DEVNULL)
    with open(Path(rd) / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps({"who": who, "argv": argv, "code": proc.returncode}) + "\n")
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"error": (proc.stdout + proc.stderr)[-400:]}


def spend_args(plan):
    return ["--allow-spend"] if plan["spend"] else []


# --- dispatcher envelopes --------------------------------------------------------------------------------------

def envelope(rd, job_id):
    """(dispatch block, stdout bytes) of a job, read from the dispatcher envelope in the ledger spool."""
    spool = Path(rd) / "project" / ".ai-continuity" / "ledger" / "spool" / f"{job_id}.out"
    block = json.loads(spool.read_text(encoding="utf-8"))["dispatch"]
    return block, Path(block["stdout"]["path"]).read_bytes()


def answer_text(harness, stdout):
    """The model's answer: the raw output, the json `result` (Claude Code) or the `text` parts (OpenCode)."""
    text = stdout.decode("utf-8", "replace")
    if harness == "claude-code":
        return str(json.loads(text).get("result", "")).strip()
    if harness == "opencode":
        parts = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "text":
                parts.append(str((event.get("part") or {}).get("text", "")))
        return "".join(parts).strip()
    return text.strip()


def usage_total(block):
    usage = block.get("usage")
    known = [usage[k] for k in TOKEN_KEYS if isinstance(usage, dict) and isinstance(usage.get(k), (int, float))]
    return int(sum(known)) if known else None


VERDICT_MAP = {"STANDS": "CONFIRMED", "REFUTED": "REJECTED", "CONFIRMED": "CONFIRMED", "REJECTED": "REJECTED"}


def normalize_verdict(reply):
    """Map the checker's reply (the refuter `VERDICT:` line, or a bare CONFIRMED/REJECTED) to CONFIRMED/REJECTED.

    Parses only the reply text, first VERDICT line wins; UNVERIFIABLE, unknown or missing -> None (fail closed)."""
    text = (reply or "").strip()
    if text in ("CONFIRMED", "REJECTED"):
        return text
    # The FIRST line that looks like a verdict (any non-word lead-in, then "verdict") decides; a malformed one
    # gives None rather than letting a later clean line win. That line may carry a list marker and markdown
    # emphasis/backticks, nothing after the verdict word.
    first = next((line for line in text.splitlines() if re.match(r"^\W*verdict\b", line, re.I)), "")
    match = re.fullmatch(r"\s*(?:[-*+]\s+)?[*_`]*VERDICT[*_`]*:[*_` \t]*([A-Za-z]+)[*_`]*\s*", first, re.I)
    word = match.group(1).upper() if match else ""
    return VERDICT_MAP[word] if word in ("STANDS", "REFUTED") else None


def job_answer(rd, job_id):
    block, stdout = envelope(rd, job_id)
    return answer_text(block["harness"], stdout)


# --- preflight -------------------------------------------------------------------------------------------------

def refuse(message):
    print(f"refused: {message}", file=sys.stderr)
    return 2


def has_marker(path):
    try:
        return MARKER.encode() in Path(path).read_bytes()[:1 << 20]
    except OSError:
        return False


def pin_of(exe):
    real = os.path.realpath(exe)
    return {"path": real, "sha256": sha256(Path(real).read_bytes())}


def resolve_fake(harness, directory, path):
    """(exe, None) or (None, message): the harness binary found on `path` must live inside the marked fake dir."""
    old = os.environ.get("PATH")
    os.environ["PATH"] = path
    try:
        exe = dispatch.find_exe(EXECUTABLE[harness])
    finally:
        os.environ["PATH"] = old if old is not None else ""
    if exe is None or os.path.dirname(os.path.realpath(exe)) != directory:
        return None, f"{EXECUTABLE[harness]} does not resolve inside --fake-bin"
    if not has_marker(exe):
        return None, f"the fake binary lacks the {MARKER} marker"
    return exe, None


def recheck_spend(plan, args):
    """Exit code 2 (refused) or None: --resume re-proves the spend guard before any continuity call."""
    if not plan.get("spend"):
        return None
    pinned = plan.get("exe") or {}
    if plan["mode"] == "fake":
        if not plan.get("fake_bin"):
            return refuse("the fake plan names no fake_bin")
        directory = os.path.realpath(plan["fake_bin"])
        exe, message = resolve_fake(plan["harness"], directory, plan.get("child_path") or "")
        if exe is None:
            return refuse(message)
    elif plan["mode"] == "live":
        if not args.i_accept_spend:
            return refuse("resuming a live run spends money: give --i-accept-spend")
        if not args.model:
            return refuse("resuming a live run needs --model")
        if args.model != plan.get("model"):
            return refuse("--model differs from the model of the first run")
        exe = dispatch.find_exe(EXECUTABLE[plan["harness"]])
        if exe is None:
            return refuse(f"{EXECUTABLE[plan['harness']]} is not on PATH")
        if has_marker(exe):
            return refuse(f"{EXECUTABLE[plan['harness']]} on PATH is a fake harness")
    else:
        return refuse("the plan allows spend in an unknown mode")
    try:
        now = pin_of(exe)
    except OSError:
        return refuse("the harness binary is unreadable")
    if not pinned or now != pinned:
        return refuse("the harness binary differs from the one pinned at the first run")
    return None


def preflight(args):
    """(plan fragment, None) or (None, exit code). Reads files only: no harness binary is ever executed here."""
    if args.offline:
        return {"mode": "offline", "harness": "local-command", "fake_bin": None, "child_path": None,
                "spend": False, "model": None}, None
    harness = args.fake_harness or args.harness
    if args.fake_harness:
        if not args.fake_bin:
            return None, refuse("--fake-harness needs --fake-bin DIR")
        directory = os.path.realpath(args.fake_bin)
        path = f"{directory}:/usr/bin:/bin"
        exe, message = resolve_fake(harness, directory, path)
        if exe is None:
            return None, refuse(message)
        return {"mode": "fake", "harness": harness, "fake_bin": directory, "child_path": path, "spend": True,
                "model": "MODEL_NAME", "exe": pin_of(exe)}, None
    if not args.i_accept_spend:
        return None, refuse("--live spends money on a real harness: give --i-accept-spend")
    if not args.model:
        return None, refuse("--live needs --model")
    exe = dispatch.find_exe(EXECUTABLE[harness])
    if exe is None:
        return None, refuse(f"{EXECUTABLE[harness]} is not on PATH")
    if has_marker(exe):
        return None, refuse(f"{EXECUTABLE[harness]} on PATH is a fake harness")
    return {"mode": "live", "harness": harness, "fake_bin": None, "child_path": None, "spend": True,
            "model": args.model, "exe": pin_of(exe)}, None


# --- setup -----------------------------------------------------------------------------------------------------

def default_out():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "ai-architecture" / "native-system"


def earlier_run(out):
    """A plan.json, or a latest.json of the shape this script writes ({"run": "." or "runs/<id>"})."""
    if regular(out / "plan.json"):
        return True
    try:
        doc = json.loads(read_json_bytes(out / "latest.json").decode("utf-8"))
    except (ValueError, RecursionError, OSError):
        return False
    return isinstance(doc, dict) and run_subdir(doc.get("run")) is not None


def prepare_out(out):
    """The output directory, 0700, never a symlink or someone else's; its realpath."""
    out = Path(out).expanduser()
    if out.is_symlink() or os.path.abspath(out) != os.path.realpath(out):
        raise SystemExit(refuse("--out is, or lies below, a symlink"))
    if out.exists():
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SystemExit(refuse("--out is not a directory owned by you"))
        if any(out.iterdir()) and not earlier_run(out):
            raise SystemExit(refuse("--out is not empty and holds no earlier run (no latest.json or plan.json)"))
        if info.st_mode & 0o077:
            out.chmod(0o700)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(mode=0o700)
    return Path(os.path.realpath(out))


def write_config(rd, plan, args):
    gate = plan["gate"]
    if plan["mode"] == "offline":
        worker = [sys.executable, str(HERE / "worker.py"), "--gate", gate]  # offline always has a gate
        if args.fail_always:
            worker.append("--fail")
        if args.wrong_worker:
            worker += ["--offset", "1"]
        checker = [sys.executable, str(HERE / "checker.py")]
        if args.fail_first:
            checker += ["--once-marker", str(Path(rd) / "checker-failed-once")]
        seats = {"worker": {"harness": "local-command", "command": worker},
                 "checker": {"harness": "local-command", "command": checker}}
    else:
        seat = {"harness": plan["harness"], "model": plan["model"]}
        seats = {"worker": dict(seat), "checker": dict(seat)}
    config = {"seats": seats, "roles": {WORKER_ROLE: ["worker"], "refuter": ["checker"]},
              "permit": [WORKER_ROLE, "refuter"], "timeout_seconds": 300}
    if plan["spend"]:
        config["allow_spend"] = True
    save(Path(rd) / "dispatch.json", config)


def new_run(args, fragment):
    out = prepare_out(args.out or default_out())
    run_id = f"ns-{uuid.uuid4().hex[:8]}"
    if (out / "project").is_symlink():
        raise SystemExit(refuse("project under --out is a symlink"))
    fresh = not (os.path.lexists(out / "receipt.json") or os.path.lexists(out / "project"))
    rd = out if fresh else out / "runs" / run_id
    try:
        if not fresh:
            no_link_below(out, "runs")
            private_dir(out / "runs")
        private_dir(rd, exist_ok=fresh)  # under runs/ the fresh id must not already exist (a planted entry fails)
        private_dir(rd / "project")
    except OSError as err:
        raise SystemExit(refuse(f"cannot create the run directory under --out ({type(err).__name__})"))
    (rd / "project" / "input.txt").write_bytes((HERE / "input.txt").read_bytes())
    release = str(rd / "gate") if fragment["mode"] == "offline" else args.gate
    gate = str(rd / "hold-never") if args.hold else release  # nobody ever creates hold-never: the worker stays blocked
    plan = dict(fragment, run_id=run_id, gate=gate, release=None if args.hold else release,
                sessions={"coordinator": f"cli:{run_id}", "restart": f"cli:{run_id}-r1"},
                wait_timeout=args.wait_timeout)
    write_config(rd, plan, args)
    save(rd / "plan.json", plan)
    save(out / "latest.json", {"run": str(rd.relative_to(out)) if rd != out else "."})
    return out, rd, plan


RUN_RE = re.compile(r"runs/[A-Za-z0-9][A-Za-z0-9_-]*")


def run_subdir(value):
    """The run directory under `out` named by a latest.json `run` value, or None unless it is `.` or `runs/<id>`."""
    if value == ".":
        return Path()
    if isinstance(value, str) and RUN_RE.fullmatch(value):
        return Path(value)
    return None


def resolve_run(out_arg):
    """(run dir, None) or (None, exit code)."""
    out = prepare_out(out_arg or default_out())
    latest = out / "latest.json"
    if os.path.lexists(latest) and not regular(latest):
        return None, refuse("latest.json is not a regular file")
    doc = load(latest) if regular(latest) else {"run": "."}
    value = doc.get("run") if isinstance(doc, dict) else None
    sub = run_subdir(value)
    if sub is None:
        return None, refuse("latest.json names a run that is not 'runs/<id>'")
    try:
        no_link_below(out, sub)
    except SystemExit as stop:
        return None, stop.code
    return out / sub, None


# --- the coordinator that gets killed ----------------------------------------------------------------------------

def coordinator(rd, args):
    plan = load(Path(rd) / "plan.json")
    refused = recheck_spend(plan, args)  # every entry, --internal-coordinator included, before any continuity call
    if refused:
        return refused
    who, rid = plan["sessions"]["coordinator"], plan["run_id"]
    code, out = cont(rd, plan, who, "start", "--config", str(Path(rd) / "dispatch.json"), "--role", WORKER_ROLE,
                     "--intent", WORKER_PROMPT, "--workdir", str(Path(rd) / "project"), "--session", who,
                     *spend_args(plan))
    if code != 0:
        print(f"start failed (continuity exit {int(code)})", file=sys.stderr)
        return 1
    job = out["job_id"]
    code, out = cont(rd, plan, who, "checkpoint", "--state", f"worker job {job} started, answer not yet checked",
                     "--next", f"native-system {rid}: check worker job {job} and publish answer",
                     "--evidence", "input.txt", "--session", who)
    if code != 0:
        print(f"checkpoint failed (continuity exit {int(code)})", file=sys.stderr)
        return 1
    save(Path(rd) / "coordinator.json", {"run_id": rid, "session": who, "pid": os.getpid(), "worker_job": job,
                                         "checker_jobs": [], "failures": [], "phase": "checkpointed"})
    while True:
        signal.pause()


def kill_coordinator(rd, plan):
    consent = ["--i-accept-spend", "--model", plan["model"]] if plan["mode"] == "live" else []
    proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--internal-coordinator", str(rd),
                             *consent], stdin=subprocess.DEVNULL, env=child_env(plan))
    path = Path(rd) / "coordinator.json"
    while proc.poll() is None:
        if path.exists() and load(path).get("phase") == "checkpointed":
            break
        time.sleep(0.05)
    if proc.poll() is not None:
        print("the coordinator exited before its checkpoint", file=sys.stderr)
        return None
    pid = proc.pid
    proc.send_signal(signal.SIGKILL)
    code = proc.wait()
    try:
        os.kill(pid, 0)
        gone = False
    except ProcessLookupError:
        gone = True
    co = load(path)
    co["kill"] = {"pid": pid, "signal": -code if code < 0 else None, "pid_gone": gone}
    save(path, co)
    if plan["release"]:
        Path(plan["release"]).touch()
    return co["kill"]


# --- the restarted coordinator ---------------------------------------------------------------------------------

def safe_text(value):
    """A short message for stderr/stdout: a failure payload's error text, or a path-free string; never a raw dict."""
    if isinstance(value, dict):
        value = value.get("error") or value.get("reason") or "no detail"
    text = "".join(c for c in str(value) if c.isprintable())
    return "[redacted: contains a path]" if any(m in text for m in ("/", "~", "\\")) else text[:200]


def shown_answer(answer):
    """An integer as is; anything else (model text) only as a hash and a length."""
    if answer is not None and not re.fullmatch(r"-?\d+", str(answer)):
        return f"sha256:{sha256(str(answer).encode())} len:{len(str(answer))}"
    return answer


def shown_verdict(verdict):
    return verdict if verdict in ("CONFIRMED", "REJECTED") else None


def clean_version(version):
    """Printable characters only, no `/`, bounded; None stays None."""
    if version is None:
        return None
    return "".join(c for c in str(version) if c.isprintable() and c != "/")[:80]


def clean_reasons(reasons):
    """A reason that may carry a host path (`/`, so also `~/` and `~user/`; or `\\`) is redacted, not published.
    Control characters are dropped, a newline, carriage return or tab becomes a space. Unlike safe_text, a bare
    `~` or `~user` with no slash (as in "~80%") is kept on purpose: it names no path."""
    clean = []
    for r in reasons or []:
        text = "".join(" " if c in "\n\r\t" else c for c in r if c.isprintable() or c in "\n\r\t") if isinstance(r, str) else "/"
        clean.append("[redacted: contains a path]" if "/" in text or "\\" in text else text[:200])
    return clean


def summary(ok, answer, verdict, ready, rc):
    state = "PASS" if ok and rc == 0 else ("NOT READY" if ok else "FAIL")
    print(f"answer: {shown_answer(answer)}\nchecker: {shown_verdict(verdict)}\nresult: {state}")
    if ready and ready.get("reasons"):
        print("not ready: " + "; ".join(clean_reasons(ready["reasons"])))
    return 0 if ok and rc == 0 else 1


def build_receipt(rd, plan, co):
    steps = [("worker", co["worker_job"])] + [("checker", j) for j in co["checker_jobs"]]
    jobs = []
    for step, job_id in steps:
        _, status = cont(rd, plan, plan["sessions"]["restart"], "status", "--job", job_id)
        row = status.get("jobs", {}).get(job_id, {})
        block, stdout = envelope(rd, job_id)
        exe = block["argv"][0]
        jobs.append({"id": job_id, "step": step, "role": row.get("role"), "outcome": row.get("outcome"),
                     "cause": row.get("cause"), "harness": block["harness"],
                     "harness_version": clean_version(block.get("harness_version")),
                     "executable": os.path.basename(exe),
                     "usage_total": usage_total(block), "stdout_sha256": sha256(stdout)})
    calls = (Path(rd) / "calls.jsonl").read_bytes()
    answer = Path(rd) / "project" / "answer.md"
    result = co.get("result", {})
    shown = shown_answer(result.get("answer"))
    verdict = shown_verdict(result.get("verdict"))
    ready = co.get("ready")
    if isinstance(ready, dict):
        ready = dict(ready, reasons=clean_reasons(ready.get("reasons")))
    rec = {
        "schema": SCHEMA, "mode": plan["mode"], "harness": plan["harness"],
        "run_id": plan["run_id"], "sessions": plan["sessions"], "kill": co.get("kill"), "jobs": jobs,
        "resume": co.get("resume"), "ready": ready, "answer": shown,
        "answer_sha256": sha256(answer.read_bytes()) if answer.exists() else None,
        "checker_verdict": verdict, "accepted_failed": co.get("accepted_failed", []),
        "calls": {"lines": calls.count(b"\n"), "sha256": sha256(calls)},
    }
    save(Path(rd) / "receipt.json", rec)
    return rec


def finish(rd, plan, co, ok, session):
    """Final checkpoint, ready, consume of that checkpoint, receipt, summary."""
    rid, project = plan["run_id"], Path(rd) / "project"
    failures = sorted(set(co["failures"])) if ok else []
    cont(rd, plan, session, "usage", "record", "--session", session, "--tokens", "0", "--source", "cli")
    text = COMPLETE_TEXT if ok else "stopped, needs operator"
    args = ["checkpoint", "--state", "answer published" if ok else "run stopped before an answer was published",
            "--next", f"native-system {rid}: {text}", "--evidence", "answer.md" if ok else "input.txt",
            "--session", session]
    if failures:
        args += ["--accept-failed", *failures]
    code, out = cont(rd, plan, session, *args)
    if code != 0:
        print(f"final checkpoint failed (continuity exit {int(code)})", file=sys.stderr)
        return 1
    rc, ready = cont(rd, plan, session, "ready", "--session", session)
    cont(rd, plan, session, "resume", "consume", "--session", session, "--cwd", str(project), "--source", "resume")
    co.update(ready={"exit": rc, "ready": bool(ready.get("ready")), "reasons": clean_reasons(ready.get("reasons"))},
              accepted_failed=failures)
    save(Path(rd) / "coordinator.json", co)
    build_receipt(rd, plan, co)
    result = co.get("result", {})
    print(f"receipt: {Path(rd) / 'receipt.json'}")
    return summary(ok, result.get("answer"), result.get("verdict"), co["ready"], rc)


COMPLETE_TEXT = "complete, nothing to do"


def act(rd, plan, co, job, session, revision, stop_after, args):
    """Only reached for a delivered `check worker job` Next of this run."""
    code, out = cont(rd, plan, session, "wait", "--job", job, "--timeout", str(plan["wait_timeout"]))
    if code == 5:
        print(f"job {job} is not banked ({out.get('state')})", file=sys.stderr)
        return 1
    result = {"answer": None, "verdict": None}
    co["result"] = result
    ok = False
    if code == 0:
        claim = job_answer(rd, job)
        result["answer"] = claim
        numbers = (Path(rd) / "project" / "input.txt").read_text(encoding="utf-8").split()
        if re.fullmatch(r"-?\d+", claim):
            for _ in range(2):
                refused = recheck_spend(plan, args)  # right before the spending call, not only at the entry
                if refused:
                    save(Path(rd) / "coordinator.json", co)
                    return refused
                code, out = cont(rd, plan, session, "run", "--config", str(Path(rd) / "dispatch.json"),
                                 "--role", "refuter", "--workdir", str(Path(rd) / "project"), "--session", session,
                                 "--intent", CHECKER_PROMPT.format(numbers=" ".join(numbers), claim=claim),
                                 *spend_args(plan))
                if "job_id" not in out:
                    break
                co["checker_jobs"].append(out["job_id"])
                save(Path(rd) / "coordinator.json", co)
                if code == 0:
                    result["verdict"] = normalize_verdict(job_answer(rd, out["job_id"]))
                    ok = result["verdict"] == "CONFIRMED"
                    break
                co["failures"].append(out["job_id"])
    else:
        print(f"worker job {job} did not succeed", file=sys.stderr)
    if ok:
        write_atomic(Path(rd) / "project" / "answer.md", f"{claim}\n")
        actions = Path(rd) / "actions.jsonl"
        known = [json.loads(x) for x in actions.read_text().splitlines()] if actions.exists() else []
        if not any(a["checkpoint_revision"] == revision for a in known):
            with open(actions, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"checkpoint_revision": revision, "run_id": plan["run_id"],
                                         "action": "publish-answer", "answer": claim}) + "\n")
    save(Path(rd) / "coordinator.json", co)
    if stop_after == "action" and ok:
        return 0
    return finish(rd, plan, co, ok, session)


def reaccept(rd, plan, co, session):
    """A `complete` Next of this run was delivered: accept every recorded failure again, checkpoint, ready.

    Only a run that really ended CONFIRMED with a published answer may be re-accepted; a bystander's Next text
    must not turn a rejected or unfinished run into a pass."""
    result = co.get("result") or {}
    if result.get("verdict") != "CONFIRMED" or not (Path(rd) / "project" / "answer.md").exists():
        print("the run did not end CONFIRMED with a published answer: nothing to re-accept", file=sys.stderr)
        return 1
    return finish(rd, plan, co, True, session)


def restart(rd, session, args, stop_after=None):
    plan, co = load(Path(rd) / "plan.json"), load(Path(rd) / "coordinator.json")
    rid, project = plan["run_id"], Path(rd) / "project"
    _, shown = cont(rd, plan, session, "resume", "show", "--session", session)
    if "next" not in shown:  # nothing to resume, or another session already took it
        print(json.dumps({k: shown.get(k) for k in ("delivered", "label", "notice", "consumed_by")}, sort_keys=True))
        return 3
    match = NEXT_RE.fullmatch(shown["next"])
    if not match or match.group(1) != rid:  # a bystander's Next: never consumed, never acted on
        print(f"the latest checkpoint Next does not belong to run {rid}")
        return 3
    code, taken = cont(rd, plan, session, "resume", "consume", "--session", session, "--cwd", str(project),
                       "--source", "resume")
    if code != 0 or not taken.get("delivered"):
        print(json.dumps({k: taken.get(k) for k in ("delivered", "label", "notice", "consumed_by")}))
        if code != 0 or shown.get("consumed_by") is None:  # `show` saw the Next free, `consume` lost it: the race
            return 3
        return 0 if (Path(rd) / "receipt.json").exists() else 1  # ours already: fine only when the run finished
    co["resume"] = {"session": session, "checkpoint_revision": taken["checkpoint_revision"], "delivered": True,
                    "label": taken["label"]}
    save(Path(rd) / "coordinator.json", co)
    text = match.group(2)
    check = CHECK_RE.fullmatch(text)
    if check:
        return act(rd, plan, co, check.group(1), session, taken["checkpoint_revision"], stop_after, args)
    if text == COMPLETE_TEXT:
        return reaccept(rd, plan, co, session)
    print(f"nothing to do for Next: {safe_text(text)}")
    return 1


# --- entry -----------------------------------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--fake-harness", choices=sorted(EXECUTABLE))
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--internal-coordinator", metavar="RUN_DIR", help=argparse.SUPPRESS)
    p.add_argument("--out")
    p.add_argument("--fake-bin")
    p.add_argument("--gate")
    p.add_argument("--harness", choices=sorted(EXECUTABLE))
    p.add_argument("--i-accept-spend", action="store_true")
    p.add_argument("--model")
    p.add_argument("--session")
    p.add_argument("--wait-timeout", type=float, default=300)
    p.add_argument("--fail-first", choices=("checker",))
    p.add_argument("--fail-always", choices=("worker",))
    p.add_argument("--hold", choices=("worker",))
    p.add_argument("--wrong-worker", action="store_true")
    p.add_argument("--stop-after", choices=("kill", "action"))
    return p


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    if args.internal_coordinator:
        return coordinator(args.internal_coordinator, args)
    if args.resume:
        rd, code = resolve_run(args.out)
        if rd is None:
            return code
        plan = load(Path(rd) / "plan.json")
        refused = recheck_spend(plan, args)
        if refused:
            return refused
        session = args.session or plan["sessions"]["restart"]
        return restart(rd, session, args, args.stop_after)
    if args.live and not args.harness:
        return refuse("--live needs --harness")
    if not (args.offline or args.fake_harness or args.live):
        return refuse("give one of --offline, --fake-harness, --live, --resume")
    if not args.offline and (args.fail_first or args.fail_always or args.hold or args.wrong_worker):
        return refuse("the failure hooks work with --offline only")
    fragment, code = preflight(args)
    if fragment is None:
        return code
    out, rd, plan = new_run(args, fragment)
    code, _ = cont(rd, plan, "parent", "init")
    if code != 0:
        print("init failed", file=sys.stderr)
        return 1
    kill = kill_coordinator(rd, plan)
    if kill is None:
        return 1
    if args.stop_after == "kill":
        return 0
    return restart(rd, plan["sessions"]["restart"], args, args.stop_after)


if __name__ == "__main__":
    sys.exit(main())
