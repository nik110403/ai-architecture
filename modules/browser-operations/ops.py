#!/usr/bin/env python3
"""Browser lifecycle on top of modules/browser: a real local Chrome backend (optional), opt-in profiles, takeover
handover/resume, a no-resubmit watchdog, scoped pruning with a preview, a local cost status and desktop/extension
adapter declarations. Same protocol as browser.py (survey, proposal, review, approval, act, receipt); 127.0.0.1 only.

Exit codes are browser.py's (0 ok, 1 receipt invalid, 2 refused, 3 changed or takeover, 4 approval invalid, used or
invalidated) plus 5 = outcome_unknown: the approved request was released to Chrome and something failed after that, so
the approval stays spent and nothing is ever re-sent. Stdlib only.
"""
import argparse
import contextlib
import functools
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "browser"))
import backends  # noqa: E402
import browser  # noqa: E402
import chrome  # noqa: E402

Fail = browser.Fail
SHA = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
TOKEN = re.compile(r"[A-Za-z0-9]{1,32}")
MARKER = ".aiarch-profile"
PROFILE_FLAG, OUT_FLAG = "--profile-dir", "--ops-out"
ADAPTERS = [
    {"kind": "extension", "state": "unsupported",
     "gap": "Driving the user's own signed-in browser through an installed extension needs an extension bundle and a "
            "native-messaging bridge; neither ships here and nothing is installed or driven.",
     "license": "Any third-party extension carries its own license; none is vendored or redistributed by this module."},
    {"kind": "desktop", "state": "unsupported",
     "gap": "Desktop control (screen capture, input injection) needs an OS-level permission model and a driver; "
            "neither is implemented and nothing is installed or driven.",
     "license": "Any third-party desktop-control tool carries its own license; none is vendored or redistributed here."},
]
ADAPTER_KINDS = {a["kind"] for a in ADAPTERS}


# --- state and profiles ------------------------------------------------------------------------------------------

