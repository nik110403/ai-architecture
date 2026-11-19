"""Acceptance tests for modules/browser-operations (A1..A14 and the tribune amendments F1..F17).
Loopback server only. Real Chrome tests skip with a printed reason when Chrome is missing or cannot start, and
fail under AIARCH_REQUIRE_BROWSER=1."""
import contextlib
import functools
import getpass
import io
import json
import os
import signal
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent
ROOT = MODULE.parent.parent
sys.dont_write_bytecode = True
for p in (MODULE, ROOT / "modules" / "browser", ROOT / "modules" / "browser" / "tests", ROOT / "modules" / "contracts",
          ROOT / "scripts"):
    sys.path.insert(0, str(p))

import backends  # noqa: E402
import browser  # noqa: E402
import chrome  # noqa: E402
import contracts  # noqa: E402
import ops  # noqa: E402
import privacy_scan  # noqa: E402
from test_browser import FACTS, form_html  # noqa: E402

EXAMPLE = ROOT / "examples" / "browser-local"
REVIEWER = EXAMPLE / "reviewer.py"
ECHO = HERE / "fixtures" / "echo_reviewer.py"
REQUIRE = os.environ.get("AIARCH_REQUIRE_BROWSER") == "1"
_LAUNCH = []


def launchable():
    """(ok, reason). Found-but-unlaunchable is a skip with the stderr reason, a failure under REQUIRE (F11)."""
    if not _LAUNCH:
        if not chrome.find_chrome():
            _LAUNCH.append((False, "no Chrome on PATH and AIARCH_CHROME unset"))
        else:
            b = chrome.Browser()
            try:
                b.start()
                _LAUNCH.append((True, ""))
            except backends.Unavailable as e:
                _LAUNCH.append((False, f"Chrome found but cannot start: {e}"))
            finally:
                b.close()
    return _LAUNCH[0]


def real_chrome(fn):
    @functools.wraps(fn)
    def wrapper(self):
        ok, reason = launchable()
        if not ok:
            print(f"SKIP {fn.__name__}: {reason}", file=sys.stderr)
            if REQUIRE:
                self.fail(f"AIARCH_REQUIRE_BROWSER=1 but {reason}")
            self.skipTest(reason)
        return fn(self)
    return wrapper


class Site:
    """Loopback server with mutable pages; records GETs and POSTs (path, values, raw body, User-Agent)."""

    def __init__(self):
        self.pages = {"/form": {"body": form_html()},
                      "/login": {"body": "<html><body><input type='password' name='pw'></body></html>"}}
        self.posts, self.gets, self.lock = [], [], threading.Lock()
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with site.lock:
                    site.gets.append(self.path)
                    page = dict(site.pages.get(self.path.split("?")[0]) or {})
                if not page:
                    self.send_error(404)
                    return
                self.send_response(page.get("status", 200))
                if "location" in page:
                    self.send_header("Location", page["location"])
                body = page.get("body", "").encode()
                if "ctype" in page:
                    self.send_header("Content-Type", page["ctype"])
                if "headers" in page:
                    for k, v in page["headers"].items():
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                with site.lock:
                    site.posts.append({"path": self.path, "raw": raw, "ua": self.headers.get("User-Agent", ""),
                                       "values": {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}})
                body = b"<html><body>received</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def url(self, path="/form"):
        return f"http://127.0.0.1:{self.port}{path}"

    def set(self, path, **page):
        with self.lock:
            self.pages[path] = page

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def read(p):
    return json.loads(Path(p).read_text())


def audit_run(argv, env, out=None):
    """Run ops.main(argv) in a fresh interpreter under an audit hook (F14). Returns (rc, events, stdout)."""
    code = f"""
import sys, json, os
sys.path.insert(0, {str(MODULE)!r})
import ops
OUT = {str(out) if out else None!r}
events = []
def hook(ev, args):
    if ev in ("socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.exec",
              "os.posix_spawn", "os.fork", "os.forkpty"):
        events.append([ev, str(args)[:80]])
    elif ev == "open" and OUT and isinstance(args[0], (str, bytes)):
        p = os.path.realpath(args[0] if isinstance(args[0], str) else args[0].decode())
        if not (p == OUT or p.startswith(OUT + os.sep)):
            events.append(["open", p])
sys.addaudithook(hook)
rc = ops.main({argv!r})
sys.stdout.write("\\n" + json.dumps({{"rc": rc, "events": events}}) + "\\n")
"""
    r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env, timeout=60)
    last = [x for x in r.stdout.splitlines() if x.startswith('{"rc"')]
    assert last, (r.stdout, r.stderr)
    res = json.loads(last[-1])
    return res["rc"], res["events"], r.stdout


