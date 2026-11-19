#!/usr/bin/env python3
"""Optional unattended plan-pack runner (stdlib only). Off by default: nothing here schedules, enables or starts anything.

  kickoff --repo R --plan DIR --provider offline|fake|live [--model M] [--fake-bin D] [--budget-tokens N]
          [--budget-usd X --prices F] [--max-rounds N] [--max-hours H] [--round-timeout S] [--max-workers K]
          [--resource-limits FILE] [--detach] [--i-accept-spend]
  resume | status | pause | stop | refreeze | clear-corrupt | plan-id    (each takes --plan-id ID)

Every command prints one JSON object with a `reason`. Exit: 0 ok/done/waiting-human/paused/stopped, 1 another stop or invalid
state, 2 refused or bad usage, 3 busy or not authorised. Workers run through modules/continuity -> modules/orchestration; only
the Verify step runs inside modules/isolation. The runner integrates onto its own branch and never pushes.
"""

import argparse
import fcntl
import json
import os
import hmac
import random
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.autopilot import budget as B  # noqa: E402
from modules.autopilot import spec as SP  # noqa: E402
from modules.autopilot import state as S  # noqa: E402
from modules.autopilot import verify as V  # noqa: E402

CONT = ROOT / "modules" / "continuity" / "continuity.py"
RESOURCES = ROOT / "modules" / "control-plane-adapters" / "resources.py"
MARKER = "AI_ARCH_FAKE_HARNESS"
NONCE_ENV = "AIARCH_AUTOPILOT_LAUNCH_NONCE"
STABLE_EXCLUDE = ("job_id", "kickoff_id", "plan_id", "pid", "started_at", "finished_at", "elapsed", "unit")
CLEAN = {"done", "waiting-human", "paused", "stopped"}
AGENT_PREFIXES = ("CLAUDE_CODE_", "OPENCODE")
WAIT_PENDING = (3, 5)  # `continuity wait` exits: 5 still running (or acted, re-ask), 3 ledger busy; neither is a banked job
CAUSE_SET_INTERRUPTED = {"orphaned", "lost", "lost_in_launch", "abandoned", "result_missing", "not_launched", "invalid_result"}
SESSION_PREFIX = "cli:autopilot-"
GIT_ID = {"GIT_AUTHOR_NAME": "autopilot", "GIT_AUTHOR_EMAIL": "autopilot@example.invalid", "GIT_COMMITTER_NAME": "autopilot",
          "GIT_COMMITTER_EMAIL": "autopilot@example.invalid"}


class Refused(Exception):
    def __init__(self, code, reason, message=""):
        super().__init__(message or reason)
        self.code, self.reason, self.message = code, reason, message or reason


# --- output ---------------------------------------------------------------------------------------------------------

class Out:
    redact = staticmethod(lambda t: t)

    @classmethod
    def arm(cls, *paths):
        cls.redact = S.redactor(*paths)

    @classmethod
    def emit(cls, code, reason, **kw):
        doc = {"reason": reason}
        doc.update(kw)
        text = cls.redact(json.dumps(doc, sort_keys=True, allow_nan=False))
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        if code and not reason == "usage":
            sys.stderr.write(cls.redact(f"autopilot: {reason}: {kw.get('message', '')}") + "\n")
        return code


def plan_id_of(repo, plan):
    return "p-" + S.sha(os.path.realpath(repo) + "\0" + plan.strip("/"))[:12]


def state_base():
    home = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(os.path.realpath(home)) / "ai-architecture" / "autopilot"


def state_dir_for(plan_id):
    if not re.fullmatch(r"p-[0-9a-f]{12}", plan_id or ""):
        raise Refused(2, "bad-plan-id", "plan ids look like p-<12 hex>")
    return state_base() / plan_id


def resolve_plan_id(arg):
    if arg:
        return arg
    base = state_base()
    ids = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if len(ids) == 1:
        return ids[0]
    raise Refused(2, "plan-id-required", "give --plan-id (none or several states exist)")


# --- git ------------------------------------------------------------------------------------------------------------

def GR(repo, *args, input=None):
    """git, never raising on a non-zero exit: the CompletedProcess."""
    cmd = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false", *args]
    env = V.git_env()
    env.update(GIT_ID)
    return subprocess.run(cmd, capture_output=True, env=env, input=input, timeout=180)


def G(repo, *args):
    """git, text of stdout; raises RuntimeError on a non-zero exit."""
    r = GR(repo, *args)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.stdout.decode("utf-8", "surrogateescape").strip()


def show(repo, rev, path):
    r = GR(repo, "show", f"{rev}:{path}")
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


# --- provider -------------------------------------------------------------------------------------------------------

