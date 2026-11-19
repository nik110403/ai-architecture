#!/usr/bin/env python3
"""Conformance harness: drive any control-plane adapter through the contract.json cells, and validate assessment.json.

  conformance.py --adapter /abs/path/to/adapter --allow-adapter --fake-bin DIR [--json] [--jobs N] [--adapter-env K=V ...]
  conformance.py --validate-assessment [FILE]

The adapter is an executable named by an ABSOLUTE path only (never searched on PATH) and is run only when --allow-adapter is
given; without it nothing is spawned (exit 2). Each cell runs the adapter against a fixture plan pack and the marked fake
harness in its own temp dir. A cell passes only on independent evidence the harness collects itself (the fake harness's call
log, process liveness and the process tree it launched, a before/after snapshot of the adapter's state files, git on the fixture
repo's runner branches), never on what the adapter says about itself. The adapter runs in a session the harness starts; the harness
kills the controller in that tree (a published pid outside it fails the cell), corrupts a state file only when the launched controller
provably holds a file open beside it, edits the frozen plan, and checks that the injection took; a refusal cell passes only on the
specific refusal reason (a crash fails), read as a discriminator beside the harness-verified injection.
A cell the harness cannot decide from its own evidence is `not-run`, never `pass`.
Exit: 0 all pass, 1 any fail, 2 refused, 3 adapter unavailable (every cell not-run).
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CELLS = ["budget", "unknown-cost", "gate-human", "blocked-by", "freeze-drift", "touches", "corrupt-state", "double-kickoff", "interruption",
         "queued-drain", "status-readonly", "pause", "stop", "resume-once"]
DISPOSITIONS = {"supported", "adapter-slot", "not-ported", "incomplete"}
KINDS = {"native", "third-party", "operator-experimental"}
REQUIRED_IDS = ["native-autopilot", "paperclip", "worker-containment", "background-round-runner", "ready-frontier-report", "sandboxed-verify-runner",
                "plan-binding-runner", "pilot-instance-installer", "network-relay", "trusted-pilot", "trusted-process-adapter",
                "trusted-launch-entrypoint", "guard-installer", "sandbox-installer"]
GITENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "c", "GIT_AUTHOR_EMAIL": "c@example.invalid",
          "GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": "c@example.invalid"}


def fold(value):
    """True -> pass, False -> fail, anything else (None, 'not-run', ...) -> not-run. Never promotes a missing result to pass."""
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "not-run"


# --- assessment validation -----------------------------------------------------------------------------------------------------

def provenance_units(root):
    units = set()
    for mod in ("autopilot", "installer", "control-plane-adapters"):
        p = Path(root) / "modules" / mod / "PROVENANCE.md"
        if p.is_file():
            units |= {l[3:].strip() for l in p.read_text().splitlines() if l.startswith("## ")}
    return units


def check_evidence(ref, root, units):
    if not isinstance(ref, str) or ("/" + "home/") in ref or ref.startswith("/"):
        raise ValueError(f"evidence {ref!r} is not an in-tree reference")
    m = re.fullmatch(r"test:(modules/[A-Za-z0-9_./-]+\.py)::(test_[A-Za-z0-9_]+)", ref)
    if m:
        f = Path(root) / m.group(1)
        if not f.is_file() or f"def {m.group(2)}(" not in f.read_text():
            raise ValueError(f"evidence {ref!r} names a test that does not exist")
        return
    m = re.fullmatch(r"provenance:(.+)", ref)
    if m and m.group(1).strip() in units:
        return
    raise ValueError(f"evidence {ref!r} is neither an in-tree test id nor a PROVENANCE unit name")


def validate_assessment(doc, root=ROOT):
    if not isinstance(doc, dict) or doc.get("cells") != CELLS or not isinstance(doc.get("records"), list):
        raise ValueError("assessment must list the contract cells and its records")
    units = provenance_units(root)
    seen = set()
    for r in doc["records"]:
        need = {"id", "description", "kind", "requirements", "cells", "disposition", "evidence", "applies_to_contract"}
        if not isinstance(r, dict) or not need <= set(r) or not set(r) <= need | {"equivalent", "blocker"}:
            raise ValueError(f"record shape: {r.get('id') if isinstance(r, dict) else r!r}")
        rid = r["id"]
        if rid in seen:
            raise ValueError(f"duplicate record {rid}")
        seen.add(rid)
        if r["kind"] not in KINDS or r["disposition"] not in DISPOSITIONS:
            raise ValueError(f"{rid}: kind or disposition invalid")
        if not isinstance(r["requirements"], list) or not all(isinstance(x, str) and x for x in r["requirements"]):
            raise ValueError(f"{rid}: requirements")
        if not isinstance(r["evidence"], list) or not r["evidence"]:
            raise ValueError(f"{rid}: a disposition without evidence")
        for ref in r["evidence"]:
            check_evidence(ref, root, units)
        if not ((isinstance(r.get("equivalent"), str) and r["equivalent"].strip()) or (isinstance(r.get("blocker"), str) and r["blocker"].strip())):
            raise ValueError(f"{rid}: needs an equivalent or a blocker")
        cells = r["cells"]
        if r["applies_to_contract"]:
            if not isinstance(cells, dict) or set(cells) != set(CELLS) or any(v not in ("pass", "fail", "not-run") for v in cells.values()):
                raise ValueError(f"{rid}: cells must hold every contract cell as pass|fail|not-run")
        elif cells != {}:
            raise ValueError(f"{rid}: a record that does not apply to the contract carries no cells")
        if r["disposition"] == "supported" and any(v != "pass" for v in cells.values()):
            raise ValueError(f"{rid}: supported with a cell that is not pass")
        if r["disposition"] in ("incomplete", "not-ported") and not (r.get("blocker") or r.get("equivalent")):
            raise ValueError(f"{rid}: needs a blocker or an equivalent")
        if r["disposition"] == "incomplete" and not (isinstance(r.get("blocker"), str) and r["blocker"].strip()):
            raise ValueError(f"{rid}: incomplete needs a blocker")
    missing = [i for i in REQUIRED_IDS if i not in seen]
    if missing:
        raise ValueError(f"missing records: {', '.join(missing)}")
    return True


def render_markdown(doc):
    """ASSESSMENT.md text from assessment.json: one section per record, never hand-edited."""
    out = ["# Control-plane assessment", "",
           "Generated from `assessment.json` by `conformance.py --render-assessment`. Dispositions: `supported` (every contract cell passed), "
           "`adapter-slot` (the harness can drive it once an operator supplies it), `not-ported` (equivalent or blocker named), `incomplete` "
           "(visibly unfinished, blocker named).", ""]
    for r in doc["records"]:
        out += [f"## {r['id']}", "", f"- What: {r['description']}", f"- Kind: {r['kind']}", f"- Disposition: **{r['disposition']}**",
                f"- Requirements: {', '.join(r['requirements'])}"]
        if r["applies_to_contract"]:
            counts = {k: sum(1 for v in r["cells"].values() if v == k) for k in ("pass", "fail", "not-run")}
            out.append(f"- Contract cells: {counts['pass']} pass, {counts['fail']} fail, {counts['not-run']} not-run")
        else:
            out.append("- Contract cells: not applicable")
        if r.get("equivalent"):
            out.append(f"- Equivalent: {r['equivalent']}")
        if r.get("blocker"):
            out.append(f"- Blocker: {r['blocker']}")
        out.append("- Evidence: " + "; ".join(f"`{e}`" if e.startswith("test:") else e for e in r["evidence"]))
        out.append("")
    return "\n".join(out)


# --- cells ---------------------------------------------------------------------------------------------------------------------------

def spec_text(items):
    out = ["# Conformance spec", "", "## Items", ""]
    for it in items:
        out += [f"### {it['id']} — item {it['id']}", "", f"- What: make {it['id']}", f"- Acceptance: {it.get('acceptance', 'file exists')}",
                f"- Verify: `{it.get('verify', 'test -f out/' + it['id'].lower() + '.txt')}`", f"- Stakes: {it.get('stakes', 'HIGH')}",
                f"- Needs: {it.get('needs', 'none')}", f"- Touches: {it.get('touches', 'out/' + it['id'].lower() + '.txt')}", f"- Gate: {it.get('gate', 'auto')}"]
        if it.get("blocked"):
            out.append(f"- Blocked-by: {it['blocked']}")
        out.append("")
    return "\n".join(out)


def item(i, **kw):
    return dict(kw, id=i)


class Cell:
    def __init__(self, adapter, fake_bin, extra_env):
        self.adapter, self.extra_env = adapter, extra_env
        self.tmp = Path(tempfile.mkdtemp(prefix="aiarch-conf-"))
        self.home = self.tmp / "state"
        self.fb = self.tmp / "fake-bin"
        self.fb.mkdir()
        shutil.copy(Path(fake_bin) / "claude", self.fb / "claude")
        self.repo = self.tmp / "repo"
        self.procs = []
        self.err = ""
        self.last_doc = {}
        self.scenario = {}
        for d in ("state", "h", "t"):
            (self.tmp / d).mkdir(exist_ok=True)

    def env(self):
        e = {"PATH": f"{self.fb}:/usr/bin:/bin", "HOME": str(self.tmp / "h"), "TMPDIR": str(self.tmp / "t"), "LANG": "C.UTF-8",
             "PYTHONDONTWRITEBYTECODE": "1", **GITENV}
        e.update(self.extra_env)
        return e

    def git(self, *a):
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false", *a], check=True, capture_output=True, env=self.env())

    def pack(self, items, questions=None, scenario=None):
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        d = self.repo / "plans" / "demo"
        d.mkdir(parents=True)
        (d / "SPEC.md").write_text(spec_text(items))
        rows = ["# Tracker", "", "| Item | Lane | State | Evidence |", "|---|---|---|---|"] + [f"| {i['id']} | build | pending | |" for i in items]
        (d / "TRACKER.md").write_text("\n".join(rows) + "\n")
        if questions:
            (d / "QUESTIONS.md").write_text(questions)
        (self.repo / "README.md").write_text("conformance\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.scenario = scenario or {}
        self.flush()

    def flush(self):
        (self.fb / "scenario.json").write_text(json.dumps(self.scenario))

    def hold(self, item_id, **extra):
        gate = str(self.tmp / f"gate-{item_id}")
        self.scenario.setdefault(item_id, {})["hold"] = gate
        self.scenario[item_id].update(extra)
        self.flush()
        return gate

    def A(self, *args, timeout=300):
        r = subprocess.run([self.adapter, *map(str, args), "--state-home", str(self.home)], capture_output=True, text=True, env=self.env(), timeout=timeout,
                           stdin=subprocess.DEVNULL)
        self.err = r.stderr
        doc = None
        for line in reversed(r.stdout.splitlines()):
            if line.startswith("{"):
                try:
                    doc = json.loads(line)
                except ValueError:
                    pass
                break
        self.last_doc = doc or {}
        return r.returncode, self.last_doc

    def kick_args(self, budget="100000", extra=()):
        return ["kickoff", "--repo", self.repo, "--plan", "plans/demo", "--fake-bin", self.fb, "--model", "fake-m", "--budget-tokens", budget, *extra]

    def popen(self, *args):
        # a session of its own: every process the harness can prove it launched is a member of session p.pid
        p = subprocess.Popen([self.adapter, *map(str, args), "--state-home", str(self.home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=self.env(), stdin=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        return p

    @staticmethod
    def session_members(sid):
        """Live pids in the session `sid` (the one the harness started the adapter in), read from /proc."""
        out = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                f = (d / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if f[0] != "Z" and int(f[3]) == sid:
                out.append(int(d.name))
        return out

    def open_dirs(self, sid):
        """Directories holding a file some process of the launched session has open (its lock, state, ...)."""
        out = set()
        for pid in self.session_members(sid):
            try:
                for fd in Path(f"/proc/{pid}/fd").iterdir():
                    t = os.readlink(fd)
                    if t.startswith("/") and os.path.isfile(t):
                        out.add(os.path.dirname(t))
            except OSError:
                continue
        return out

    def log(self):
        p = self.fb / "dispatch.log"
        return [l.split()[0] for l in p.read_text().splitlines()] if p.exists() else []

    def wait_for(self, path, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path):
                return True
            time.sleep(0.05)
        raise TimeoutError(path)

    def worker_pids(self):
        out = []
        for f in (self.fb / "pids").glob("*") if (self.fb / "pids").exists() else []:
            try:
                out.append(int(f.read_text().split()[0]))
            except (OSError, ValueError, IndexError):
                pass
        return out

    @staticmethod
    def alive(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False

    # -- harness-collected evidence: git on the fixture repo and the state file the adapter keeps under --state-home
    def gitout(self, *a, cwd=None):
        r = subprocess.run(["git", "-C", str(cwd or self.repo), "-c", "core.hooksPath=/dev/null", *a], capture_output=True, text=True, env=self.env())
        return r.stdout.strip() if r.returncode == 0 else None

    def runner_refs(self):
        refs = (self.gitout("for-each-ref", "--format=%(refname)", "refs/heads") or "").split()
        return [r for r in refs if r != "refs/heads/main" and not r.startswith("refs/heads/dispatch/")]  # a worker's own branch is not an integration

    def runner_paths(self):
        """Every path any runner branch differs from main in."""
        out = set()
        for ref in self.runner_refs():
            out |= set((self.gitout("diff", "--name-only", "main", ref) or "").split())
        return out

    def integrated(self):
        """Items whose output file is on a runner branch tip (what was actually integrated)."""
        found = set()
        for ref in self.runner_refs():
            found |= {Path(n).stem.upper() for n in (self.gitout("ls-tree", "-r", "--name-only", ref, "--", "out/") or "").split()}
        return sorted(found)

    def runner_commits(self):
        return sum(int(self.gitout("rev-list", "--count", f"main..{ref}") or 0) for ref in self.runner_refs())

    def runner_worktree(self):
        cur = None
        for line in (self.gitout("worktree", "list", "--porcelain") or "").splitlines():
            if line.startswith("worktree "):
                cur = line[9:]
            elif line.startswith("branch ") and line != "branch refs/heads/main" and cur:
                return Path(cur)
        return None

    def state_holders(self):
        """Live pids, other than the harness, holding a kernel lock (flock or POSIX, from /proc/locks) on any file or directory under this
        cell's temp root. After the harness killed the launched tree there must be none: one left is a controller that escaped the kill,
        whatever its timing. /proc/locks is world-readable, so a process that made itself non-dumpable cannot hide, and no adapter-written
        evidence (published pids, worker pid files) decides who is skipped."""
        ids = set()
        for dirpath, dirs, files in os.walk(os.path.realpath(self.tmp)):
            for n in [*dirs, *files, "."]:
                try:
                    st = os.stat(os.path.join(dirpath, n))
                except OSError:
                    continue
                ids.add(f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}")
        out = []
        try:
            lines = Path("/proc/locks").read_text().splitlines()
        except OSError:
            return out
        for line in lines:
            f = line.replace("->", " ").split()
            if len(f) < 6 or f[5] not in ids or not f[4].lstrip("-").isdigit():
                continue
            pid = int(f[4])
            if pid > 1 and pid != os.getpid() and self.alive(pid) and pid not in out:
                out.append(pid)
        return out

    def state_files(self):
        found = []
        for dirpath, dirs, files in os.walk(self.home):
            dirs[:] = sorted(d for d in dirs if d not in {"work", "worktrees", "verify", "prompts"})
            if "state.json" in files:
                found.append(Path(dirpath) / "state.json")
        return found

    def live_state_file(self, sid):
        """The state.json the running controller provably uses: the only one whose directory holds a file a process of the launched session
        has open (the controller's lock). Anything the harness cannot tie to the launched tree is None, never a guess."""
        held = self.open_dirs(sid)
        live = [f for f in self.state_files() if str(f.parent) in held]
        return live[0] if len(live) == 1 else None

    def published_pids(self):
        """Controller pids any state file under --state-home publishes (adapter-written, so only ever checked, never obeyed)."""
        out = []
        for f in self.state_files():
            try:
                pid = ((json.loads(f.read_text()) or {}).get("owner") or {}).get("pid")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(pid, int) and pid > 1:
                out.append(pid)
        return out

    def snapshot(self):
        skip = {".ai-continuity", "work", "worktrees", "verify", "prompts"}
        out = {}
        for dirpath, dirs, files in os.walk(self.home):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                p = Path(dirpath) / f
                st = p.stat()
                out[str(p.relative_to(self.home))] = (st.st_size, st.st_mtime_ns)
        return out

    def finish(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)  # whatever is left of the session the harness started
            except (ProcessLookupError, PermissionError):
                pass
        for pid in self.worker_pids():
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for j in self.home.glob("**/runs/*/start.json"):
            try:
                os.killpg(json.loads(j.read_text())["pgid"], signal.SIGKILL)
            except (OSError, ValueError, KeyError, TypeError, ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def ok(c, cond, note):
    return (bool(cond), note)


def cell_budget(c):
    c.pack([item("S1"), item("S2", needs="S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}], "usage": {"input": 3000, "output": 2000, "cache_read": 0, "cache_write": 0}},
                                                       "S2": {"writes": [{"path": "out/s2.txt"}]}})
    rc, _ = c.A(*c.kick_args("4000"))
    return ok(c, c.log() == ["S1"] and "S2" not in c.integrated(), f"dispatch log {c.log()}, integrated {c.integrated()}")


def cell_unknown_cost(c):
    c.pack([item("S1"), item("S2", needs="S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}], "usage": None}, "S2": {"writes": [{"path": "out/s2.txt"}]}})
    c.A(*c.kick_args("50000"))
    return ok(c, c.log() == ["S1"] and c.integrated() == [], f"dispatch log {c.log()}, integrated {c.integrated()}")


def cell_gate_human(c):
    c.pack([item("H1", gate="human — direction", verify="true"), item("S2")], scenario={"S2": {"writes": [{"path": "out/s2.txt"}]}})
    c.A(*c.kick_args())
    return ok(c, "H1" not in c.log() and c.log() == ["S2"], f"dispatch log {c.log()}")


def cell_blocked_by(c):
    c.pack([item("B1", blocked="Q1", verify="true"), item("S2")], questions="# Q\n\n## Q1 — q\n\nStatus: open\n", scenario={"S2": {"writes": [{"path": "out/s2.txt"}]}})
    c.A(*c.kick_args())
    return ok(c, "B1" not in c.log() and c.log() == ["S2"], f"dispatch log {c.log()}")


def refusal_ok(c, rc, reasons, before):
    """The resume was refused for one of the specific `reasons`: not merely non-zero (a crash must fail), nothing dispatched."""
    reason = c.last_doc.get("reason")
    good = rc != 0 and reason in reasons and "Traceback" not in c.err and len(c.log()) == before
    return good, f"rc {rc}, reason {reason!r}, dispatches {before} -> {len(c.log())}"


def cell_freeze_drift(c):
    c.pack([item("S1"), item("S2", needs="S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}]}, "S2": {"writes": [{"path": "out/s2.txt"}]}})
    c.A(*c.kick_args(extra=["--max-rounds", "1"]))
    n = len(c.log())
    wt, ref = c.runner_worktree(), (c.runner_refs() or [None])[0]
    if n != 1 or wt is None or ref is None:
        return None, f"cannot inject: dispatches {n}, runner worktree {'found' if wt else 'not found'}"
    spec = wt / "plans" / "demo" / "SPEC.md"
    spec.write_text(spec.read_text().replace("- Acceptance: ", "- Acceptance: (edited outside a round) ", 1))
    c.gitout("add", "--", "plans/demo/SPEC.md", cwd=wt)
    c.gitout("commit", "-q", "-m", "outside edit", cwd=wt)
    if "(edited outside a round)" not in (c.gitout("show", f"{ref}:plans/demo/SPEC.md") or ""):
        return None, "the harness's drift injection did not reach the runner branch"
    rc, _ = c.A("resume", "--max-rounds", "9")
    good, note = refusal_ok(c, rc, ("verify-drift", "gate-changed"), n)
    return good and "S2" not in c.integrated(), note


def cell_touches(c):
    c.pack([item("S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}, {"path": "elsewhere/x.txt"}]}})
    c.A(*c.kick_args())
    bad = sorted(p for p in c.runner_paths() if not p.startswith("plans/"))
    return ok(c, c.log() == ["S1"] and bad == [], f"dispatch log {c.log()}, paths integrated outside the plan {bad}")


def cell_corrupt_state(c):
    gate, p = held_single(c)
    f = c.live_state_file(p.pid)
    if f is None:
        return None, "the harness cannot prove which state file the running controller reads (no single state.json beside a file the launched tree holds open)"
    good, note = killed_note(c, p)
    if not good:
        return good, note
    Path(gate).write_text("go")
    if survived(c, 1):
        return False, "work continued after the launched controller was killed"
    commits = c.runner_commits()
    before = f.read_bytes()
    f.write_bytes(before[:30])
    if f.read_bytes() == before:
        return None, "the harness's corruption did not change the state file"
    rc, _ = c.A("resume")
    good, note = refusal_ok(c, rc, ("state-corrupt",), 1)
    return good and c.runner_commits() == commits and c.integrated() == [], note


def cell_double_kickoff(c):
    c.pack([item("S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}]}})
    gate = c.hold("S1")
    barrier = threading.Barrier(2)
    procs = []

    def go():
        barrier.wait()
        procs.append(c.popen(*c.kick_args()))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    end = time.time() + 90
    while time.time() < end and not any(p.poll() is not None for p in procs):
        time.sleep(0.05)
    early = [p for p in procs if p.poll() is not None]
    c.wait_for(gate + ".started")
    Path(gate).write_text("go")
    for p in procs:
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            pass
    return ok(c, len(early) == 1 and early[0].returncode != 0 and c.log() == ["S1"], f"early exits {len(early)}, dispatch log {c.log()}")


def held_single(c, extra=None):
    c.pack([item("S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}]}})
    gate = c.hold("S1", **(extra or {}))
    p = c.popen(*c.kick_args())
    c.wait_for(gate + ".started")
    return gate, p


def kill_controller(c, p):
    """Kill the controller the harness itself launched: every non-worker process of the session it started the adapter in.
    Returns True (killed, tree empty), False (an adapter-published controller pid lies outside the launched tree) or None (cannot tie any
    published pid to the tree, so the harness cannot prove what it killed)."""
    published = [x for x in c.published_pids() if c.alive(x)]
    if not published:
        return None
    members = c.session_members(p.pid)
    if any(x not in members for x in published):
        return False
    workers = set(c.worker_pids())

    def kill(pids):
        for pid in pids:
            if pid not in workers:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    kill(members)
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        return False
    # A SIGKILLed process can sit in uninterruptible sleep (state D) for a while before it exits, so poll with a deadline instead of
    # one fixed sleep; re-kill each round so a child forked just before the first kill dies too.
    end = time.time() + 15
    while True:
        left = [x for x in c.session_members(p.pid) if x not in workers]
        if not left:
            return True
        if time.time() >= end:
            return False
        kill(left)
        time.sleep(0.1)


def survived(c, before_log):
    """With the controller dead nothing may be sent, integrated or committed on its own: a controller that escaped the kill shows up here.
    The window starts once the spared workers have exited (the released gate lets them finish), not at release: under host load an escaped
    controller can take longer than a fixed window from release just to see its worker finish. A controller that is still slower is caught
    by state_holders: it is either still alive holding its state (lock) open, or it already acted inside the window."""
    end = time.monotonic() + 60
    while time.monotonic() < end and any(c.alive(x) for x in c.worker_pids()):
        if c.state_holders():
            return True
        time.sleep(0.1)
    end = time.monotonic() + 4
    while time.monotonic() < end:
        if c.integrated() or len(c.log()) != before_log or c.state_holders():
            return True
        time.sleep(0.2)
    return bool(c.state_holders())


def killed_note(c, p):
    k = kill_controller(c, p)
    if k is None:
        return None, "the harness could not tie any published controller pid to the process tree it launched"
    if k is False:
        return False, "an adapter-published controller pid lies outside the launched process tree (or the launched tree would not die)"
    return True, ""


def cell_interruption(c):
    gate, p = held_single(c)
    good, note = killed_note(c, p)
    if not good:
        return good, note
    Path(gate).write_text("go")
    if survived(c, 1):
        return False, f"work continued after the launched controller was killed: dispatch log {c.log()}, integrated {c.integrated()}"
    rc, _ = c.A("resume")
    return ok(c, rc == 0 and c.log() == ["S1"] and c.integrated() == ["S1"], f"resume rc {rc}, dispatch log {c.log()}, integrated {c.integrated()}")


def cell_resume_once(c):
    gate, p = held_single(c)
    good, note = killed_note(c, p)
    if not good:
        return good, note
    Path(gate).write_text("go")
    if survived(c, 1):
        return False, f"work continued after the launched controller was killed: dispatch log {c.log()}, integrated {c.integrated()}"
    c.A("resume")
    commits = c.runner_commits()
    rc, _ = c.A("resume")
    return ok(c, rc == 0 and c.log() == ["S1"] and c.integrated() == ["S1"] and c.runner_commits() == commits,
              f"second resume rc {rc}, dispatch log {c.log()}, runner commits {commits} -> {c.runner_commits()}")


def cell_queued_drain(c):
    items = [item("P1"), item("P2"), item("P3")]
    c.pack(items, scenario={k: {"writes": [{"path": f"out/{k.lower()}.txt"}]} for k in ("P1", "P2", "P3")})
    g1, g2 = c.hold("P1", spawn_child=True), c.hold("P2", spawn_child=True)
    p = c.popen(*c.kick_args(extra=["--max-workers", "2"]))
    c.wait_for(g1 + ".started")
    c.wait_for(g2 + ".started")
    c.A("stop")
    try:
        p.wait(timeout=240)
    except subprocess.TimeoutExpired:
        return ok(c, False, "the controller did not stop")
    leftover = [x for x in c.worker_pids() if c.alive(x)]
    queued_untouched = "P3" not in c.log()
    Path(g1).write_text("go")
    Path(g2).write_text("go")
    rc, _ = c.A("resume", "--max-workers", "2")
    return ok(c, not leftover and queued_untouched and rc == 0 and c.log().count("P3") == 1 and c.integrated() == ["P1", "P2", "P3"],
              f"orphans {leftover}, P3 before resume {not queued_untouched}, rc {rc}, P3 total {c.log().count('P3')}, integrated {c.integrated()}")


def cell_status_readonly(c):
    gate, p = held_single(c)
    time.sleep(1.5)
    before = c.snapshot()
    rc, st = c.A("status")
    after = c.snapshot()
    Path(gate).write_text("go")
    p.wait(timeout=120)
    return ok(c, rc == 0 and before == after, f"state files changed: {sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}")


def cell_pause(c):
    c.pack([item("S1"), item("S2", needs="S1")], scenario={"S1": {"writes": [{"path": "out/s1.txt"}]}, "S2": {"writes": [{"path": "out/s2.txt"}]}})
    gate = c.hold("S1")
    p = c.popen(*c.kick_args())
    c.wait_for(gate + ".started")
    c.A("pause")
    Path(gate).write_text("go")
    p.wait(timeout=120)
    return ok(c, c.log() == ["S1"] and c.integrated() == ["S1"], f"dispatch log {c.log()}, integrated {c.integrated()}")


def dispatch_leftovers(c):
    """Dispatch branches or worktrees still registered on the fixture repo: a drained, cancelled stop cleans its workers up, a kill does not."""
    refs = (c.gitout("for-each-ref", "--format=%(refname)", "refs/heads/dispatch") or "").split()
    trees = [l for l in (c.gitout("worktree", "list", "--porcelain") or "").splitlines() if l.startswith("branch ") and "/dispatch/" in l]
    return refs + trees


def cell_stop(c):
    gate, p = held_single(c, {"spawn_child": True})
    sent = len(c.log())
    c.A("stop")
    try:
        p.wait(timeout=240)
    except subprocess.TimeoutExpired:
        return ok(c, False, "the controller did not stop")
    leftover = [x for x in c.worker_pids() if c.alive(x)]
    members = [x for x in c.session_members(p.pid) if x not in c.worker_pids()]
    Path(gate).write_text("go")
    time.sleep(1)
    left = dispatch_leftovers(c)
    return ok(c, not leftover and not members and not left and c.integrated() == [] and len(c.log()) == sent,
              f"surviving workers {leftover}, surviving launched processes {members}, dispatch leftovers {left}, integrated {c.integrated()}, dispatches {sent} -> {len(c.log())}")


CELL_FUNCS = {"budget": cell_budget, "unknown-cost": cell_unknown_cost, "gate-human": cell_gate_human, "blocked-by": cell_blocked_by,
              "freeze-drift": cell_freeze_drift, "touches": cell_touches, "corrupt-state": cell_corrupt_state, "double-kickoff": cell_double_kickoff,
              "interruption": cell_interruption, "queued-drain": cell_queued_drain, "status-readonly": cell_status_readonly, "pause": cell_pause,
              "stop": cell_stop, "resume-once": cell_resume_once}


def run_cell(name, adapter, fake_bin, extra_env):
    c = Cell(adapter, fake_bin, extra_env)
    try:
        passed, note = CELL_FUNCS[name](c)  # passed: True|False, or None when the harness's own evidence cannot decide -> not-run
        return name, {"result": fold(passed), "note": note}
    except (OSError, TimeoutError, subprocess.TimeoutExpired, KeyError, ValueError) as error:
        return name, {"result": "fail", "note": f"harness error: {error.__class__.__name__}"}
    finally:
        c.finish()


def emit(doc, as_json, code):
    if as_json:
        print(json.dumps(doc, sort_keys=True))
    else:
        print(f"conformance: {doc.get('reason')}")
        for k, v in (doc.get("cells") or {}).items():
            print(f"  {k}: {v['result']}  {v.get('note', '')}")
    return code


def main(argv=None):
    p = argparse.ArgumentParser(prog="conformance.py", description=__doc__.splitlines()[0])
    p.add_argument("--adapter")
    p.add_argument("--allow-adapter", action="store_true")
    p.add_argument("--fake-bin")
    p.add_argument("--json", action="store_true")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--adapter-env", action="append", default=[])
    p.add_argument("--validate-assessment", nargs="?", const=str(HERE / "assessment.json"))
    p.add_argument("--render-assessment", nargs="?", const=str(HERE / "assessment.json"))
    try:
        a = p.parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    if a.render_assessment:
        print(render_markdown(json.loads(Path(a.render_assessment).read_text())))
        return 0
    if a.validate_assessment:
        try:
            validate_assessment(json.loads(Path(a.validate_assessment).read_text()))
        except (OSError, ValueError) as error:
            return emit({"reason": "assessment-invalid", "message": str(error)}, True, 1)
        return emit({"reason": "assessment-valid"}, True, 0)
    not_run = {c: {"result": "not-run", "note": ""} for c in CELLS}
    if not a.adapter:
        return emit({"reason": "usage", "message": "--adapter is required"}, True, 2)
    if not a.allow_adapter:
        return emit({"reason": "adapter-not-allowed", "message": "an adapter is only run with --allow-adapter"}, a.json, 2)
    if not os.path.isabs(a.adapter):
        return emit({"reason": "adapter-not-absolute", "message": "the adapter must be given as an absolute path; it is never searched on PATH"}, a.json, 2)
    if not a.fake_bin or not (Path(a.fake_bin) / "claude").is_file():
        return emit({"reason": "usage", "message": "--fake-bin DIR with the marked fake harness is required"}, a.json, 2)
    if not (os.path.isfile(a.adapter) and os.access(a.adapter, os.X_OK)):
        return emit({"reason": "adapter-unavailable", "cells": not_run}, a.json, 3)
    extra = dict(kv.split("=", 1) for kv in a.adapter_env if "=" in kv)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        for name, res in pool.map(lambda n: run_cell(n, a.adapter, a.fake_bin, extra), CELLS):
            results[name] = res
    ordered = {c: results[c] for c in CELLS}
    code = 0 if all(v["result"] == "pass" for v in ordered.values()) else 1
    return emit({"reason": "conformance", "adapter": os.path.basename(a.adapter), "cells": ordered}, a.json, code)


if __name__ == "__main__":
    sys.exit(main())