def pump_for(backend, seconds):
    """Keep the CDP event loop (the Fetch gate) running for a while, as the real pump does during a POST."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with contextlib.suppress(TimeoutError):
            backend.br.pump(lambda: time.monotonic() >= end, 0.2)


def procs_with(needle):
    found = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit() and d.name != str(os.getpid()):
            try:
                if needle in (d / "cmdline").read_bytes().decode("utf-8", "replace"):
                    found.append(int(d.name))
            except OSError:
                pass
    return found


def tree_digest(root):
    out = []
    for p in sorted(Path(root).rglob("*")):
        st = p.lstat()
        out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns, p.read_bytes() if p.is_file() else b""))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "t").mkdir()
        (self.tmp / "home").mkdir()
        self.state = self.tmp / "state"
        os.environ.update(XDG_STATE_HOME=str(self.state), HOME=str(self.tmp / "home"), TMPDIR=str(self.tmp / "t"))
        os.environ.pop("AIARCH_CHROME", None)
        tempfile.tempdir = str(self.tmp / "t")
        self.site = Site()
        self.orig = dict(backends.BACKENDS)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.facts = self.tmp / "facts.json"
        self.facts.write_text(json.dumps(FACTS))

    def tearDown(self):
        self.site.close()
        chrome.close_all()
        leaked = procs_with(str(self.tmp / "t" / "aiarch-chrome-"))
        left = list((self.tmp / "t").glob("aiarch-chrome-*"))
        os.environ.clear()
        os.environ.update(self._env)
        tempfile.tempdir = None
        self.assertEqual(backends.BACKENDS, self.orig)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(leaked, [], "a Chrome process of this test survived")
        self.assertEqual(left, [], "a temp profile of this test survived")

    # -- helpers --
    def cli(self, *argv, extra=None):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            code = ops.main([str(a) for a in argv], _backends=extra)
        return code, o.getvalue(), e.getvalue()

    def run_(self, *argv, extra=None, out=None):
        return self.cli("run", "--ops-out", out or self.out, *argv, extra=extra)

    def build(self, backend="fixture", path="/form", extra=None, reviewer=None, out=None):
        """survey -> propose -> review -> approve through ops run; every exit must be 0."""
        d = Path(out or self.out)
        d.mkdir(exist_ok=True)
        r = reviewer or [sys.executable, str(REVIEWER)]
        steps = [["survey", "--mode", "form", "--url", self.site.url(path), "--out", d / "survey.json", "--backend", backend],
                 ["propose", "--survey", d / "survey.json", "--facts", self.facts, "--form", "card", "--out", d / "proposal.json"],
                 ["review", "--survey", d / "survey.json", "--proposal", d / "proposal.json", "--facts", self.facts,
                  "--out", d / "review.json", "--reviewer-cmd", *r]]
        for argv in steps:
            code, o, e = self.run_(*argv, extra=extra, out=d)
            self.assertEqual(code, 0, (argv[0], o, e))
        sha = read(d / "proposal.json")["proposal_sha256"]
        code, o, e = self.run_("approve", "--mode", "form", "--review", d / "review.json", "--user-confirm", sha,
                               "--out", d / "approval.json", extra=extra, out=d)
        self.assertEqual(code, 0, (o, e))
        return d / "approval.json"

    def act(self, approval, backend="fixture", extra=None, name="receipt.json", out=None):
        d = Path(out or self.out)
        return self.run_("act", "--approval", approval, "--out", d / name, "--backend", backend, extra=extra, out=d)

    def stops(self, out=None):
        f = Path(out or self.out) / "stops.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def marker(self, approval):
        return Path(os.environ["XDG_STATE_HOME"]) / "ai-architecture" / "browser" / "used" / read(approval)["approval_sha256"]

    def env(self):
        return dict(os.environ)


class Crash(backends.FixtureBackend):
    """Test-only: the POST lands, then the process dies before the receipt is written (A4)."""

    def __init__(self, **kw):
        super().__init__()

    def submit(self, *a):
        super().submit(*a)
        raise SystemExit(9)


class TestFixtureLifecycle(Base):
    def test_a4_crash_after_post_watchdog_never_resubmits(self):
        approval = self.build()
        with self.assertRaises(SystemExit):
            self.act(approval, extra={"fixture": Crash})
        self.assertEqual(len(self.site.posts), 1)
        self.assertEqual(self.marker(approval).stat().st_size, 0)  # claimed, no receipt
        self.assertEqual(self.act(approval)[0], 4)
        self.assertEqual(self.stops()[-1]["reason"], "approval_used")
        sha = read(approval)["approval_sha256"]
        code, o, e = self.cli("watchdog", "--out", self.out, "--json")
        self.assertEqual(code, 0, e)
        self.assertEqual(json.loads(o)["outcome_unknown"], [sha])
        for argv in (("handover", "--out", self.out, "--approval", approval),
                     ("resume", "--out", self.out, "--approval", approval)):
            self.assertIn(self.cli(*argv)[0], (2, 4))
        self.assertEqual(len(self.site.posts), 1)

    def test_f5_watchdog_audited_changes_nothing(self):
        approval = self.build()
        with self.assertRaises(SystemExit):
            self.act(approval, extra={"fixture": Crash})
        before = tree_digest(self.state)
        rc, events, out = audit_run(["watchdog", "--out", str(self.out), "--json"], self.env())
        self.assertEqual((rc, events), (0, []), out)
        self.assertEqual(tree_digest(self.state), before)
        self.assertEqual(len(self.site.posts), 1)
        self.assertIn(read(approval)["approval_sha256"], out)

    def test_a5_takeover_handover_resume(self):
        approval = self.build()
        sha = read(approval)["approval_sha256"]
        self.site.set("/form", status=302, location="/login")
        self.assertEqual(self.act(approval)[0], 3)
        self.assertEqual(self.stops()[-1]["reason"], "takeover")
        self.assertEqual(self.cli("handover", "--out", self.out, "--approval", approval)[0], 0)
        self.assertTrue((self.out / "handover" / f"{sha}.json").is_file())
        self.assertEqual(self.cli("resume", "--out", self.out, "--approval", approval)[0], 3)  # still the login page
        self.site.set("/form", body=form_html())  # the user logged in; identical form
        self.assertEqual(self.cli("resume", "--out", self.out, "--approval", approval)[0], 0)
        self.assertFalse((self.out / "invalidated" / sha).exists())
        self.assertEqual(self.act(approval)[0], 0)
        self.assertEqual(len(self.site.posts), 1)

    def test_a5_form_differs_after_login_invalidates(self):
        for n, variant in enumerate((form_html(extra='<input name="extra" type="text">'), form_html(rev="2"))):
            self.site.set("/form", body=form_html())
            out = self.tmp / f"o{n}"
            approval = self.build(out=out)
            sha = read(approval)["approval_sha256"]
            self.site.set("/form", status=302, location="/login")
            self.assertEqual(self.act(approval, out=out)[0], 3)
            self.assertEqual(self.cli("handover", "--out", out, "--approval", approval)[0], 0)
            self.site.set("/form", body=variant)  # DOM differs / revision only differs
            self.assertEqual(self.cli("resume", "--out", out, "--approval", approval)[0], 3)
            self.assertTrue((out / "invalidated" / sha).exists())
            code, o, e = self.act(approval, out=out)
            self.assertEqual(code, 4, (o, e))
            self.assertEqual(self.stops(out)[-1]["reason"], "approval_invalidated")
            self.assertEqual(self.cli("resume", "--out", out, "--approval", approval)[0], 4)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):  # bare protocol too
                self.assertIn(browser.main(["act", "--approval", str(approval), "--out", str(out / "r2.json")]), (3, 4))
        self.assertEqual(self.site.posts, [])

    def test_a6_no_credential_flags_and_no_password_bytes(self):
        def options(p, seen):
            for act in p._actions:
                seen += list(act.option_strings)
                if isinstance(getattr(act, "choices", None), dict):
                    for sub in act.choices.values():
                        options(sub, seen)
            return seen
        for parser in (ops.parser(), browser.parser()):
            for opt in options(parser, []):
                self.assertNotRegex(opt, r"pass|secret|token|cookie|credential|login|key|auth", opt)
        secret = "hunter2-correct-horse"
        self.site.set("/login", body=f"<html><body>sign in <input type='password' name='pw' value='{secret}'></body></html>")
        approval = self.build()
        self.site.set("/form", status=302, location="/login")
        self.act(approval)
        self.cli("handover", "--out", self.out, "--approval", approval)
        self.site.set("/form", body=form_html())  # the user logged in elsewhere
        self.cli("resume", "--out", self.out, "--approval", approval)
        self.act(approval)
        for root in (self.out, self.state):
            for f in Path(root).rglob("*"):
                if f.is_file():
                    self.assertNotIn(secret.encode(), f.read_bytes(), f.name)

    def test_a6_profile_dir_refusals_and_creation(self):
        home, out, st = self.tmp / "home", self.out, self.state
        (home / ".config" / "google-chrome").mkdir(parents=True)
        used = st / "ai-architecture" / "browser" / "used"
        used.mkdir(parents=True)
        real = self.tmp / "realdir"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "Local State").write_text("{}")
        refused = ["relative/profile", str(link), str(link / "sub"), str(out), str(out / "p"), str(self.tmp), str(used),
                   str(st), str(home / ".config" / "google-chrome"), str(home / ".config" / "google-chrome" / "Default"),
                   str(home / ".config" / "chromium"), str(home / "snap" / "chromium"),
                   str(home / ".var" / "app" / "com.google.Chrome"), str(foreign), str(home)]
        for p in refused:
            code, o, e = self.run_("survey", "--mode", "read", "--url", self.site.url(), "--out", out / "s.json",
                                   "--backend", "fixture", "--profile-dir", p)
            self.assertEqual(code, 2, (p, o, e))
        good = self.tmp / "profiles" / "p1"
        self.assertEqual(self.run_("survey", "--mode", "read", "--url", self.site.url(), "--out", out / "s.json",
                                   "--backend", "fixture", "--profile-dir", good)[0], 0)
        self.assertEqual(oct(good.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((good / ".aiarch-profile").stat().st_mode & 0o777), "0o600")
        (good / "SingletonLock").symlink_to(f"host-{os.getpid()}")  # a live owner
        self.assertEqual(self.run_("survey", "--mode", "read", "--url", self.site.url(), "--out", out / "s.json",
                                   "--backend", "fixture", "--profile-dir", good)[0], 2)

    def test_a7_prune_scope(self):
        runs = self.out / "runs"
        runs.mkdir()
        old, young = time.time() - 40 * 86400, time.time() - 1 * 86400
        for name, t in (("r-old1", old), ("r-old2", old), ("r-young", young)):
            (runs / name).mkdir()
            (runs / name / "events.jsonl").write_text("{}\n")
            os.utime(runs / name, (t, t))
        ext = self.tmp / "external"
        ext.mkdir()
        (ext / "keep.txt").write_text("x")
        (runs / "r-link").symlink_to(ext)
        os.utime(runs / "r-link", (old, old), follow_symlinks=False)
        (runs / "notes.txt").write_text("sibling")
        used = self.state / "ai-architecture" / "browser" / "used"
        used.mkdir(parents=True)
        (used / ("a" * 64)).write_text("{}")
        (self.out / "receipt.json").write_text("{}")
        code, o, e = self.cli("prune", "--out", self.out, "--json")
        self.assertEqual(code, 0, e)
        self.assertEqual(sorted(json.loads(o)["would_delete"]), ["runs/r-old1", "runs/r-old2"])
        self.assertTrue((runs / "r-old1").exists())  # the preview deletes nothing
        code, o, e = self.cli("prune", "--out", self.out, "--apply", "--json")
        self.assertEqual(code, 0, e)
        self.assertEqual(sorted(p.name for p in runs.iterdir()), ["notes.txt", "r-link", "r-young"])
        self.assertTrue((ext / "keep.txt").exists() and (used / ("a" * 64)).exists() and (self.out / "receipt.json").exists())
        self.assertEqual(self.cli("prune", "--out", self.out, "--days", "0", "--apply")[0], 0)
        self.assertEqual(sorted(p.name for p in runs.iterdir()), ["notes.txt", "r-link"])
        self.assertTrue((ext / "keep.txt").exists() and (used / ("a" * 64)).exists())

    def test_a8_cost_equals_events_and_is_offline(self):
        approval = self.build()
        self.assertEqual(self.act(approval)[0], 0)
        self.assertEqual(self.act(approval, name="r2.json")[0], 4)
        code, o, e = self.cli("cost", "--out", self.out, "--json")
        self.assertEqual(code, 0, e)
        self.assertEqual(json.loads(o), {"surveys": 1, "acts": 2, "receipts": 1,
                                         "stops_by_reason": {"approval_used": 1}, "browser_seconds": 0.0})
        rc, events, out = audit_run(["cost", "--out", str(self.out), "--json"], self.env(), out=os.path.realpath(self.out))
        self.assertEqual((rc, events), (0, []), out)

    def test_a9_watchdog_kill_scope(self):
        procs = []

        def fake(token):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--aiarch-token={token}"],
                                 start_new_session=True)
            procs.append(p)
            return p

        try:
            victim, decoy, recycled, fresh = fake("tok1"), fake("tok1"), fake("tok3"), fake("tok4")
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            rows = ((victim, "tok1", chrome.starttime(victim.pid), 100), (recycled, "tok3", chrome.starttime(recycled.pid) + 7, 100),
                    (fresh, "tok4", chrome.starttime(fresh.pid), 0), (dead, "orph", 1, 100))
            for i, (p, token, start, age) in enumerate(rows):
                d = self.out / "runs" / f"r{i}"
                d.mkdir(parents=True)
                (d / "browser.pid.json").write_text(json.dumps({"pid": p.pid, "starttime": start, "token": token,
                                                                "started": time.time() - age, "temp_profile": True,
                                                                "closed": False}))
            (self.tmp / "t" / "aiarch-chrome-orph").mkdir()
            code, o, e = self.cli("watchdog", "--out", self.out, "--timeout", "50", "--json")
            self.assertEqual(code, 0, e)
            self.assertIsNotNone(victim.wait(10))
            for p in (decoy, recycled, fresh):
                self.assertIsNone(p.poll(), "a process outside the pidfile match was killed")
            self.assertEqual(json.loads(o)["orphaned_profiles"], ["orph"])
            self.assertNotIn(str(self.tmp), o)
        finally:
            shutil.rmtree(self.tmp / "t" / "aiarch-chrome-orph", ignore_errors=True)
            for p in procs:
                p.kill()
                p.wait()

    def test_a10_adapters(self):
        code, o, e = self.cli("adapters", "--json")
        self.assertEqual(code, 0, e)
        kinds = {k["kind"]: k for k in json.loads(o)["adapters"]}
        self.assertEqual(set(kinds), {"extension", "desktop"})
        for k in kinds.values():
            self.assertEqual(k["state"], "unsupported")
            self.assertTrue(k["gap"].strip() and k["license"].strip())
        rc, events, out = audit_run(["adapters", "--json"], self.env())
        self.assertEqual((rc, events), (0, []), out)
        for name in ("extension", "desktop"):
            self.assertEqual(self.run_("act", "--approval", "x", "--out", self.out / "r.json", "--backend", name)[0], 2)
        self.assertEqual(set(backends.BACKENDS), {"fixture", "cdp"})

    def test_a11_descriptor_and_status(self):
        desc = contracts.check_module_descriptor(MODULE, module="browser-operations")
        self.assertEqual((desc["claimed_support"], desc["evidence"]), ("planned", []))
        os.environ["AIARCH_CHROME"] = str(self.tmp / "missing")
        rc, events, out = audit_run(["status", "--json"], self.env())
        self.assertEqual((rc, events), (0, []), out)
        st = json.loads(out.splitlines()[0])["backends"]
        self.assertEqual(st["chrome"]["state"], "unavailable")
        self.assertIn("AIARCH_CHROME", st["chrome"]["setup"])
        del os.environ["AIARCH_CHROME"]
        code, o, _ = self.cli("status", "--json")
        self.assertIn(json.loads(o)["backends"]["chrome"]["state"], ("available", "unavailable"))

    def test_a12_privacy(self):
        for root in (MODULE, EXAMPLE):
            self.assertEqual(privacy_scan.scan(root), [])
        approval = self.build()
        self.act(approval)
        forbidden = {os.path.realpath(self.out), os.path.expanduser("~"), getpass.getuser(), socket.gethostname()}
        forbidden.discard("")
        files = [f for f in self.out.rglob("*") if f.is_file()]
        self.assertTrue(any(f.name == "events.jsonl" for f in files))
        for f in files:
            text = f.read_text()
            for bad in forbidden:
                self.assertNotIn(bad, text, f.name)

    def test_f9_backends_scoped_and_restored(self):
        seen = []

        class Spy(backends.FixtureBackend):
            def __init__(self, **kw):
                super().__init__()
                seen.append(sorted(backends.BACKENDS))

        for argv in (["survey", "--mode", "read", "--url", self.site.url(), "--out", self.out / "s.json", "--backend", "fixture"],
                     ["survey", "--mode", "read", "--url", "http://192.0.2.1/", "--out", self.out / "s.json", "--backend", "fixture"],
                     ["survey", "--backend", "nope"], ["frobnicate"]):
            self.run_(*argv, extra={"fixture": Spy})
            self.assertEqual(backends.BACKENDS, self.orig)
        self.assertEqual(seen[0], ["cdp", "chrome", "fixture"])
        self.cli("status")
        self.cli("nonsense")
        self.assertEqual(backends.BACKENDS, self.orig)
        for f in (MODULE / "ops.py", MODULE / "chrome.py"):
            src = f.read_text()
            for bad in ("importlib", "__import__", "entry_points", "pkg_resources"):
                self.assertNotIn(bad, src, f.name)


class FakeServer:
    """A one-shot websocket server on 127.0.0.1 that sends scripted raw frames after a valid (or bad) handshake."""

    def __init__(self, frames, good=True):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(1)
        self.port = self.s.getsockname()[1]
        self.frames, self.good, self.request = frames, good, b""
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        import base64
        import hashlib
        c, _ = self.s.accept()
        while b"\r\n\r\n" not in self.request:
            self.request += c.recv(1024)
        key = [ln.split(b": ")[1] for ln in self.request.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + chrome.GUID).digest()).decode() if self.good else "bogus"
        c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        for f in self.frames:
            c.sendall(f)
        time.sleep(0.3)
        c.close()
        self.s.close()


def frame(op, payload, fin=True, mask=False):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | op])
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n)
    if mask:
        return head[:1] + bytes([head[1] | 0x80]) + head[2:] + b"\0\0\0\0" + payload
    return head + payload


class TestWebSocket(unittest.TestCase):
    def test_f16_client_rules(self):
        srv = FakeServer([frame(9, b"hi"), frame(1, b'{"a":', fin=False), frame(0, b'1}')])
        ws = chrome.WS(srv.port, "/devtools/browser/abc")
        self.assertNotIn(b"origin", srv.request.lower())  # F1: no Origin header
        self.assertEqual(ws.recv(2), '{"a":1}')  # ping answered, continuation frames joined
        ws.close()
        with self.assertRaises(chrome.CDPError):
            chrome.WS(FakeServer([], good=False).port, "/x")  # Sec-WebSocket-Accept mismatch
        ws = chrome.WS(FakeServer([frame(1, b"x" * (backends.BODY_CAP + 1))]).port, "/x")
        with self.assertRaises(chrome.CDPError):
            ws.recv(2)  # message cap
        ws = chrome.WS(FakeServer([frame(1, b"x", mask=True)]).port, "/x")
        with self.assertRaises(chrome.CDPError):
            ws.recv(2)  # servers must not mask
        ws = chrome.WS(FakeServer([]).port, "/x")
        with self.assertRaises((TimeoutError, chrome.CDPError)):
            ws.recv(0.05)  # per-call timeout

    def test_f16_responses_matched_by_id(self):
        b = chrome.Browser()
        evs = []
        b.on_event = evs.append
        srv = FakeServer([frame(1, json.dumps(m).encode()) for m in (
            {"method": "Page.x", "params": {}}, {"id": 99, "result": {"no": 1}}, {"id": 1, "result": {"yes": 1}})])
        b.ws = chrome.WS(srv.port, "/x")
        self.assertEqual(b.call("Any.method", session=False), {"yes": 1})
        self.assertEqual([e["method"] for e in evs], ["Page.x"])
        b.ws.close()


class Scripted(chrome.ChromeBackend):
    RUN_SCRIPTS = True  # hostile-page tests need the page's scripts to run so that the Fetch gate is what stops them


class TestRealChrome(Base):
    def spy(self, cls=chrome.ChromeBackend, **hooks):
        made = []

        class S(cls):
            def __init__(self, **kw):
                super().__init__(**kw)
                made.append(self)

            def hook(self, name):
                if name in hooks:
                    hooks[name](self)
        return S, made

    @real_chrome
    def test_a1_real_backend_journey(self):
        fixture_hash = backends.FixtureBackend().survey(self.site.url())["dom_sha256"]
        approval = self.build("chrome")
        self.assertEqual(read(self.out / "survey.json")["backend"], "chrome")
        self.assertEqual(read(self.out / "survey.json")["dom_sha256"], fixture_hash)  # comparable with the fixture backend
        code, o, e = self.act(approval, "chrome")
        self.assertEqual(code, 0, (o, e))
        self.assertEqual(self.run_("verify-receipt", self.out / "receipt.json")[0], 0)
        self.assertEqual(read(self.out / "receipt.json")["backend"], "chrome")
        self.assertEqual(len(self.site.posts), 1)
        self.assertEqual(self.site.posts[0]["values"], FACTS)
        self.assertIn("HeadlessChrome", self.site.posts[0]["ua"])
        self.assertEqual(procs_with(str(self.tmp / "t" / "aiarch-chrome-")), [])
        self.assertEqual(list((self.tmp / "t").glob("aiarch-chrome-*")), [])

    @real_chrome
    def test_a2_confinement_flags_env_and_fetch_log(self):
        os.environ.update(DBUS_SESSION_BUS_ADDRESS="unix:path=/x", DISPLAY=":9", WAYLAND_DISPLAY="w",
                          http_proxy="http://192.0.2.9:1", ALL_PROXY="http://192.0.2.9:1")
        self.site.set("/form", body=form_html(extra='<img src="http://192.0.2.1/x">'))
        info = {}

        def capture(b):
            pid = b.br.proc.pid
            info["argv"] = chrome.cmdline(pid)
            info["env"] = dict(x.split("=", 1) for x in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if x)
            info["port"], info["sink"] = b.br.port, b.br.sink
            info["listen"] = [ln.split() for f in ("tcp", "tcp6") for ln in Path(f"/proc/net/{f}").read_text().splitlines()[1:]]
        S, made = self.spy(before_post=capture)
        approval = self.build("chrome", extra={"chrome": S})
        self.assertEqual(self.act(approval, "chrome", extra={"chrome": S})[0], 0)
        argv, sink = info["argv"], info["sink"].port
        for flag in chrome.LAUNCH_FLAGS:
            want = flag.replace("<sink>", str(sink))
            if "<udd>" in flag or "<token>" in flag:
                self.assertTrue(any(a.startswith(want.split("<")[0]) for a in argv), flag)
            else:
                self.assertIn(want, argv)
        for bad in chrome.FORBIDDEN_FLAGS:
            self.assertFalse([a for a in argv if a.startswith(bad)], bad)
        self.assertFalse([a for a in argv if "9222" in a])
        allowed = {"PATH", "LANG", "TMPDIR", "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"}
        wrapper = {"SHLVL", "PWD", "OLDPWD", "_", "CHROME_WRAPPER", "CHROME_VERSION_EXTRA", "GNOME_DISABLE_CRASH_DIALOG"}  # set by google-chrome's own launcher script
        self.assertEqual(set(info["env"]) - allowed - wrapper, set())
        self.assertFalse([k for k in info["env"] if k.upper().startswith(("DBUS", "DISPLAY", "WAYLAND")) or k.lower().endswith("_proxy")])
        self.assertTrue({"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"} <= set(info["env"]))
        hexport = f":{info['port']:04X}"
        socks = [ln for ln in info["listen"] if ln[1].endswith(hexport) and ln[3] == "0A"]
        self.assertTrue(socks)
        self.assertTrue(all(ln[1].startswith(("0100007F", "00000000000000000000000001000000")) for ln in socks), socks)
        blocked = [e for m in made for e in m.fetch_log if "192.0.2.1" in e["url"]]
        self.assertTrue(blocked and not any(e["allowed"] for e in blocked), blocked)
        self.assertFalse([a for a in info["sink"].attempts if "192.0.2.1" in a])
        self.assertTrue(all(e["url"].startswith("http://127.0.0.1") or not e["allowed"] for m in made for e in m.fetch_log))
        readme = (MODULE / "README.md").read_text()
        for flag in chrome.LAUNCH_FLAGS + chrome.FORBIDDEN_FLAGS:
            self.assertIn(flag, readme)

    @real_chrome
    def test_a2_sink_proxy_takes_every_off_host_attempt(self):
        b = chrome.Browser()
        b.start()
        try:
            b.call("Page.enable")
            b.call("Page.navigate", {"url": "http://192.0.2.1/probe"})
            b.pump(lambda: any("192.0.2.1" in a for a in b.sink.attempts), 15)
            self.assertTrue(any("192.0.2.1" in a for a in b.sink.attempts))
        finally:
            b.close()

    @real_chrome
    def test_a3_restart_relaunches_resurveys_and_reposts_once(self):
        site = self.site

        def killer(bump):
            class Killer(chrome.ChromeBackend):
                def submit(self, *a):  # inside act, after cmd_act's pre-check, before ChromeBackend.submit
                    self.br.proc.kill()
                    self.br.proc.wait()
                    if bump:
                        site.set("/form", body=form_html(rev="2"))
                    return super().submit(*a)
            return Killer
        for bump, code, posts in ((False, 0, 1), (True, 3, 0)):
            site.posts.clear()
            site.set("/form", body=form_html())
            out = self.tmp / f"o{int(bump)}"
            approval = self.build("chrome", out=out)
            got = self.act(approval, "chrome", extra={"chrome": killer(bump)}, out=out)
            self.assertEqual((got[0], len(site.posts)), (code, posts), got)
            if bump:
                self.assertFalse(self.marker(approval).exists())  # nothing posted: the approval is not spent
                self.assertEqual(self.stops(out)[-1]["reason"], "revision_changed")

    @real_chrome
    def test_f3_kill_after_release_keeps_marker_and_never_reposts(self):
        S, made = self.spy(after_continue=lambda b: (b.br.proc.kill(), b.br.proc.wait()))
        approval = self.build("chrome")
        code, o, e = self.act(approval, "chrome", extra={"chrome": S})
        self.assertEqual(code, 5, (o, e))
        self.assertTrue(self.marker(approval).exists())
        self.assertEqual(self.stops()[-1]["reason"], "outcome_unknown")
        self.assertEqual(self.act(approval, "chrome", name="r2.json")[0], 4)
        self.assertLessEqual(len(self.site.posts), 1)

    @real_chrome
    def test_f2_hostile_page_exactly_one_approved_post(self):
        hostile = ("<script>fetch('/submit',{method:'POST',body:'evil=load'});"
                   "var f=document.getElementById('card');f.action='/submit2';"
                   "var h=document.createElement('input');h.type='hidden';h.name='evil';h.value='1';f.appendChild(h);"
                   "setInterval(function(){fetch('/submit',{method:'POST',body:'evil=tick'})},20);"
                   "document.addEventListener('input',function(){fetch('/submit',{method:'POST',body:'evil=input'})});</script>")
        self.site.set("/form", body=form_html(extra=hostile))
        S, made = self.spy(cls=Scripted)  # scripts ON here: the Fetch gate is what stops this page
        approval = self.build("chrome", extra={"chrome": S}, reviewer=[sys.executable, str(ECHO)])
        code, o, e = self.act(approval, "chrome", extra={"chrome": S})
        self.assertEqual(code, 0, (o, e))
        self.assertEqual([(p["path"], p["raw"]) for p in self.site.posts], [("/submit2", urllib.parse.urlencode(read(self.out / "proposal.json")["values"]))])
        log = [x for m in made for x in m.fetch_log if x["method"] == "POST"]
        self.assertEqual(sum(1 for x in log if x["allowed"]), 1)
        self.assertTrue(any(not x["allowed"] for x in log))

    @real_chrome
    def test_u10f1_pin_predicates_each_block_a_page_driven_post(self):
        """Tribune F2: after the pin the page swaps the form action and fires POSTs that each differ from the approved
        one in exactly one respect (URL / body / frame). Only the approved POST may be allowed."""
        site, state = self.site, {}
        hostile = ("<script>var f=document.getElementById('card'),armed=false;"
                   "function post(u,b){fetch(u,{method:'POST',body:b,headers:{'Content-Type':'application/x-www-form-urlencoded'}}).catch(function(){})}"
                   "setInterval(function(){fetch('/flag').then(function(r){return r.text()}).then(function(t){"
                   "if(t!=='go'||armed)return;armed=true;f.action='/evil';"
                   "fetch('/body').then(function(r){return r.text()}).then(function(b){"
                   "post('/evil',b);post('/submit','x=wrong');"
                   "var i=document.createElement('iframe');i.onload=function(){i.contentWindow.fetch(location.origin+'/submit',"
                   "{method:'POST',body:b,headers:{'Content-Type':'application/x-www-form-urlencoded'}}).catch(function(){})};"
                   "i.srcdoc='<p>x</p>';document.body.appendChild(i);});"
                   "}).catch(function(){})},20);</script>")
        site.set("/form", body=form_html(extra=hostile))
        site.set("/flag", body="wait")

        def arm(b):
            body = urllib.parse.urlencode(read(self.out / "proposal.json")["values"])
            state.update(body=body, frame=b.armed["frame"])
            site.set("/body", body=body)
            site.set("/flag", body="go")
            pump_for(b, 1.0)  # the page's own POSTs race the approved one inside this window
        S, made = self.spy(cls=Scripted, before_post=arm)
        approval = self.build("chrome", extra={"chrome": S}, reviewer=[sys.executable, str(ECHO)])
        code, o, e = self.act(approval, "chrome", extra={"chrome": S})
        self.assertEqual(code, 0, (o, e))
        self.assertEqual([(p["path"], p["raw"]) for p in site.posts], [("/submit", state["body"])])
        log = [x for m in made for x in m.fetch_log if x["method"] == "POST"]
        allowed = [x for x in log if x["allowed"]]
        self.assertEqual(len(allowed), 1, log)
        self.assertEqual(allowed[0]["frame"], state["frame"])
        for what in ("/evil", "/submit"):  # URL swap; and (wrong body / other frame) on the approved URL
            self.assertTrue([x for x in log if x["url"].endswith(what) and not x["allowed"]], (what, log))
        self.assertTrue([x for x in log if x["frame"] != state["frame"] and not x["allowed"]], log)

    def _hostile_fixture(self, websocket=True):
        """Second loopback server B that must see NOTHING; the page on A tries popup, worker, service worker, WebSocket,
        beacon and a service-worker-handled POST to B."""
        b = Site()
        self.addCleanup(b.close)
        bu = f"http://127.0.0.1:{b.port}"
        page = ("<script>function go(){try{window.open('%s/popup')}catch(e){}try{navigator.sendBeacon('%s/beacon','x=1')}catch(e){}"
                "%stry{new Worker('/w.js')}catch(e){}}"
                "setTimeout(go,0);setTimeout(go,500);"
                "try{navigator.serviceWorker.register('/sw.js')}catch(e){}</script>") % (
            bu, bu, ("try{new WebSocket('ws://127.0.0.1:%d/ws')}catch(e){}" % b.port) if websocket else "")
        js = "application/javascript"
        self.site.set("/form", body=form_html(extra=page))
        self.site.set("/w.js", ctype=js, body="try{fetch('%s/worker',{method:'POST',body:'w=1',mode:'no-cors'})}catch(e){}"
                      "%s" % (bu, ("try{new WebSocket('ws://127.0.0.1:%d/wsw')}catch(e){}" % b.port) if websocket else ""))
        self.site.set("/sw.js", ctype=js, body=(
            "self.addEventListener('install',function(e){self.skipWaiting();fetch('%s/sw-install',{method:'POST',body:'s=1',mode:'no-cors'})});"
            "self.addEventListener('activate',function(e){e.waitUntil(clients.claim())});"
            "self.addEventListener('fetch',function(e){if(e.request.method==='POST'){"
            "fetch('%s/sw-fetch',{method:'POST',body:'s=2',mode:'no-cors'});e.respondWith(new Response('<html><body>swallowed</body></html>'))}});") % (bu, bu))
        return b

    @real_chrome
    def test_u10f2_hostile_page_reaches_no_other_loopback_port(self):
        """Security F1: popup, worker, service worker, beacon and WebSocket attempts to a second loopback port. Page scripts
        do not run at all, so nothing can start a WebSocket (Fetch cannot gate those); the approved POST still lands."""
        b = self._hostile_fixture()
        S, made = self.spy(before_post=lambda _b: pump_for(_b, 2.5))  # let any attempts happen while Chrome is up
        approval = self.build("chrome", extra={"chrome": S}, reviewer=[sys.executable, str(ECHO)])
        code, o, e = self.act(approval, "chrome", extra={"chrome": S})
        self.assertEqual(code, 0, (o, e))
        self.assertEqual(len(self.site.posts), 1)
        self.assertEqual((b.gets, [p["path"] for p in b.posts]), ([], []), "another loopback port was reached")

    @real_chrome
    def test_u10f4_markup_and_header_egress_opens_no_connection(self):
        """Security F1 (fix1): with the default config, preconnect (tag and Link header), speculation rules (prefetch and
        prerender), prefetch, meta refresh and a[ping] aimed at a second loopback port open no TCP connection and send
        no request. B counts raw accepts, so a bare preconnect is visible."""
        conns, reqs, stop = [], [], threading.Event()
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(32)
        lsock.settimeout(0.1)
        bport = lsock.getsockname()[1]

        def accept_loop():
            while not stop.is_set():
                try:
                    c, _ = lsock.accept()
                except (socket.timeout, OSError):
                    continue
                conns.append(1)
                try:
                    c.settimeout(0.3)
                    line = c.recv(256).split(b"\r\n")[0].decode("latin-1")
                    if line:
                        reqs.append(line)
                except OSError:
                    pass
                c.close()
        t = threading.Thread(target=accept_loop, daemon=True)
        t.start()

        def cleanup():
            stop.set()
            t.join(2)
            lsock.close()
        self.addCleanup(cleanup)
        bu = f"http://127.0.0.1:{bport}"
        rules = json.dumps({"prefetch": [{"source": "list", "urls": [bu + "/sp-prefetch"]}],
                            "prerender": [{"source": "list", "urls": [bu + "/sp-prerender"]}]})
        page = (f"<link rel='preconnect' href='{bu}'><link rel='prefetch' href='{bu}/lp'><link rel='dns-prefetch' href='{bu}'>"
                f"<link rel='prerender' href='{bu}/lpr'><script type='speculationrules'>{rules}</script>"
                f"<a ping='{bu}/ping' href='{bu}/a'>x</a>")
        self.site.set("/form", body=form_html(extra=page), headers={"Link": f"<{bu}>; rel=preconnect"})
        S, made = self.spy(before_post=lambda _b: pump_for(_b, 3.0))
        approval = self.build("chrome", extra={"chrome": S}, reviewer=[sys.executable, str(ECHO)])
        code, o, e = self.act(approval, "chrome", extra={"chrome": S})
        self.assertEqual(code, 0, (o, e))
        self.assertEqual(len(self.site.posts), 1)
        # meta refresh would navigate the page away from the approved POST, so it gets its own page and a survey
        self.site.set("/refresh", body=f"<html><head><meta http-equiv='refresh' content='0;url={bu}/refresh'></head><body>r</body></html>")
        self.run_("survey", "--mode", "read", "--url", self.site.url("/refresh"), "--out", self.out / "r.json",
                  "--backend", "chrome", extra={"chrome": S})
        time.sleep(0.5)
        self.assertEqual((len(conns), reqs), (0, []), "a markup/header vector reached the second loopback port")

    @real_chrome
    def test_u10f3_child_targets_are_gated_even_when_scripts_run(self):
        """Tribune F1 + security F1 with scripts ON (defence in depth): workers, service workers and popups get the same
        Fetch gate, and a service worker neither swallows the approved POST nor sends its own."""
        b = self._hostile_fixture(websocket=False)
        gated = []
        S, made = self.spy(cls=Scripted, before_post=lambda _b: pump_for(_b, 2.5), child_target=gated.append)
        approval = self.build("chrome", extra={"chrome": S}, reviewer=[sys.executable, str(ECHO)])
        code, o, e = self.act(approval, "chrome", extra={"chrome": S})
        self.assertEqual(code, 0, (o, e))
        self.assertEqual(len(self.site.posts), 1)
        self.assertEqual((b.gets, [p["path"] for p in b.posts]), ([], []), "another loopback port was reached")
        self.assertTrue(gated, "no child target was seen: the fixture did not exercise the gate")

    @real_chrome
    def test_a14_refusals_with_chrome(self):
        S, made = self.spy()
        ex, out = {"chrome": S}, self.out
        for path, loc in (("/r1", "http://192.0.2.1/x"), ("/r2", "http://127.0.0.1:1/x"), ("/r3", "http://localhost/x")):
            self.site.set(path, status=302, location=loc)
            self.assertEqual(self.run_("survey", "--mode", "read", "--url", self.site.url(path), "--out", out / "s.json",
                                       "--backend", "chrome", extra=ex)[0], 2, path)
        self.site.set("/lg", status=302, location="/login")
        self.assertEqual(self.run_("survey", "--mode", "form", "--url", self.site.url("/lg"), "--out", out / "s.json",
                                   "--backend", "chrome", extra=ex)[0], 0)
        self.assertTrue(read(out / "s.json")["takeover"])
        self.assertEqual(self.run_("propose", "--survey", out / "s.json", "--facts", self.facts, "--form", "card",
                                   "--out", out / "p.json")[0], 3)
        self.site.set("/e", body=form_html(action="http://192.0.2.1/x"))
        self.assertEqual(self.run_("survey", "--mode", "form", "--url", self.site.url("/e"), "--out", out / "s.json",
                                   "--backend", "chrome", extra=ex)[0], 0)
        self.assertEqual(self.run_("propose", "--survey", out / "s.json", "--facts", self.facts, "--form", "card",
                                   "--out", out / "p.json")[0], 2)
        for url in ("file:///etc/hostname", (ROOT / "modules" / "browser" / "tests" / "fixtures" / "form.html").as_uri()):
            self.assertEqual(self.run_("survey", "--mode", "read", "--url", url, "--out", out / "s.json",
                                       "--backend", "chrome", extra=ex)[0], 2)
        o2 = self.tmp / "o2"
        approval = self.build("chrome", extra=ex, out=o2)
        self.site.set("/form", body=form_html(action="http://192.0.2.1/x"))  # action swapped before act
        self.assertIn(self.act(approval, "chrome", extra=ex, out=o2)[0], (2, 3))
        self.assertEqual(self.site.posts, [])
        for m in made:
            self.assertTrue(all(not e["allowed"] or e["url"].startswith("http://127.0.0.1") for e in m.fetch_log))
            self.assertFalse([a for a in m.sinks if "192.0.2.1" in a])

    @real_chrome
    def test_f6_sigterm_leaves_no_chrome_and_a_consistent_outcome(self):
        approval = self.build("chrome")
        proc = subprocess.Popen([sys.executable, "-B", str(MODULE / "ops.py"), "run", "--ops-out", str(self.out), "act",
                                 "--approval", str(approval), "--out", str(self.out / "receipt.json"), "--backend", "chrome"],
                                env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 30
            while not list(self.out.glob("runs/*/browser.pid.json")) and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)  # the pid file IS the chrome-launched event
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(procs_with(str(self.tmp / "t" / "aiarch-chrome-")), [])
        self.assertEqual(list((self.tmp / "t").glob("aiarch-chrome-*")), [])
        before = len(self.site.posts)
        if self.marker(approval).exists():  # claimed: never act twice
            self.assertEqual(self.act(approval, "chrome", name="r2.json")[0], 4)
            self.assertEqual(len(self.site.posts), before)
        self.assertLessEqual(len(self.site.posts), 1)

    @real_chrome
    def test_profile_dir_with_chrome(self):
        prof = self.tmp / "profiles" / "p"
        self.assertEqual(self.run_("survey", "--mode", "read", "--url", self.site.url(), "--out", self.out / "s.json",
                                   "--backend", "chrome", "--profile-dir", prof)[0], 0)
        self.assertTrue((prof / ".aiarch-profile").is_file())
        self.assertEqual(oct(prof.stat().st_mode & 0o777), "0o700")
        self.assertEqual(procs_with(str(prof)), [])


class TestExample(Base):
    def run_script(self, *argv, env=None):
        e = self.env()
        e.update(env or {})
        return subprocess.run([sys.executable, "-B", str(EXAMPLE / "verify_receipt.py"), *argv], capture_output=True,
                              text=True, env=e, timeout=120)

    def test_a13_bare_run_reports_real_backend(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"real_backend: (ran|NOT RUN \(.+\))")

    def test_a13_require_browser_without_chrome_fails(self):
        r = self.run_script(env={"AIARCH_REQUIRE_BROWSER": "1", "PATH": "/nonexistent"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("NOT RUN", r.stdout)

    def test_a13_tampered_receipt_fails(self):
        approval = self.build()
        self.assertEqual(self.act(approval)[0], 0)
        good = self.out / "receipt.json"
        self.assertEqual(self.run_script("--receipt", str(good)).returncode, 0)
        doc = read(good)
        doc["status"] = 500
        good.write_text(json.dumps(doc))
        self.assertEqual(self.run_script("--receipt", str(good)).returncode, 1)


if __name__ == "__main__":
    unittest.main()