def used_root_ro():
    """The per-user single-use root as browser.state_used_root() computes it, without creating anything."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "ai-architecture" / "browser" / "used"


def marker_state(sha):
    """None (never claimed), 'claimed' (claimed, no receipt: the outcome is unknown), or 'recorded'."""
    try:
        st = (used_root_ro() / sha).lstat()
    except OSError:
        return None
    return "claimed" if st.st_size == 0 else "recorded"


def _within(p, root):
    return p == root or root in p.parents


def _default_profile_roots():
    home = Path(os.path.expanduser("~"))
    roots = [home / ".config" / n for n in ("google-chrome", "google-chrome-beta", "google-chrome-unstable", "chromium")]
    cfg = home / ".config"
    if cfg.is_dir():
        roots += [d for d in cfg.iterdir() if d.name.startswith(("google-chrome", "chromium"))]
    roots += [home / "snap" / "chromium", home / ".var" / "app" / "com.google.Chrome",
              home / ".var" / "app" / "org.chromium.Chromium",
              home / "Library" / "Application Support" / "Google" / "Chrome",
              home / "Library" / "Application Support" / "Chromium"]
    return roots


def prepare_profile(raw, out):
    """Validate an opt-in --profile-dir (Fail(2) on any doubt), create it 0700 with the module's marker, return it."""
    p = Path(raw)
    if not p.is_absolute() or str(p) != os.path.abspath(p) or os.path.realpath(p) != str(p):
        raise Fail(2, "--profile-dir must be an absolute path with no symlinked component")
    for root in (Path(os.path.realpath(out)), Path(out), used_root_ro(), *_default_profile_roots()):
        if _within(p, root) or _within(root, p):
            raise Fail(2, "--profile-dir overlaps the output directory, the state root or a default browser profile")
    if p.exists():
        st = p.lstat()
        if not p.is_dir() or st.st_uid != os.getuid():
            raise Fail(2, "--profile-dir is not a directory owned by you")
        if any(p.iterdir()) and not (p / MARKER).is_file():
            raise Fail(2, "--profile-dir is not empty and was not created by this module")
        lock = p / "SingletonLock"
        if lock.is_symlink() or lock.exists():
            try:
                pid = int(os.readlink(lock).rsplit("-", 1)[1])
            except (OSError, ValueError, IndexError):
                pid = 0
            if pid and chrome.starttime(pid) is not None:
                raise Fail(2, "--profile-dir is in use by a running browser")
    else:
        p.mkdir(mode=0o700, parents=True)
    p.chmod(0o700)
    if not (p / MARKER).exists():
        os.close(os.open(p / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    return p


# --- backend registration ----------------------------------------------------------------------------------------

@contextlib.contextmanager
def registered(add):
    """Register backends for one command only; BACKENDS is restored in finally. Nothing is loaded from the
    environment, the command line or a plugin path: the caller passes classes."""
    if ADAPTER_KINDS & set(add):
        raise Fail(2, "extension and desktop are adapter declarations, not backends")
    saved = dict(backends.BACKENDS)
    try:
        backends.BACKENDS.update(add)
        yield
    finally:
        backends.BACKENDS.clear()
        backends.BACKENDS.update(saved)


def factories(extra, run_dir, profile):
    base = {"chrome": chrome.ChromeBackend}
    base.update(extra or {})
    return {n: functools.partial(f, run_dir=run_dir, profile_dir=profile) for n, f in base.items()}


# --- run ---------------------------------------------------------------------------------------------------------

def take(rest, flag):
    """Remove `flag VALUE` from rest (only before --reviewer-cmd, which swallows the remainder)."""
    end = rest.index("--reviewer-cmd") if "--reviewer-cmd" in rest else len(rest)
    for i in range(end):
        if rest[i] == flag:
            if i + 1 >= end:
                raise Fail(2, f"{flag} needs a value")
            val = rest[i + 1]
            return val, rest[:i] + rest[i + 2:]
        if rest[i].startswith(flag + "="):
            return rest[i].split("=", 1)[1], rest[:i] + rest[i + 1:]
    return None, rest


def value_of(rest, flag):
    end = rest.index("--reviewer-cmd") if "--reviewer-cmd" in rest else len(rest)
    for i in range(end - 1):
        if rest[i] == flag:
            return rest[i + 1]
    return None


def write_line(path, doc):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(doc, sort_keys=True) + "\n")


def stop_lines(path):
    try:
        return Path(path).read_text().splitlines()
    except OSError:
        return []


class Term(Exception):
    pass


@contextlib.contextmanager
def term_handlers():
    """SIGTERM/SIGHUP raise SystemExit so every finally runs and Chrome is cleaned up (F6)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    def handler(signum, frame):
        raise SystemExit(128 + signum)
    old = {s: signal.signal(s, handler) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def new_run_dir(out):
    d = Path(out) / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(2)}"
    browser.private_dir(d.parent)
    d.mkdir(mode=0o700)
    return d


def cmd_run(rest, extra):
    ops_out, rest = take(rest, OUT_FLAG)
    profile, rest = take(rest, PROFILE_FLAG)
    if not rest or rest[0] not in browser.COMMANDS or rest[0] == "status":
        raise Fail(2, "run needs a browser.py subcommand (survey, propose, review, approve, act, verify-receipt)")
    sub = rest[0]
    backend = value_of(rest, "--backend") or "fixture"
    if backend in ADAPTER_KINDS:
        raise Fail(2, f"{backend} is an adapter declaration and is unsupported; see `adapters`")
    target = value_of(rest, "--out")
    out = Path(ops_out) if ops_out else (Path(target).parent if target else None)
    if out is None:
        raise Fail(2, "run needs --ops-out DIR (or a subcommand --out FILE)")
    out = browser.private_dir(out)
    prof = prepare_profile(profile, out) if profile else None  # validated before anything starts
    run = new_run_dir(out)
    stops = (Path(target).parent / "stops.jsonl") if target else None
    seen = len(stop_lines(stops)) if stops else 0
    chrome.BROWSER_SECONDS[0] = 0.0
    t0, rc = time.monotonic(), -1
    try:
        with term_handlers(), registered(factories(extra, run, prof)):
            if sub == "act":
                app = browser.load(value_of(rest, "--approval") or "", 4)
                sha = app.get("approval_sha256")
                if isinstance(sha, str) and SHA.fullmatch(sha) and (out / "invalidated" / sha).exists():
                    browser.stop(Path(target).parent, "approval_invalidated", sha, "")
                    raise Fail(4, "approval_invalidated: the page changed during handover")
            try:
                rc = browser.main(rest)
            except chrome.OutcomeUnknown as e:
                sha = None
                with contextlib.suppress(Fail, OSError):
                    sha = browser.load(value_of(rest, "--approval") or "", 4).get("approval_sha256")
                browser.stop(Path(target).parent, "outcome_unknown", sha, str(e)[:200])
                print(f"stopped: outcome_unknown: {e}; the approval stays spent and nothing is re-sent", file=sys.stderr)
                rc = 5
    except Fail as e:
        print(f"{'refused' if e.code == 2 else 'stopped'}: {e.msg}", file=sys.stderr)
        rc = e.code
    finally:
        chrome.close_all()
        reason = None
        new = stop_lines(stops)[seen:] if stops else []
        if new:
            reason = json.loads(new[-1]).get("reason")
        write_line(run / "events.jsonl", {"cmd": sub, "backend": backend, "rc": rc, "stop_reason": reason,
                                           "seconds": round(time.monotonic() - t0, 3),
                                           "browser_seconds": round(chrome.BROWSER_SECONDS[0], 3)})
    return rc


# --- handover / resume -------------------------------------------------------------------------------------------

def load_approval(path):
    app = browser.load(path, 4)
    if set(app) != browser.APPROVAL_KEYS or not browser.check_hash(app, "approval_sha256"):
        raise Fail(4, "approval hash mismatch")
    return app


def refuse_spent(out, sha):
    if (Path(out) / "invalidated" / sha).exists():
        raise Fail(4, "approval_invalidated")
    if marker_state(sha):
        raise Fail(4, "approval_used (or outcome unknown): it is never re-acted")


def cmd_handover(a):
    out = Path(a.out)
    app = load_approval(a.approval)
    sha = app["approval_sha256"]
    refuse_spent(out, sha)
    if not any(json.loads(x).get("reason") == "takeover" and json.loads(x).get("approval_sha256") == sha
               for x in stop_lines(out / "stops.jsonl")):
        raise Fail(2, "no takeover stop is recorded for this approval; handover applies after act stopped with takeover")
    rec = {"approval_sha256": sha, "url": app["url"], "pin": {"revision": app["revision"], "dom_sha256": app["dom_sha256"]},
           "status": "handover"}
    browser.write_atomic(out / "handover" / f"{sha}.json", json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"handover": sha, "next": "log in yourself in a browser you control; the module never reads or "
                      "stores credentials. Then run: ops.py resume --out OUT --approval APPROVAL"}))
    return 0


def cmd_resume(a, extra):
    out = Path(a.out)
    app = load_approval(a.approval)
    sha = app["approval_sha256"]
    refuse_spent(out, sha)
    rec_path = out / "handover" / f"{sha}.json"
    if not rec_path.is_file():
        raise Fail(2, "no handover is recorded for this approval")
    prof = prepare_profile(a.profile_dir, out) if a.profile_dir else None
    run = new_run_dir(out)
    try:
        with term_handlers(), registered(factories(extra, run, prof)):
            b = browser.get_backend(a.backend)
            if b.probe()["state"] != "available":
                raise Fail(2, f"backend {a.backend} unavailable")
            now = b.survey(app["url"])
    finally:
        chrome.close_all()
    if now["takeover"]:
        print(json.dumps({"resumed": False, "reason": "takeover"}))
        return 3
    if (now["revision"], now["dom_sha256"]) == (app["revision"], app["dom_sha256"]):
        rec = json.loads(rec_path.read_text())
        rec["status"] = "resumed"
        browser.write_atomic(rec_path, json.dumps(rec, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"resumed": True, "approval_sha256": sha}))
        return 0
    browser.write_atomic(out / "invalidated" / sha, json.dumps({"reason": "page differs after handover"}) + "\n")
    with contextlib.suppress(FileExistsError):  # the bare protocol refuses it as well
        fd = os.open(browser.state_used_root() / sha, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"invalidated": True}) + "\n")
    print(json.dumps({"resumed": False, "reason": "invalidated"}))
    return 3


# --- watchdog / prune / cost -------------------------------------------------------------------------------------

def real_dirs(parent):
    parent = Path(parent)
    if parent.is_symlink() or not parent.is_dir():
        return []
    return [d for d in sorted(parent.iterdir()) if not d.is_symlink() and d.is_dir() and RUN_ID.fullmatch(d.name)]


def pid_records(out):
    for d in real_dirs(Path(out) / "runs"):
        f = d / "browser.pid.json"
        if f.is_file() and not f.is_symlink():
            try:
                rec = json.loads(f.read_text())
            except (ValueError, OSError):
                continue
            if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and isinstance(rec.get("token"), str) \
                    and TOKEN.fullmatch(rec["token"]):
                yield d, rec


def matches(rec):
    """Kill-scope rule: pid, start time and the token in the command line must all match the record."""
    pid = rec["pid"]
    return (rec.get("starttime") is not None and chrome.starttime(pid) == rec["starttime"]
            and f"{chrome.TOKEN_FLAG}{rec['token']}" in chrome.cmdline(pid))


def gone(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def kill_record(rec):
    pid = rec["pid"]
    group = os.getpgid(pid) == pid
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            (os.killpg if group else os.kill)(pid, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if gone(pid):
                return True
            time.sleep(0.02)
    return gone(pid)


def orphan_tokens(out):
    tmp = Path(tempfile.gettempdir())
    found = []
    for d, rec in pid_records(out):
        if rec.get("temp_profile") and (tmp / f"aiarch-chrome-{rec['token']}").is_dir() and not matches(rec):
            found.append(rec["token"])
    return sorted(set(found))


def unknown_outcomes(out):
    """Approvals whose single-use marker is claimed but empty: act died (or Chrome failed) after the POST was released."""
    shas = set()
    for f in list(Path(out).glob("*.json")) + list((Path(out) / "artifacts").glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(doc, dict) and doc.get("schema") == "browser-approval/1" and isinstance(doc.get("approval_sha256"), str) \
                and SHA.fullmatch(doc["approval_sha256"]):
            shas.add(doc["approval_sha256"])
    return sorted(s for s in shas if marker_state(s) == "claimed")


def cmd_watchdog(a):
    out = Path(a.out)
    killed = []
    for d, rec in pid_records(out):
        if not rec.get("closed") and matches(rec) and time.time() - float(rec.get("started", 0)) >= a.timeout:
            if kill_record(rec):
                killed.append(rec["token"])
    res = {"killed": killed, "orphaned_profiles": orphan_tokens(out), "outcome_unknown": unknown_outcomes(out)}
    print(json.dumps(res, sort_keys=True) if a.json else "\n".join(
        [f"killed hung browser: {t}" for t in killed] + [f"orphaned temp profile: {t}" for t in res["orphaned_profiles"]]
        + [f"outcome_unknown (never re-sent): {s}" for s in res["outcome_unknown"]] + ["watchdog done"]))
    return 0


def cmd_prune(a):
    out = Path(a.out)
    cutoff = time.time() - a.days * 86400
    runs = out / "runs"
    live = {d.name for d, rec in pid_records(out) if not rec.get("closed") and matches(rec)}
    old = [d for d in real_dirs(runs) if d.lstat().st_mtime < cutoff and d.name not in live]
    names = [f"runs/{d.name}" for d in old]
    deleted = []
    if a.apply:
        for d in old:
            if d.parent.resolve() == runs.resolve() and not d.is_symlink():
                shutil.rmtree(d)
                deleted.append(f"runs/{d.name}")
    res = {"applied": a.apply, "would_delete": names, "deleted": deleted, "orphaned_profiles": orphan_tokens(out)}
    print(json.dumps(res, sort_keys=True) if a.json else "\n".join(
        [("deleted " if a.apply else "would delete ") + n for n in names] + [f"orphaned temp profile: {t}" for t in res["orphaned_profiles"]]
        + ([] if a.apply else ["preview only: pass --apply to delete"])))
    return 0


def cmd_cost(a):
    res = {"surveys": 0, "acts": 0, "receipts": 0, "stops_by_reason": {}, "browser_seconds": 0.0}
    for d in real_dirs(Path(a.out) / "runs"):
        f = d / "events.jsonl"
        if not f.is_file() or f.is_symlink():
            continue
        for line in f.read_text().splitlines():
            ev = json.loads(line)
            res["surveys"] += ev["cmd"] == "survey"
            res["acts"] += ev["cmd"] == "act"
            res["receipts"] += ev["cmd"] == "act" and ev["rc"] == 0
            if ev.get("stop_reason"):
                res["stops_by_reason"][ev["stop_reason"]] = res["stops_by_reason"].get(ev["stop_reason"], 0) + 1
            res["browser_seconds"] += ev.get("browser_seconds", 0)
    res["browser_seconds"] = round(res["browser_seconds"], 3)
    if a.json:
        print(json.dumps(res, sort_keys=True))
    else:
        print(f"surveys {res['surveys']}, acts {res['acts']}, receipts {res['receipts']}, "
              f"stops {res['stops_by_reason']}, browser seconds {res['browser_seconds']}")
    return 0


def cmd_adapters(a):
    if a.json:
        print(json.dumps({"adapters": ADAPTERS}, sort_keys=True))
    else:
        for x in ADAPTERS:
            print(f"{x['kind']}: {x['state']}\n  gap: {x['gap']}\n  license: {x['license']}")
    return 0


def cmd_status(a):
    st = {"fixture": {"state": "available", "setup": ""}, "chrome": chrome.ChromeBackend().probe()}
    if a.json:
        print(json.dumps({"backends": st}, sort_keys=True))
    else:
        for n, s in st.items():
            print(f"{n}: {s['state']}" + (f" ({s['setup']})" if s["setup"] else ""))
    return 0


# --- entry -------------------------------------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="ops.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("run", help="pass a browser.py subcommand through with lifecycle bookkeeping")
    s.add_argument(OUT_FLAG, help="bookkeeping directory (default: parent of the subcommand --out)")
    s.add_argument(PROFILE_FLAG, help="opt-in persistent profile directory (validated; created 0700)")
    s.add_argument("rest", nargs=argparse.REMAINDER)
    for name in ("handover", "resume"):
        s = sub.add_parser(name)
        s.add_argument("--out", required=True)
        s.add_argument("--approval", required=True)
        if name == "resume":
            s.add_argument("--backend", choices=["fixture", "chrome"], default="fixture")
            s.add_argument(PROFILE_FLAG)
    s = sub.add_parser("watchdog")
    s.add_argument("--out", required=True)
    s.add_argument("--timeout", type=float, default=120.0)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("prune")
    s.add_argument("--out", required=True)
    s.add_argument("--days", type=float, default=30.0)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("cost")
    s.add_argument("--out", required=True)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("adapters")
    s.add_argument("--json", action="store_true")
    return p


def main(argv=None, _backends=None):
    """_backends is for tests only (classes injected in-process); no CLI flag, environment variable or file reaches it."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "run":
            return cmd_run(argv[1:], _backends)
        try:
            a = parser().parse_args(argv)
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 2
        if a.cmd == "resume":
            return cmd_resume(a, _backends)
        return {"status": cmd_status, "handover": cmd_handover, "watchdog": cmd_watchdog, "prune": cmd_prune,
                "cost": cmd_cost, "adapters": cmd_adapters}[a.cmd](a)
    except Fail as e:
        print(f"{'refused' if e.code == 2 else 'stopped'}: {e.msg}", file=sys.stderr)
        return e.code
    except (backends.Refused, backends.Unavailable) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