def find_exe(name, path):
    for d in path.split(os.pathsep):
        if os.path.isabs(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def has_marker(path):
    try:
        return MARKER.encode() in Path(path).read_bytes()[: 1 << 20]
    except OSError:
        return False


def pin_of(exe):
    real = os.path.realpath(exe)
    return {"path": real, "sha256": S.sha(Path(real).read_bytes())}


def check_fake(fake_bin, pin=None):
    """The `claude` on PATH must live in the marked fake dir. Returns the pin; raises Refused(2, ...)."""
    directory = os.path.realpath(fake_bin)
    exe = find_exe("claude", os.environ.get("PATH", ""))
    if exe is None or os.path.dirname(os.path.realpath(exe)) != directory:
        raise Refused(2, "fake-binary-shadowed", "the claude on PATH does not resolve inside --fake-bin")
    if not has_marker(exe):
        raise Refused(2, "fake-binary-unmarked", f"the fake binary lacks the {MARKER} marker")
    now = pin_of(exe)
    if pin is not None and now != pin:
        raise Refused(2, "fake-binary-changed", "the fake binary changed since kickoff")
    return now


# --- live gate (a tripwire against accidental agent kickoff, not a boundary) ----------------------------------------

def agent_env():
    return [k for k in os.environ if k == "CLAUDECODE" or k == "AI_ARCH_JOB_MARKER" or k.startswith(AGENT_PREFIXES)]


def agent_ancestor():
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            return False
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            if comm in ("claude", "opencode"):
                return True
            if any(e.startswith(b"CLAUDECODE=") for e in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")):
                return True
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def typed_confirmation(plan_id, what):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Refused(3, "tty-required", f"{what} needs a person at a terminal (stdin and stdout must be a TTY)")
    sys.stdout.write("Type the plan id to confirm: ")
    sys.stdout.flush()
    line = sys.stdin.readline().strip()
    sys.stdout.write("\n")
    if line != plan_id:
        raise Refused(3, "confirmation-mismatch", "the typed text is not the plan id")


def live_gate(args, plan_id, resume=False):
    """All of: --i-accept-spend, finite budget, no agent env, TTY, typed plan id, no agent ancestor."""
    if not getattr(args, "i_accept_spend", False):
        raise Refused(2, "spend-not-accepted", "a live run spends money: give --i-accept-spend")
    if args.budget_tokens is None and args.budget_usd is None:
        raise Refused(2, "budget-required", "a live run needs a finite budget")
    exe = find_exe("claude", os.environ.get("PATH", ""))
    if exe is None:
        raise Refused(2, "harness-not-found", "no claude executable on PATH")
    if has_marker(exe):
        raise Refused(2, "fake-binary-as-live", "the claude on PATH carries the fake marker; refusing it as live")
    if agent_env():
        raise Refused(3, "agent-environment", "an agent session environment is set; a person must start a live run")
    typed_confirmation(plan_id, "a live run")
    if agent_ancestor():
        raise Refused(3, "agent-ancestor", "an agent process is an ancestor of this command")
    return pin_of(exe)


# --- continuity ------------------------------------------------------------------------------------------------------

def cont(*args, timeout=600):
    r = subprocess.run([sys.executable, "-B", str(CONT), *map(str, args)], capture_output=True, text=True, env=dict(os.environ),
                       timeout=timeout, stdin=subprocess.DEVNULL)
    try:
        obj = json.loads(r.stdout)
    except ValueError:
        obj = None
    return r.returncode, obj if isinstance(obj, dict) else {}


# --- state skeleton ---------------------------------------------------------------------------------------------------

def new_state(plan_id, a, repo, plan, items_ids, frozen, user_branch, user_head, pin, prices_path, kickoff_id):
    return {
        "schema": S.SCHEMA, "plan_id": plan_id, "kickoff_id": kickoff_id, "repo": str(repo), "plan_rel": plan,
        "user_branch": user_branch, "user_head": user_head, "runner_branch": f"autopilot/{plan_id}",
        "provider": a.provider, "model": a.model, "fake_bin": os.path.realpath(a.fake_bin) if a.fake_bin else None, "pin": pin,
        "limits": {"budget_tokens": a.budget_tokens, "budget_usd": a.budget_usd, "prices": prices_path, "max_rounds": a.max_rounds,
                   "max_hours": a.max_hours, "round_timeout": a.round_timeout, "max_workers": a.max_workers,
                   "reserve_tokens": a.reserve_tokens, "resource_limits": a.resource_limits},
        "frozen": frozen, "spent": {"tokens": 0, "usd": 0.0, "unknown": 0}, "rounds": [], "chain_head": "0" * 64,
        "fix_rounds": {}, "inflight": [], "queued": [], "done": [], "stop_reason": None, "owner": None, "detached_pid": None,
        "elapsed": 0.0, "seq": 0, "consumed": None, "adopted": None,
    }


def stable_sha256(receipt):
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in STABLE_EXCLUDE}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return S.sha(S.canonical(strip(receipt)))


# --- the controller ---------------------------------------------------------------------------------------------------

class Controller:
    def __init__(self, sdir, doc):
        self.sdir, self.doc = Path(sdir), doc
        self.repo = Path(doc["repo"])
        self.work = self.sdir / "work"
        self.id = doc["plan_id"]
        self.plan = doc["plan_rel"]
        self.branch = doc["runner_branch"]
        self.lim = doc["limits"]
        self.prices = B.load_prices(self.lim["prices"]) if self.lim.get("prices") else None
        self.flag = {"stop": False}
        self.user_stop = False  # set while a person's `stop` is banking cancelled rounds
        self.t0 = time.monotonic()
        self.session = SESSION_PREFIX + self.id

    # -- helpers
    def save(self):
        S.write(self.sdir, self.doc)

    def tip(self):
        return G(self.work, "rev-parse", f"refs/heads/{self.branch}")

    def plan_texts(self, rev):
        return show(self.work, rev, f"{self.plan}/SPEC.md"), show(self.work, rev, f"{self.plan}/TRACKER.md"), \
            show(self.work, rev, f"{self.plan}/QUESTIONS.md")

    def control(self):
        p = self.sdir / "control.json"
        try:
            st = os.lstat(p)
            if st.st_size > 4096 or not os.path.isfile(p) or os.path.islink(p):
                return {}
            return S.strict_loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def elapsed(self):
        return self.doc["elapsed"] + (time.monotonic() - self.t0)

    def on_signal(self, signum, frame):
        self.flag["stop"] = True

    # -- stop paths
    def finish(self, reason):
        self.doc["stop_reason"] = reason
        self.doc["owner"] = None
        self.doc["elapsed"] = self.elapsed()
        self.save()
        self.write_receipt()
        return reason

    def write_receipt(self):
        d = self.doc
        rounds = [{k: r.get(k) for k in ("item", "job_id", "usage", "cost", "touches_ok", "verify_rc", "sandbox", "outcome")} for r in d["rounds"]]
        rec = {"schema": "autopilot-receipt/1", "provider": d["provider"], "rounds": rounds, "stop_reason": d["stop_reason"],
               "budget": {"limit": {"tokens": self.lim["budget_tokens"], "usd": self.lim["budget_usd"]},
                          "spent": d["spent"]["tokens"], "usd": d["spent"]["usd"], "unknown": d["spent"]["unknown"]},
               "frozen": d["frozen"], "resource": {"requested": os.path.basename(self.lim["resource_limits"]) if self.lim.get("resource_limits") else None}}
        tmp = self.sdir / "receipt.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(rec, indent=1, sort_keys=True, allow_nan=False))
        os.replace(tmp, self.sdir / "receipt.json")

    # -- selection
    def ready_items(self, items, done, questions):
        out = []
        for it in items:
            f = it["fields"]
            if it["id"] in done or SP.gate(f) != "auto":
                continue
            if any(n not in done for n in SP.needs(f)):
                continue
            if any(SP.is_open(questions, q) for q in SP.blocked_by(f)):
                continue
            out.append(it)
        return out

    def batch_of(self, ready, k):
        batch = []
        for it in ready:
            if len(batch) >= k:
                break
            if all(SP.disjoint(SP.touches(it["fields"]), SP.touches(b["fields"])) for b in batch):
                batch.append(it)
        return batch

    # -- dispatch config
    def dispatch_config(self, usd_left):
        d = self.doc
        if d["provider"] == "offline":
            seat = {"harness": "local-command", "command": [sys.executable, "-c", "import sys; sys.stdin.read()"]}
            spend = False
        else:
            seat = {"harness": "claude-code", "model": d["model"]}
            if usd_left is not None:
                seat["max_budget_usd"] = round(max(usd_left, 0.000001), 6)
            spend = True
        cfg = {"seats": {"main": seat}, "roles": {"builder": ["main"]}, "permit": ["builder"], "allow_write": True, "allow_spend": spend,
               "timeout_seconds": self.lim["round_timeout"], "worktree_root": str(self.sdir / "worktrees")}
        p = self.sdir / "dispatch.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        os.chmod(p, 0o600)
        return p, spend

    def prompt_for(self, it, attempt):
        f = it["fields"]
        lines = [f"Item: {it['id']}", f"Attempt: {attempt}", f"Plan: {self.plan}", f"What: {f.get('What', '')}",
                 f"Acceptance: {f.get('Acceptance', '')}", f"Verify: {f.get('Verify', '')}", f"Touches: {f.get('Touches', '')}", "",
                 "Make the change inside the Touches prefixes only and commit it. Do not edit the plan files (SPEC, TRACKER, QUESTIONS).",
                 "The runner runs the Verify command itself in a sandbox."]
        if attempt > 1:
            lines.append("A previous attempt did not pass Verify.")
        return "\n".join(lines) + "\n"

    # -- the loop
    def run(self, resumed=False):
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        d = self.doc
        d["owner"] = S.owner_record(os.getpid(), d["kickoff_id"])
        d["stop_reason"] = None
        self.save()
        try:
            if resumed:
                early = self.settle_inflight()
                d["consumed"] = consume_next(self)  # the latest checkpoint's Next is delivered at most once
                if early:
                    return self.finish(early)
            while True:
                reason = self.step()
                if reason:
                    return self.finish(reason)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
            self.doc["stop_reason"] = "error"
            self.doc["owner"] = None
            self.doc["adopted"] = {"error": error.__class__.__name__}
            self.save()
            self.write_receipt()
            return "error"

    def step(self):
        d = self.doc
        if self.flag["stop"]:
            return self.do_stop([])
        ctl = self.control()
        tip = self.tip()
        spec_t, tracker_t, q_t = self.plan_texts(tip)
        if spec_t is None or tracker_t is None:
            return "verify-drift"
        items = SP.parse_spec(spec_t)
        done = SP.parse_tracker_done(tracker_t)
        questions = SP.open_questions(q_t or "")
        d["done"] = sorted(done)
        ready = self.ready_items(items, done, questions)
        if ctl.get("pause"):
            d["queued"] = [i["id"] for i in ready]
            return "paused"
        r = GR(self.repo, "rev-parse", f"refs/heads/{d['user_branch']}" if d["user_branch"] else "HEAD")
        if r.returncode != 0 or r.stdout.decode().strip() != d["user_head"]:
            return "branch-moved"
        if G(self.work, "status", "--porcelain", "--untracked-files=no"):
            return "dirty-tree"
        drift = SP.drift(d["frozen"], spec_t, q_t)
        if drift:
            return drift
        if all(i["id"] in done for i in items):
            return "done"
        if not ready:
            return "waiting-human"
        if self.lim["max_hours"] is not None and self.elapsed() > self.lim["max_hours"] * 3600:
            return "budget"
        allowed = self.lim["max_rounds"] - len(d["rounds"]) if self.lim["max_rounds"] is not None else 10 ** 9
        if allowed <= 0 or B.refuse_start(self.lim, d["spent"]):
            return "budget"
        by_id = {i["id"]: i for i in items}
        for it in ready:
            root = SP.lineage_root(it)
            if d["fix_rounds"].get(root, 0) > SP.fix_cap(items, it):
                return "verify-failed"
        if d["provider"] == "fake":
            try:
                check_fake(d["fake_bin"], d["pin"])
            except Refused as e:
                return e.reason
        batch = self.batch_of(ready, min(self.lim["max_workers"], allowed))
        d["queued"] = [i["id"] for i in ready if i not in batch]
        return self.run_batch(batch, items, by_id, tip)

    # -- one batch of workers
    def run_batch(self, batch, items, by_id, base):
        d = self.doc
        usd_left = (self.lim["budget_usd"] - d["spent"]["usd"]) if self.lim["budget_usd"] is not None else None
        tokens_left = (self.lim["budget_tokens"] - d["spent"]["tokens"]) if self.lim["budget_tokens"] is not None else None
        cfg, spend = self.dispatch_config(usd_left)
        entries = []
        for it in batch:
            d["seq"] += 1
            attempt = 1 + sum(1 for r in d["rounds"] if r["item"] == it["id"] and r["outcome"] != "accepted")
            job = f"r{d['seq']}-{re.sub(r'[^A-Za-z0-9]', '', it['id'])[:12]}-{random.randrange(16 ** 6):06x}"
            entries.append({"item": it["id"], "job_id": job, "attempt": attempt, "base": base, "status": "admitted",
                            "cap_tokens": max(tokens_left, 0) if tokens_left is not None else None})
        d["inflight"] = entries
        self.save()
        (self.sdir / "prompts").mkdir(exist_ok=True, mode=0o700)
        for e in entries:
            if self.flag["stop"]:
                break
            prompt = self.sdir / "prompts" / f"{e['job_id']}.txt"
            prompt.write_text(self.prompt_for(by_id[e["item"]], e["attempt"]), encoding="utf-8")
            args = ["start", "--project", self.work, "--config", cfg, "--role", "builder", "--prompt-file", prompt, "--workdir", self.work,
                    "--job-id", e["job_id"]]
            if tokens_left is not None:
                args += ["--budget-tokens", max(tokens_left, 0)]
            if spend:
                args.append("--allow-spend")
            rc, out = cont(*args)
            if rc != 0:
                e["status"] = "refused"
                e["why"] = (out.get("error") or "")[:160]
            else:
                e["status"] = "launched"
            self.save()
        waiting = [e for e in entries if e["status"] == "launched"]
        banked = {}
        while waiting:
            if self.flag["stop"]:
                return self.do_stop(entries)
            for e in list(waiting):
                rc, out = cont("wait", "--project", self.work, "--job", e["job_id"], "--timeout", 1)
                if rc not in WAIT_PENDING:
                    banked[e["job_id"]] = (rc, out)
                    waiting.remove(e)
        refused = [e for e in entries if e["status"] == "refused"]
        for e in refused:
            self.record(e, {"outcome": "interrupted", "usage": None, "touches_ok": None, "verify_rc": None, "sandbox": False,
                            "why": e.get("why")}, unknown=True)
        stop = None
        for e in entries:
            if e["job_id"] in banked:  # every banked sibling is recorded even once one round has asked to stop the run
                st = self.process(e, banked[e["job_id"]], by_id, items)
                stop = stop or st
        if stop is None and refused and self.budgeted():
            stop = "usage-unknown"  # a round that never started has no usage to count: not a free round under a budget
        d["inflight"] = []
        self.save()
        return stop

    def settle_inflight(self):
        d = self.doc
        items = SP.parse_spec(self.plan_texts(self.tip())[0] or "")
        by_id = {i["id"]: i for i in items}
        entries, d["inflight"] = d["inflight"], []
        keep = []
        stop = None
        for e in entries:
            if e["status"] == "admitted" and cont("status", "--project", self.work, "--job", e["job_id"])[0] == 0:
                e["status"] = "launched"  # started just before the controller died
            if e["status"] != "launched":
                continue
            end = time.monotonic() + self.lim["round_timeout"] + 120
            rc, out = 5, {}
            while time.monotonic() < end:
                rc, out = cont("wait", "--project", self.work, "--job", e["job_id"], "--timeout", 2)
                if rc not in WAIT_PENDING or self.flag["stop"]:
                    break
            if rc in WAIT_PENDING:
                keep.append(e)
                continue
            st = self.process(e, (rc, out), by_id, items)  # a stop from one round never drops its siblings
            stop = stop or st
        d["inflight"] = keep
        self.save()
        return stop

    def budgeted(self):
        return self.lim["budget_tokens"] is not None or self.lim["budget_usd"] is not None

    def record(self, e, rec, unknown=False, usage=None, tokens=None, usd=None):
        d = self.doc
        spent = d["spent"]
        if unknown or tokens is None:
            spent["unknown"] += 1
            if self.lim["budget_tokens"] is not None:
                # pessimistic charge for a round with no usage event: at least the reserve, and (outside a person's own stop)
                # the whole per-round token cap the dispatcher was given, i.e. everything that was left when the round began
                cap = 0 if self.user_stop else (e.get("cap_tokens") or 0)
                spent["tokens"] += max(self.lim["reserve_tokens"], cap)
        else:
            spent["tokens"] += tokens
            if usd is not None:
                spent["usd"] += usd
        rec = dict(rec, n=len(d["rounds"]) + 1, item=e["item"], attempt=e["attempt"], job_id=e["job_id"], cost=usd)
        rec.setdefault("usage", usage)
        S.chain_append(d, rec)
        self.save()
        cont("checkpoint", "--project", self.work, "--state", f"round {rec['n']} {e['item']} {rec['outcome']}", "--next",
             f"autopilot {self.id} round {rec['n']}", "--evidence", f"autopilot-round-{rec['n']}", "--session", self.session)

    def envelope(self, job_id):
        p = self.work / ".ai-continuity" / "ledger" / "runs" / job_id / "result.json"
        try:
            doc = S.strict_loads(p.read_text())
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def cleanup_worker(self, job_id, branch=None):
        path = self.sdir / "worktrees" / job_id
        GR(self.work, "worktree", "remove", "--force", str(path))
        GR(self.work, "branch", "-D", branch or f"dispatch/builder-{job_id}")

    def process(self, e, banked, by_id, items):
        """Classify one banked job. Returns a stop reason or None."""
        d = self.doc
        rc, out = banked
        it = by_id[e["item"]]
        env = self.envelope(e["job_id"])
        disp = env.get("dispatch") if isinstance(env.get("dispatch"), dict) else {}
        wt = disp.get("worktree") if isinstance(disp.get("worktree"), dict) else {}
        branch = wt.get("branch") or f"dispatch/builder-{e['job_id']}"
        cause = out.get("cause")
        if cause == "cancelled" or (cause in CAUSE_SET_INTERRUPTED) or not env:
            self.record(e, {"outcome": "cancelled" if cause == "cancelled" else "interrupted", "touches_ok": None, "verify_rc": None,
                            "sandbox": False}, unknown=True)
            self.cleanup_worker(e["job_id"], branch)
            return "usage-unknown" if self.budgeted() else None  # unknown usage is never $0 under a budget: the run stops
        usage = disp.get("usage")
        tokens, usd = B.charge(usage, d["model"], self.prices)
        budgeted = self.lim["budget_tokens"] is not None or self.lim["budget_usd"] is not None
        if budgeted and (tokens is None or (B.needs_cost(self.lim) and usd is None)):
            self.record(e, {"outcome": "unknown-usage", "touches_ok": None, "verify_rc": None, "sandbox": False}, unknown=True, usage=usage)
            self.cleanup_worker(e["job_id"], branch)
            return "usage-unknown"
        base = e["base"]
        r = GR(self.work, "rev-parse", f"refs/heads/{branch}")
        wtip = r.stdout.decode().strip() if r.returncode == 0 else None
        fails = {"outcome": None, "touches_ok": None, "verify_rc": None, "sandbox": False}
        stop = None
        if disp.get("exit_code") not in (0, None) or env.get("result", {}).get("outcome") != "succeeded" or not wtip:
            self.record(e, dict(fails, outcome="worker-failed"), usage=usage, tokens=tokens, usd=usd)
            self.cleanup_worker(e["job_id"], branch)
            return self.fail_attempt(it, items)
        # plan files at the worker tip against the freeze (gate / drift), before anything else
        spec_t, tracker_t, q_t = self.plan_texts(wtip)
        drift = SP.drift(d["frozen"], spec_t, q_t) if spec_t is not None else "verify-drift"
        if drift:
            self.record(e, dict(fails, outcome=drift), usage=usage, tokens=tokens, usd=usd)
            self.cleanup_worker(e["job_id"], branch)
            return drift
        ok_anc = GR(self.work, "merge-base", "--is-ancestor", base, wtip).returncode == 0
        changed = GR(self.work, "diff", "--name-only", "--no-renames", "-z", base, wtip)
        names = [n for n in changed.stdout.decode("utf-8", "surrogateescape").split("\0") if n] if changed.returncode == 0 else []
        prefixes = SP.touches(it["fields"])
        if not ok_anc or any(not SP.path_matches(n, prefixes) for n in names):
            self.record(e, dict(fails, outcome="touches-violation", touches_ok=False), usage=usage, tokens=tokens, usd=usd)
            self.cleanup_worker(e["job_id"], branch)
            return "touches-violation"
        cur = self.tip()
        candidate = wtip
        if GR(self.work, "merge-base", "--is-ancestor", cur, wtip).returncode != 0:  # K>1: integrate serially
            mt = GR(self.work, "merge-tree", "--write-tree", f"--merge-base={base}", cur, wtip)
            if mt.returncode != 0:
                self.record(e, dict(fails, outcome="conflict", touches_ok=True), usage=usage, tokens=tokens, usd=usd)
                self.cleanup_worker(e["job_id"], branch)
                return self.fail_attempt(it, items)
            tree = mt.stdout.decode().splitlines()[0].strip()
            candidate = G(self.work, "commit-tree", tree, "-p", cur, "-m", f"autopilot: {it['id']} ({branch})")
        cmd = SP.verify_command(it["fields"])
        vdir = self.sdir / "verify" / f"{d['seq']}-{e['item']}"
        V.cleanup(vdir)
        V.materialize(self.work, candidate, vdir)
        rcv, why = V.run(cmd, vdir, self.lim["round_timeout"]) if cmd else (1, "no Verify command")
        V.cleanup(vdir)
        if rcv == 3:
            self.record(e, dict(fails, outcome="unverifiable", touches_ok=True, verify_rc=3), usage=usage, tokens=tokens, usd=usd)
            self.cleanup_worker(e["job_id"], branch)
            return "unverifiable"
        if rcv != 0:
            self.record(e, dict(fails, outcome="verify-failed", touches_ok=True, verify_rc=rcv, sandbox=True), usage=usage, tokens=tokens, usd=usd)
            self.cleanup_worker(e["job_id"], branch)
            return self.fail_attempt(it, items)
        G(self.work, "update-ref", f"refs/heads/{self.branch}", candidate, cur)
        G(self.work, "--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904", "reset", "--hard", "-q", candidate)  # empty tree: a planted .gitattributes cannot trigger a filter
        tpath = self.work / self.plan / "TRACKER.md"
        tpath.write_text(SP.mark_done(tpath.read_text(), it["id"]), encoding="utf-8")
        G(self.work, "add", "--", f"{self.plan}/TRACKER.md")
        G(self.work, "commit", "-q", "-m", f"autopilot: {it['id']} done", "--no-verify")
        self.record(e, dict(fails, outcome="accepted", touches_ok=True, verify_rc=0, sandbox=True), usage=usage, tokens=tokens, usd=usd)
        self.cleanup_worker(e["job_id"], branch)
        d["done"] = sorted(set(d["done"]) | {it["id"]})
        if B.exceeded(self.lim, d["spent"]):
            stop = "budget"
        return stop

    def fail_attempt(self, it, items):
        d = self.doc
        root = SP.lineage_root(it)
        d["fix_rounds"][root] = d["fix_rounds"].get(root, 0) + 1
        self.save()
        if d["fix_rounds"][root] > SP.fix_cap(items, it):
            return "verify-failed"
        return None

    # -- stop (SIGTERM): drain, cancel, bank cancelled, mark the unlaunched
    def do_stop(self, entries):
        d = self.doc
        self.user_stop = True
        cont("drain", "start", "--project", self.work, "--reason", "autopilot-stop")
        live = [e for e in d["inflight"] if e["status"] == "launched"]
        for e in live:
            cont("cancel", "--project", self.work, "--job", e["job_id"], timeout=180)
        cont("drain", "wait", "--project", self.work, "--timeout", 90, timeout=180)
        items = SP.parse_spec(self.plan_texts(self.tip())[0] or "")
        by_id = {i["id"]: i for i in items}
        for e in live:
            rc, out = cont("wait", "--project", self.work, "--job", e["job_id"], "--timeout", 5)
            end = time.monotonic() + 30
            while rc == 3 and time.monotonic() < end:  # a busy ledger says nothing about the job: ask again
                rc, out = cont("wait", "--project", self.work, "--job", e["job_id"], "--timeout", 5)
            self.process(e, (rc, out), by_id, items)  # a job that finished before the cancel landed is judged, not discarded
            self.cleanup_worker(e["job_id"])
        d["queued"] = sorted(set(d["queued"]) | {e["item"] for e in d["inflight"] if e["status"] == "admitted"})
        d["inflight"] = []
        self.save()
        return "stopped"

    def stop_and_finish(self):
        return self.do_stop([])


# --- commands ---------------------------------------------------------------------------------------------------------

def acquire(sdir):
    fd = os.open(S.lock_path(sdir), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Refused(3, "busy", "another controller holds this plan")
    return fd


def exit_for(reason):
    if reason in CLEAN:
        return 0
    if reason == "fake-binary-changed":
        return 2
    return 1


def print_result(doc, extra=None):
    reason = doc["stop_reason"]
    return Out.emit(exit_for(reason), reason, plan_id=doc["plan_id"], rounds=len(doc["rounds"]), state="stopped",
                    stop_reason=reason, spent=doc["spent"], **(extra or {}))


def parse_budget(args):
    a = args
    if a.budget_tokens is not None:
        v = B.parse_positive(a.budget_tokens, integer=True)
        if v is None:
            raise Refused(2, "budget-invalid", "--budget-tokens must be a finite integer > 0")
        a.budget_tokens = v
    if a.budget_usd is not None:
        v = B.parse_positive(a.budget_usd)
        if v is None:
            raise Refused(2, "budget-invalid", "--budget-usd must be a finite number > 0")
        a.budget_usd = v
        if not a.prices:
            raise Refused(2, "prices-required", "--budget-usd needs --prices FILE (no price table ships)")
    for name, integer in (("max_rounds", True), ("max_hours", False), ("round_timeout", False), ("max_workers", True), ("reserve_tokens", True)):
        raw = getattr(a, name)
        if raw is None:
            continue
        v = B.parse_positive(raw, integer=integer)
        if v is None:
            raise Refused(2, "budget-invalid", f"--{name.replace('_', '-')} must be a finite number > 0")
        setattr(a, name, v)


def cmd_kickoff(a):
    os.umask(0o077)
    if a.resource_limits and not a.detach:
        raise Refused(2, "resource-limits-need-detach", "--resource-limits only applies to a --detach controller; a foreground run would be unbounded")
    if a.provider in ("fake", "live") and a.budget_tokens is None and a.budget_usd is None:
        raise Refused(2, "budget-required", f"--provider {a.provider} needs a finite --budget-tokens or --budget-usd")
    parse_budget(a)
    a.max_workers = min(a.max_workers or 1, 4)
    a.round_timeout = a.round_timeout or 600
    a.reserve_tokens = a.reserve_tokens if a.reserve_tokens is not None else B.DEFAULT_RESERVE_TOKENS
    if a.provider in ("fake", "live") and not a.model:
        raise Refused(2, "model-required", f"--provider {a.provider} needs --model")
    if a.provider == "offline" and a.budget_usd is not None:
        raise Refused(2, "budget-invalid", "a USD budget needs a provider that reports cost; offline reports none")
    prices_path = None
    if a.prices:
        try:
            B.load_prices(a.prices)
        except (OSError, ValueError) as e:
            raise Refused(2, "prices-invalid", f"price table unreadable: {e.__class__.__name__}")
        prices_path = os.path.realpath(a.prices)
    repo = Path(os.path.realpath(a.repo))
    plan = os.path.normpath(a.plan).strip("/")
    if plan.startswith("..") or os.path.isabs(a.plan):
        raise Refused(2, "plan-invalid", "--plan must be a path inside the repository")
    top = V.git(repo, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or os.path.realpath(top.stdout.decode().strip()) != str(repo):
        raise Refused(2, "repo-invalid", "--repo must be the top level of a git work tree")
    plan_id = plan_id_of(repo, plan)
    Out.arm(("<repo>", repo), ("<state>", state_base() / plan_id))
    for f in ("SPEC.md", "TRACKER.md"):
        if V.git(repo, "ls-files", "--error-unmatch", "--", f"{plan}/{f}", check=False).returncode != 0:
            raise Refused(2, "plan-untracked", f"{plan}/{f} is not tracked in the repository (commit the plan first)")
    head = G(repo, "rev-parse", "HEAD")
    spec_t, tracker_t, q_t = show(repo, head, f"{plan}/SPEC.md"), show(repo, head, f"{plan}/TRACKER.md"), show(repo, head, f"{plan}/QUESTIONS.md")
    if spec_t is None or tracker_t is None:
        raise Refused(2, "plan-untracked", f"{plan}/SPEC.md and TRACKER.md must be committed at HEAD")
    items = SP.parse_spec(spec_t)
    if not items:
        raise Refused(2, "plan-invalid", "no items found in SPEC.md")
    rows = SP.tracker_rows(tracker_t)
    for it in items:
        if it["id"] not in rows:
            raise Refused(2, "tracker-row-missing", f"TRACKER.md has no row for item {it['id']}")
        for prefix in SP.touches(it["fields"]):
            check_touches(repo, it["id"], prefix)
        if not SP.verify_command(it["fields"]):
            raise Refused(2, "plan-invalid", f"item {it['id']} has no Verify command")
    ub = GR(repo, "symbolic-ref", "--short", "-q", "HEAD")
    user_branch = ub.stdout.decode().strip() if ub.returncode == 0 else None
    pin = None
    if a.provider == "fake":
        if not a.fake_bin or not Path(a.fake_bin).is_dir():
            raise Refused(2, "fake-bin-required", "--provider fake needs --fake-bin DIR")
        pin = check_fake(a.fake_bin)
    elif a.provider == "live":
        pin = live_gate(a, plan_id)
    if a.resource_limits:
        read_limits(a.resource_limits)
    if a.provider == "offline" and (a.budget_tokens is None and a.budget_usd is None):
        pass
    # --- validated: from here on the state dir is created
    base = state_base()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in (base, base.parent):
        if os.path.islink(p):
            raise Refused(2, "state-symlink", "the state base is a symlink")
    sdir = base / plan_id
    try:
        os.mkdir(sdir, 0o700)
    except FileExistsError:
        pass
    if os.path.islink(sdir) or os.stat(sdir).st_uid != S.expected_uid():
        raise Refused(2, "state-foreign", "the state dir is a symlink or owned by someone else")
    fd = acquire(sdir)
    try:
        if os.path.lexists(sdir / "state.json") or S.marker_exists(sdir):
            try:
                S.load(sdir)
            except S.Corrupt as e:
                S.write_marker(sdir, str(e))
                raise Refused(1, "state-corrupt", str(e))
            if S.marker_exists(sdir):
                raise Refused(1, "state-corrupt", "a state-corrupt marker is present; clear it with clear-corrupt")
            raise Refused(2, "already-kicked-off", "this plan already has a state: use resume")
        foreign = [p.name for p in sdir.iterdir() if p.name not in ("lock",)]
        if foreign:
            raise Refused(2, "state-foreign", "the state dir has files this tool did not create")
        branch = f"autopilot/{plan_id}"
        if GR(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
            raise Refused(2, "branch-exists", f"branch {branch} already exists")
        (sdir / "worktrees").mkdir(mode=0o700)
        G(repo, "worktree", "add", "-q", "-b", branch, str(sdir / "work"), head)
        rc, out = cont("init", "--project", sdir / "work")
        if rc != 0:
            raise Refused(1, "continuity-init-failed", str(out.get("error", ""))[:160])
        kickoff_id = S.sha(f"{plan_id}:{os.getpid()}:{time.time()}")[:16]
        doc = new_state(plan_id, a, repo, plan, [i["id"] for i in items], SP.freeze(spec_t, q_t), user_branch, head, pin, prices_path, kickoff_id)
        doc["done"] = sorted(SP.parse_tracker_done(tracker_t))
        S.write(sdir, doc)
        return go(sdir, doc, fd, a.detach, a.resource_limits, resumed=False)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def check_touches(repo, item_id, prefix):
    bad = Refused(2, "touches-outside-repo",
                  f"Touches prefix {prefix!r} of item {item_id} resolves outside the repository work tree (nested or ignored repository)")
    if prefix.startswith("/") or ".." in prefix.split("/") or re.search(r"[*?\[]", prefix):
        raise Refused(2, "touches-invalid", f"Touches prefix {prefix!r} of item {item_id} must be a literal relative path prefix")
    rel = prefix.rstrip("/")
    cur = repo
    for part in rel.split("/"):
        cur = cur / part
        if (cur / ".git").exists():
            raise bad
        if not cur.exists():
            break
    real = os.path.realpath(repo / rel)
    if real != str(repo) and not real.startswith(str(repo) + os.sep):
        raise bad
    if V.git(repo, "check-ignore", "-q", "--", prefix, check=False).returncode == 0:
        raise bad


def read_limits(path):
    try:
        doc = S.strict_loads(Path(path).read_text())
    except (OSError, ValueError):
        raise Refused(2, "resource-limits-invalid", "--resource-limits FILE is unreadable or not strict JSON")
    if not isinstance(doc, dict):
        raise Refused(2, "resource-limits-invalid", "--resource-limits FILE must hold an object")
    return doc


def go(sdir, doc, fd, detach, resource_limits, resumed):
    """Run the controller here, or re-exec it detached (new session, optionally inside the resource adapter's scope)."""
    if not detach:
        ctl = Controller(sdir, doc)
        ctl.run(resumed=resumed)
        return print_result(ctl.doc)
    nonce = secrets.token_hex(16)  # single use, goes to the child by environment (never argv); only its hash is stored
    doc["launch"] = {"nonce_sha": S.sha(nonce)}
    S.write(sdir, doc)
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_controller", "--plan-id", doc["plan_id"], "--resumed" if resumed else "--fresh"]
    if resource_limits:
        lim = read_limits(resource_limits)
        cmd = [sys.executable, "-B", str(RESOURCES), "run", "--backend", lim.get("backend", "systemd-scope"), "--limits", str(resource_limits),
               "--unit-file", str(sdir / "resource.unit"), "--evidence-file", str(sdir / "resource-evidence.json"), "--", *argv]
    else:
        cmd = argv
    log = os.open(sdir / "controller.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.close(fd)  # the child takes the lock itself; this process must not hold it
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                            env=dict(os.environ, **{NONCE_ENV: nonce}))
    os.close(log)
    end = time.monotonic() + 40
    ok = False
    while time.monotonic() < end:
        try:
            cur = S.load(sdir)
        except S.Corrupt:
            cur = None
        if cur and (cur["owner"] or {}).get("pid") is not None and (cur["owner"] or {}).get("pid") != os.getpid() and cur["owner"]["kickoff_id"] == doc["kickoff_id"]:
            ok = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    if resource_limits:
        unit = (sdir / "resource.unit")
        for _ in range(100):
            if unit.exists() and unit.read_text().strip():
                (sdir / "resource.json").write_text(json.dumps({"unit": unit.read_text().strip(), "requested": os.path.basename(str(resource_limits))}))
                break
            time.sleep(0.05)
    if not ok:
        code = proc.poll()
        return Out.emit(3 if code == 3 else 1, "busy" if code == 3 else "detach-failed", message="the detached controller did not take ownership")
    cur = S.load(sdir)
    cur["detached_pid"] = proc.pid
    detached_write(sdir, cur, proc.pid)
    return Out.emit(0, "detached", plan_id=doc["plan_id"], detached_pid=proc.pid)


def detached_write(sdir, cur, pid):
    """Record the detached pid without racing the controller: a side file, merged by status."""
    (sdir / "detached.json").write_text(json.dumps({"pid": pid}))


def cmd_controller(a):
    """Internal: the re-exec'd controller. Takes the lock itself (retrying while the launcher lets go)."""
    os.umask(0o077)
    sdir = state_dir_for(a.plan_id)
    fd = None
    for _ in range(100):
        try:
            fd = acquire(sdir)
            break
        except Refused:
            time.sleep(0.1)
    if fd is None:
        raise Refused(3, "busy", "another controller holds this plan")
    try:
        nonce = os.environ.pop(NONCE_ENV, "")  # never inherited by workers
        doc = S.load(sdir)
        launch = doc.get("launch") if isinstance(doc.get("launch"), dict) else {}
        want = launch.get("nonce_sha")
        if not nonce or not isinstance(want, str) or not hmac.compare_digest(S.sha(nonce), want):
            raise Refused(3, "controller-not-authorised", "this internal command is started only by kickoff/resume --detach")
        doc["launch"] = None  # single use
        S.write(sdir, doc)
        Out.arm(("<repo>", doc["repo"]), ("<state>", sdir))
        if doc["provider"] == "live":
            exe = find_exe("claude", os.environ.get("PATH", ""))  # the launcher passed live_gate; what a detached child can still check
            if exe is None:
                raise Refused(2, "harness-not-found", "no claude executable on PATH")
            if has_marker(exe):
                raise Refused(2, "fake-binary-as-live", "the claude on PATH carries the fake marker; refusing it as live")
            if agent_env():
                raise Refused(3, "agent-environment", "an agent session environment is set; a person must start a live run")
            if doc["pin"] is None or pin_of(exe) != doc["pin"]:
                raise Refused(2, "fake-binary-changed", "the harness binary changed since the launcher's live gate")
        ctl = Controller(sdir, doc)
        ctl.run(resumed=a.resumed)
        return print_result(ctl.doc)
    finally:
        os.close(fd)


def cmd_resume(a):
    os.umask(0o077)
    pid_ = resolve_plan_id(a.plan_id)
    sdir = state_dir_for(pid_)
    Out.arm(("<state>", sdir))
    if not sdir.is_dir():
        raise Refused(2, "no-state", "no state for this plan id")
    fd = acquire(sdir)
    try:
        try:
            doc = S.load(sdir)
        except S.Corrupt as e:
            S.write_marker(sdir, str(e))
            raise Refused(1, "state-corrupt", str(e))
        if S.marker_exists(sdir):
            raise Refused(1, "state-corrupt", "a state-corrupt marker is present; clear it with clear-corrupt")
        Out.arm(("<repo>", doc["repo"]), ("<state>", sdir))
        lim = doc["limits"]
        if lim.get("resource_limits") and not a.detach:
            raise Refused(2, "resource-limits-need-detach", "this run was started with --resource-limits: resume it with --detach so they apply")
        for name, key, integer in (("budget_tokens", "budget_tokens", True), ("budget_usd", "budget_usd", False), ("max_rounds", "max_rounds", True),
                                   ("max_hours", "max_hours", False), ("max_workers", "max_workers", True)):
            raw = getattr(a, name)
            if raw is None:
                continue
            v = B.parse_positive(raw, integer=integer)
            if v is None:
                raise Refused(2, "budget-invalid", f"--{name.replace('_', '-')} must be a finite number > 0")
            if key == "max_workers":
                v = min(v, 4)
            lim[key] = v
        if lim["budget_tokens"] is not None and lim["budget_tokens"] < doc["spent"]["tokens"] or \
                lim["budget_usd"] is not None and lim["budget_usd"] < doc["spent"]["usd"]:
            raise Refused(2, "budget-invalid", "a new budget total cannot be below what is already spent")
        if doc["provider"] == "fake":
            check_fake(doc["fake_bin"], doc["pin"])
        elif doc["provider"] == "live":
            live_args = argparse.Namespace(i_accept_spend=a.i_accept_spend, budget_tokens=lim["budget_tokens"], budget_usd=lim["budget_usd"])
            now = live_gate(live_args, doc["plan_id"], resume=True)
            if doc["pin"] is not None and now != doc["pin"]:
                raise Refused(2, "fake-binary-changed", "the harness binary changed since kickoff")
        for stale in ("control.json",):
            try:
                os.unlink(sdir / stale)
            except OSError:
                pass
        owner = doc["owner"]
        interrupted = owner is not None and doc["stop_reason"] is None
        reconciled = False
        if interrupted or doc["inflight"]:
            rc, out = cont("reconcile", "--project", sdir / "work")
            reconciled = True
        cont("drain", "end", "--project", sdir / "work")
        doc["queued"] = []
        doc["adopted"] = {"stale_owner": interrupted} if interrupted else None
        S.write(sdir, doc)
        ctl = Controller(sdir, doc)
        if a.detach:
            return go(sdir, doc, fd, True, doc["limits"].get("resource_limits"), resumed=True)
        ctl.run(resumed=True)
        return print_result(ctl.doc, {"adopted_stale_owner": interrupted, "reconciled_before_adopt": reconciled})
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def consume_next(ctl):
    rc, out = cont("resume", "consume", "--project", ctl.work, "--session", ctl.session, "--cwd", ctl.work, "--source", "resume")
    return bool(out.get("delivered"))


def load_for_read(sdir):
    if not sdir.is_dir():
        raise Refused(2, "no-state", "no state for this plan id")
    if S.marker_exists(sdir):
        raise Refused(1, "state-corrupt", "a state-corrupt marker is present")
    try:
        return S.load(sdir)
    except S.Corrupt as e:
        raise Refused(1, "state-corrupt", str(e))


def cmd_status(a):
    sdir = state_dir_for(resolve_plan_id(a.plan_id))
    Out.arm(("<state>", sdir))
    doc = load_for_read(sdir)
    Out.arm(("<repo>", doc["repo"]), ("<state>", sdir))
    alive = S.alive(doc["owner"])
    stop = doc["stop_reason"]
    state = "running" if alive else ("interrupted" if stop is None else "stopped")
    ids = doc["frozen"]["ids"]
    resource = None
    rj = sdir / "resource.json"
    if rj.is_file():
        try:
            resource = S.strict_loads(rj.read_text())
        except (OSError, ValueError):
            resource = None
    detached = None
    dj = sdir / "detached.json"
    if dj.is_file():
        try:
            detached = S.strict_loads(dj.read_text()).get("pid")
        except (OSError, ValueError):
            detached = None
    status = {
        "schema": "autopilot-status/1", "plan_id": doc["plan_id"], "state": state, "stop_reason": stop, "provider": doc["provider"],
        "rounds": len(doc["rounds"]), "spent": doc["spent"],
        "budget": {"tokens": doc["limits"]["budget_tokens"], "usd": doc["limits"]["budget_usd"], "max_rounds": doc["limits"]["max_rounds"]},
        "fix_rounds": doc["fix_rounds"], "items": {"done": doc["done"], "pending": [i for i in ids if i not in doc["done"]]},
        "queued": doc["queued"], "controller": {"alive": alive, "pid": (doc["owner"] or {}).get("pid") if alive else None},
        "branch": doc["runner_branch"], "detached_pid": detached, "resource": resource,
    }
    if a.json:
        return Out.emit(0, stop or state, **{k: v for k, v in status.items()})
    lines = [f"plan {status['plan_id']}: {state} ({stop or 'no stop reason'}), rounds {status['rounds']}, spent {doc['spent']['tokens']} tokens",
             f"items done: {', '.join(doc['done']) or 'none'}; pending: {', '.join(status['items']['pending']) or 'none'}",
             f"controller: {'running' if alive else 'not running'}"]
    sys.stdout.write(Out.redact("\n".join(lines)) + "\n")
    return 0


def cmd_pause(a):
    sdir = state_dir_for(resolve_plan_id(a.plan_id))
    doc = load_for_read(sdir)
    if not S.alive(doc["owner"]):
        return Out.emit(0, "not-running", message="no controller is running; nothing to pause")
    tmp = sdir / "control.json.tmp"
    tmp.write_text(json.dumps({"pause": True}))
    os.replace(tmp, sdir / "control.json")
    return Out.emit(0, "pause-requested", plan_id=doc["plan_id"])


def cmd_stop(a):
    sdir = state_dir_for(resolve_plan_id(a.plan_id))
    doc = load_for_read(sdir)
    owner = doc["owner"]
    if not S.alive(owner):
        return Out.emit(0, "not-running", message="no controller is running; no signal was sent")
    pid = owner["pid"]
    try:
        fd = os.pidfd_open(pid)
        try:
            if S.start_ticks(pid) != owner["start_ticks"]:
                return Out.emit(0, "not-running", message="the recorded pid is not the controller; no signal was sent")
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)
    except (AttributeError, OSError):
        if S.start_ticks(pid) == owner["start_ticks"]:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    end = time.monotonic() + 240
    while time.monotonic() < end:
        try:
            fd = acquire(sdir)
        except Refused:
            time.sleep(0.2)
            continue
        os.close(fd)
        break
    else:
        return Out.emit(1, "stop-timeout", message="the controller did not stop in time")
    doc = load_for_read(sdir)
    return Out.emit(exit_for(doc["stop_reason"]), doc["stop_reason"] or "stopped", plan_id=doc["plan_id"], stop_reason=doc["stop_reason"])


def cmd_refreeze(a):
    sdir = state_dir_for(resolve_plan_id(a.plan_id))
    pid_ = sdir.name
    Out.arm(("<state>", sdir))
    if not sdir.is_dir():
        raise Refused(2, "no-state", "no state for this plan id")
    typed_confirmation(pid_, "refreeze")
    fd = acquire(sdir)
    try:
        doc = load_for_read(sdir)
        ctl = Controller(sdir, doc)
        spec_t, tracker_t, q_t = ctl.plan_texts(ctl.tip())
        if spec_t is None:
            raise Refused(2, "plan-invalid", "SPEC.md is missing at the runner tip")
        doc["frozen"] = SP.freeze(spec_t, q_t)
        S.write(sdir, doc)  # spent, rounds and fix_rounds are never touched
        return Out.emit(0, "refrozen", plan_id=pid_)
    finally:
        os.close(fd)


def cmd_clear_corrupt(a):
    sdir = state_dir_for(resolve_plan_id(a.plan_id))
    Out.arm(("<state>", sdir))
    if not sdir.is_dir():
        raise Refused(2, "no-state", "no state for this plan id")
    typed_confirmation(sdir.name, "clear-corrupt")
    fd = acquire(sdir)
    try:
        corrupt = S.marker_exists(sdir)
        try:
            S.load(sdir)
        except S.Corrupt:
            corrupt = True
        if not corrupt:
            raise Refused(2, "not-corrupt", "the state is valid; nothing to clear")
        q = sdir / "quarantine" / str(int(time.time()))
        q.mkdir(parents=True, mode=0o700)
        for name in ("state.json", S.MARKER):
            if os.path.lexists(sdir / name):
                os.rename(sdir / name, q / name)
        return Out.emit(0, "corrupt-cleared", message="state quarantined; remove branch and work dir before a new kickoff")
    finally:
        os.close(fd)


def cmd_plan_id(a):
    return Out.emit(0, "plan-id", plan_id=plan_id_of(a.repo, a.plan))


# --- CLI ----------------------------------------------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused(2, "usage", message)


def build_parser():
    p = Parser(prog="autopilot.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    k = sub.add_parser("kickoff")
    k.add_argument("--repo", required=True)
    k.add_argument("--plan", required=True)
    k.add_argument("--provider", required=True, choices=("offline", "fake", "live"))
    k.add_argument("--model")
    k.add_argument("--fake-bin")
    k.add_argument("--budget-tokens")
    k.add_argument("--budget-usd")
    k.add_argument("--prices")
    k.add_argument("--max-rounds")
    k.add_argument("--max-hours")
    k.add_argument("--round-timeout")
    k.add_argument("--max-workers")
    k.add_argument("--reserve-tokens")
    k.add_argument("--resource-limits")
    k.add_argument("--detach", action="store_true")
    k.add_argument("--i-accept-spend", action="store_true")
    r = sub.add_parser("resume")
    r.add_argument("--plan-id")
    for name in ("budget-tokens", "budget-usd", "max-rounds", "max-hours", "max-workers"):
        r.add_argument(f"--{name}")
    r.add_argument("--detach", action="store_true")
    r.add_argument("--i-accept-spend", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--plan-id")
    s.add_argument("--json", action="store_true")
    for name in ("pause", "stop", "refreeze", "clear-corrupt"):
        sub.add_parser(name).add_argument("--plan-id")
    pi = sub.add_parser("plan-id")
    pi.add_argument("--repo", required=True)
    pi.add_argument("--plan", required=True)
    c = sub.add_parser("_controller")
    c.add_argument("--plan-id", required=True)
    c.add_argument("--resumed", action="store_true")
    c.add_argument("--fresh", action="store_true")
    return p


COMMANDS = {"kickoff": cmd_kickoff, "resume": cmd_resume, "status": cmd_status, "pause": cmd_pause, "stop": cmd_stop,
            "refreeze": cmd_refreeze, "clear-corrupt": cmd_clear_corrupt, "plan-id": cmd_plan_id, "_controller": cmd_controller}


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    except Refused as e:
        return Out.emit(2, "usage", message=e.message)
    try:
        return COMMANDS[args.command](args)
    except Refused as e:
        return Out.emit(e.code, e.reason, message=e.message)
    except S.Corrupt as e:
        return Out.emit(1, "state-corrupt", message=str(e))
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as e:
        return Out.emit(1, "internal-error", message=f"{e.__class__.__name__}: {str(e)[:160]}")


if __name__ == "__main__":
    sys.exit(main())
