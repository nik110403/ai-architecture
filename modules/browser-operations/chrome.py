"""Headless Chrome backend for modules/browser: launcher, minimal CDP client, ChromeBackend. Stdlib only.

127.0.0.1 only. Chrome is started per run with a fresh profile (or an opt-in, validated profile directory), driven
over a loopback DevTools port, and always killed and cleaned up in close(). Page content is untrusted: it is parsed
by backends.snapshot() and hashed, never interpreted.
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "browser"))
import backends  # noqa: E402

CHROME_ENV = "AIARCH_CHROME"
NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
SETUP = ("install Chrome or Chromium and put it on PATH, or set AIARCH_CHROME=/path/to/chrome "
         "(headless Chrome is launched per run; nothing else is needed)")
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
NAV_TIMEOUT = 40
CALL_TIMEOUT = 30
TOKEN_FLAG = "--aiarch-token="
FORBIDDEN_FLAGS = ("--remote-allow-origins", "--remote-debugging-address", "--no-sandbox", "--disable-setuid-sandbox",
                   "--disable-web-security", "--allow-file-access-from-files", "--single-process", "--no-zygote")
# The exact launch argv (README lists the same flags); <udd>, <sink> and <token> are filled per run.
LAUNCH_FLAGS = ("--headless=new", "--user-data-dir=<udd>", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-component-extensions-with-background-pages",
                "--disable-background-networking", "--disable-sync", "--disable-component-update",
                "--disable-default-apps", "--disable-domain-reliability", "--disable-breakpad", "--no-pings",
                "--password-store=basic", "--proxy-server=http://127.0.0.1:<sink>",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--remote-debugging-port=0",
                "--aiarch-token=<token>")
LIVE = []          # browsers started and not yet closed (ops closes them on exit and on SIGTERM/SIGHUP)
BROWSER_SECONDS = [0.0]


class OutcomeUnknown(Exception):
    """The approved POST was released to Chrome and something failed after that: it may or may not have landed."""


class CDPError(Exception):
    pass


def find_chrome(environ=None):
    """Path of the Chrome binary from AIARCH_CHROME or PATH, or None. Reads configuration only; starts nothing."""
    environ = os.environ if environ is None else environ
    cfg = environ.get(CHROME_ENV)
    if cfg:
        return cfg if os.access(cfg, os.X_OK) and os.path.isfile(cfg) else None
    return next((p for p in (shutil.which(n, path=environ.get("PATH", os.defpath)) for n in NAMES) if p), None)


def starttime(pid):
    """Field 22 of /proc/<pid>/stat (clock ticks since boot), or None when the process is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        return []


# --- websocket (RFC 6455 client, loopback only) ------------------------------------------------------------------

class WS:
    def __init__(self, port, path, timeout=CALL_TIMEOUT):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk or len(head) > 8192:
                raise CDPError("websocket handshake failed")
            head += chunk
        lines = head.decode("latin-1").split("\r\n")
        hdr = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)}
        want = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        if " 101" not in lines[0] or hdr.get("sec-websocket-accept") != want:
            raise CDPError("websocket handshake rejected")

    def _read(self, n, deadline):
        buf = b""
        while len(buf) < n:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                raise TimeoutError("cdp read timed out")
            if not chunk:
                raise CDPError("browser connection closed")
            buf += chunk
        return buf

    def send(self, text):
        data = text.encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.settimeout(CALL_TIMEOUT)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout):
        """One complete text message (continuation frames joined). Capped at BODY_CAP; control frames handled."""
        deadline = time.monotonic() + timeout
        parts, size = [], 0
        while True:
            b0, b1 = self._read(2, deadline)
            fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if masked or b0 & 0x70:
                raise CDPError("protocol violation from browser")
            if n == 126:
                n = struct.unpack(">H", self._read(2, deadline))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8, deadline))[0]
            if n > backends.BODY_CAP or size + n > backends.BODY_CAP:
                raise CDPError("browser message exceeds the size cap")
            payload = self._read(n, deadline) if n else b""
            if op == 8:
                raise CDPError("browser closed the connection")
            if op == 9:
                self.sock.sendall(bytes([0x8A, 0x80]) + b"\0\0\0\0")
                continue
            if op == 10:
                continue
            if op in (1, 0):
                parts.append(payload)
                size += n
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")
            else:
                raise CDPError("unsupported websocket frame")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Sink:
    """127.0.0.1 proxy sink: Chrome's proxy for everything that is not loopback. Counts and refuses every attempt."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.attempts = []
        self.closed = False

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                line = conn.recv(1024).split(b"\r\n")[0].decode("latin-1")
                self.attempts.append(line[:200])
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.closed = True
        self.sock.close()


# --- launcher ---------------------------------------------------------------------------------------------------

def _pdeathsig():
    os.umask(0o077)
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def _write_json(path, doc):
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(doc, sort_keys=True) + "\n")
    os.replace(tmp, path)


class Browser:
    """One Chrome process: own session, parent-death signal, fresh temp profile unless a validated one is given."""

    def __init__(self, profile_dir=None, run_dir=None):
        self.profile_dir, self.run_dir = profile_dir, run_dir
        self.proc = self.ws = self.sink = self.udd = None
        self.temp = profile_dir is None
        self.token = secrets.token_hex(8)
        self.port = None
        self.t0 = None
        self.seq = 0
        self.session = self.tid = None
        self.on_event = lambda msg: None
        self.on_reply = lambda msg: None

    def argv(self, exe):
        flags = [f.replace("<udd>", str(self.udd)).replace("<sink>", str(self.sink.port)).replace("<token>", self.token)
                 for f in LAUNCH_FLAGS]
        return [exe, *flags, "about:blank"]

    def start(self):
        exe = find_chrome()
        if not exe:
            raise backends.Unavailable(SETUP)
        self.sink = Sink()
        self.udd = Path(self.profile_dir) if self.profile_dir else Path(tempfile.gettempdir()) / f"aiarch-chrome-{self.token}"
        if self.temp:
            os.mkdir(self.udd, 0o700)
        home = self.udd / ".aiarch-home"
        home.mkdir(mode=0o700, exist_ok=True)
        self._lock_prefs()
        (self.udd / "DevToolsActivePort").unlink(missing_ok=True)
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), XDG_CACHE_HOME=str(home))
        errf = open(home / "stderr.txt", "wb")
        try:
            self.proc = subprocess.Popen(self.argv(exe), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=errf, start_new_session=True, preexec_fn=_pdeathsig)
        finally:
            errf.close()
        self.t0 = time.monotonic()
        LIVE.append(self)
        self.sink.start()
        self._record(False)
        try:
            self._connect(home)
        except BaseException as e:
            tail = f"[{type(e).__name__}: {str(e)[:80]}] "
            try:
                tail += (home / "stderr.txt").read_text("utf-8", "replace")[-300:]
            except OSError:
                pass
            self.close()
            raise backends.Unavailable("chrome did not start (no sandbox fallback is used): " + " ".join(tail.split()))

    def _lock_prefs(self):
        """Preconnect/prefetch hints are browser-process work that neither Fetch nor script-disabling gates: switch the
        network predictor off in the profile before launch (merged into an existing profile's Preferences)."""
        d = self.udd / "Default"
        d.mkdir(mode=0o700, exist_ok=True)
        f = d / "Preferences"
        try:
            doc = json.loads(f.read_text())
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, ValueError):
            doc = {}
        doc.setdefault("net", {})
        if not isinstance(doc["net"], dict):
            doc["net"] = {}
        doc["net"]["network_prediction_options"] = 2  # NetworkPredictionOptions::kDisabled
        _write_json(f, doc)

    def _connect(self, home):
        port_file = self.udd / "DevToolsActivePort"
        deadline = time.monotonic() + NAV_TIMEOUT
        while True:  # Chrome writes the file in pieces: wait until both lines are there and well formed
            lines = port_file.read_text().splitlines() if port_file.exists() else []
            if len(lines) >= 2 and lines[0].isdigit() and re.fullmatch(r"/devtools/browser/[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", lines[1]):
                break
            if self.proc.poll() is not None or time.monotonic() > deadline:
                raise backends.Unavailable("chrome exited or did not publish a valid DevTools port")
            time.sleep(0.02)
        self.port = int(lines[0])
        self.ws = WS(self.port, lines[1])
        self.tid = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": self.tid, "flatten": True})["sessionId"]

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and self.ws is not None

    def send(self, method, params=None, session=True):
        self.seq += 1
        msg = {"id": self.seq, "method": method, "params": params or {}}
        if isinstance(session, str):  # a child target's flattened session
            msg["sessionId"] = session
        elif session and self.session:
            msg["sessionId"] = self.session
        self.ws.send(json.dumps(msg))
        return self.seq

    def call(self, method, params=None, session=True, timeout=CALL_TIMEOUT):
        """Send and wait for the response with the same id; every other message is an event for on_event()."""
        want = self.send(method, params, session)
        deadline = time.monotonic() + timeout
        while True:
            msg = json.loads(self.ws.recv(max(0.01, deadline - time.monotonic())))
            if msg.get("id") == want:
                if "error" in msg:
                    raise CDPError(f"{method}: {str(msg['error'].get('message'))[:120]}")
                return msg.get("result", {})
            if "method" in msg:
                self.on_event(msg)
            else:
                self.on_reply(msg)

    def pump(self, done, timeout):
        """Read events until done() is true."""
        deadline = time.monotonic() + timeout
        while not done():
            msg = json.loads(self.ws.recv(max(0.01, deadline - time.monotonic())))
            if "method" in msg:
                self.on_event(msg)
            else:
                self.on_reply(msg)

    def _record(self, closed):
        if not self.run_dir:
            return
        _write_json(Path(self.run_dir) / "browser.pid.json",
                    {"pid": self.proc.pid, "starttime": starttime(self.proc.pid), "token": self.token,
                     "started": time.time(), "temp_profile": self.temp, "closed": closed})

    def close(self):
        if self in LIVE:
            LIVE.remove(self)
        if self.t0 is not None:
            BROWSER_SECONDS[0] += time.monotonic() - self.t0
            self.t0 = None
        if self.proc is not None:
            pgid = self.proc.pid
            for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    self.proc.wait(wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            try:
                os.killpg(pgid, signal.SIGKILL)  # stragglers in the group
            except (ProcessLookupError, PermissionError):
                pass
            self.proc.wait()
            if self.ws:  # only after the process group is dead: detaching the debugger would release held requests/targets
                self.ws.close()
                self.ws = None
            try:
                self._record(True)
            except OSError:
                pass
            self.proc = None
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.sink:
            self.sink.close()
        if self.temp and self.udd:
            shutil.rmtree(self.udd, ignore_errors=True)
        elif self.udd:
            shutil.rmtree(self.udd / ".aiarch-home", ignore_errors=True)


def close_all():
    for b in list(LIVE):
        b.close()


# --- backend ----------------------------------------------------------------------------------------------------

FETCH_ALL = {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
DOC_JS = "function(){return document.documentElement.outerHTML}"
POST_JS = ("function(action, body){fetch(action,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
           "body:body,redirect:'manual',credentials:'same-origin',cache:'no-store'}).catch(function(){});}")


class ChromeBackend(backends.Backend):
    name = "chrome"
    RUN_SCRIPTS = False      # page scripts never run (the survey is the served DOM, as with the fixture backend); tests flip this

    def __init__(self, run_dir=None, profile_dir=None):
        self.run_dir, self.profile_dir = run_dir, profile_dir
        self.br = None
        self.fetch_log = []
        self.allowed = None      # origin every request must have
        self.armed = None        # the one approved POST: dict(action, body, frame)
        self.continued = False
        self.nav = {}
        self.post = {}
        self.sinks = []          # sink attempts of closed browsers
        self.gating = {}         # in-flight steps that gate a new target: reply id -> (session, steps, next, target)

    def hook(self, name):
        """Test seam: called at named points; does nothing."""

    def probe(self):
        if find_chrome():
            return {"state": "available", "setup": ""}
        return {"state": "unavailable", "setup": SETUP}

    def can_submit(self, url):
        self._http(url)

    @staticmethod
    def _http(url):
        backends.check_url(url)
        if url.startswith("file:"):
            raise backends.Refused("the chrome backend reaches http://127.0.0.1 only")
        return url

    def close(self):
        if self.br:
            self.sinks += self.br.sink.attempts if self.br.sink else []
            self.br.close()
            self.br = None

    # -- browser session --
    def _ensure(self):
        if self.br is not None and self.br.alive():
            return
        self.close()
        self.br = Browser(self.profile_dir, self.run_dir)
        self.br.on_event = self._event
        self.br.on_reply = self._reply
        self.gating = {}
        self.br.start()
        try:
            for m in ("Page.enable", "Network.enable"):
                self.br.call(m)
            if not self.RUN_SCRIPTS:  # WebSocket handshakes and beacons bypass Fetch: no page script, no way to start them
                self.br.call("Emulation.setScriptExecutionDisabled", {"value": True})
            self.br.call("Fetch.enable", FETCH_ALL)
            # Every other target (popup, dedicated/shared/service worker, iframe process) is held at start, given the same
            # gate, and only then resumed; if the gate cannot be set the target stays paused. Page and browser level:
            # workers attach to their page, popups and service workers to the browser.
            self.br.call("Target.setAutoAttach", AUTO_ATTACH)
            self.br.call("Target.setAutoAttach", AUTO_ATTACH, session=False)
        except (CDPError, OSError, TimeoutError) as e:
            self.close()
            raise backends.Unavailable(f"cannot drive chrome: {type(e).__name__}")
        self.hook("connected")

    def _allow(self, p):
        req = p["request"]
        url, method = req["url"], req["method"]
        if method == "GET":
            try:
                backends.check_url(url)
            except backends.Refused:
                return False
            return url.startswith("http://") and backends.origin(url) == self.allowed
        a = self.armed
        return bool(method == "POST" and a and not self.continued and url == a["action"] and p.get("frameId") == a["frame"]
                    and req.get("postData") == a["body"])

    def _attached(self, p):
        """A new target appeared: gate it before it runs. Steps are chained on the replies, so a failed step leaves it paused."""
        sid, info = p.get("sessionId"), p.get("targetInfo", {})
        if not sid or info.get("targetId") == self.br.tid or not p.get("waitingForDebugger"):
            return  # our own page, or a target that existed before auto-attach (never held at start)
        if not self.RUN_SCRIPTS and info.get("type") in ("worker", "service_worker", "shared_worker"):
            self.br.send("Target.closeTarget", {"targetId": info.get("targetId")}, session=False)  # script gate is not offered on workers
            self.hook("child_target")
            return
        steps = [("Emulation.setScriptExecutionDisabled", {"value": not self.RUN_SCRIPTS}),
                 ("Fetch.enable", FETCH_ALL), ("Target.setAutoAttach", AUTO_ATTACH), ("Runtime.runIfWaitingForDebugger", {})]
        self.gating[self.br.send(steps[0][0], steps[0][1], session=sid)] = (sid, steps, 1, info.get("targetId"))
        self.hook("child_target")

    def _reply(self, msg):
        job = self.gating.pop(msg.get("id"), None)
        if not job:
            return
        sid, steps, i, tid = job
        optional = steps[i - 1][0] in ("Emulation.setScriptExecutionDisabled",)  # not offered on every target type
        if "error" in msg and not optional:
            self.br.send("Target.closeTarget", {"targetId": tid}, session=False)  # left paused if this fails too
            return
        if i < len(steps):
            self.gating[self.br.send(steps[i][0], steps[i][1], session=sid)] = (sid, steps, i + 1, tid)

    def _event(self, msg):
        m, p = msg["method"], msg.get("params", {})
        if m == "Target.attachedToTarget":
            self._attached(p)
        elif m == "Fetch.requestPaused":
            ok = self._allow(p)
            req = p["request"]
            self.fetch_log.append({"method": req["method"], "url": req["url"][:200], "allowed": ok, "frame": p.get("frameId")})
            if ok and req["method"] == "POST":
                self.continued = True  # from the moment the bytes go out the outcome is unknown, not "changed"
                self.post["net"] = p.get("networkId") or p.get("requestId")
            try:
                sid = msg.get("sessionId", True)  # answer on the session the request came from
                if ok:
                    self.br.send("Fetch.continueRequest", {"requestId": p["requestId"]}, session=sid)
                else:
                    self.br.send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "BlockedByClient"}, session=sid)
            except OSError:
                if ok and req["method"] == "POST":
                    return
                raise
            if ok and req["method"] == "POST":
                self.hook("after_continue")
        elif m == "Network.responseReceived":
            r = p.get("response", {})
            if p.get("type") == "Document" and p.get("frameId") == self.nav.get("frame"):
                self.nav["status"] = r.get("status")
                self.nav["rev"] = {k.lower(): v for k, v in r.get("headers", {}).items()}.get("x-revision")
            if self.post.get("net") and p.get("requestId") == self.post["net"]:
                self.post["status"] = r.get("status")
        elif m == "Network.loadingFailed" and self.post.get("net") and p.get("requestId") == self.post["net"]:
            self.post["failed"] = True
        elif m in ("Page.loadEventFired", "Page.frameStoppedLoading"):
            self.nav["loaded"] = True
        elif m == "Network.loadingFailed" and p.get("type") == "Document":
            self.nav["loaded"] = True

    def _world(self, frame):
        return self.br.call("Page.createIsolatedWorld", {"frameId": frame, "worldName": "aiarch"})["executionContextId"]

    def _frame(self):
        f = self.br.call("Page.getFrameTree")["frameTree"]["frame"]
        return f["id"], f.get("loaderId"), f.get("url", "")

    def _page(self, url):
        """Navigate and return (final_url, html, revision_header, login_redirect, frame_id, loader_id)."""
        url0 = self._http(url)
        self.allowed = backends.origin(url0)
        self.nav = {"loaded": False}
        res = self.br.call("Page.navigate", {"url": url0})
        self.nav["frame"] = res.get("frameId")
        if res.get("errorText"):
            raise backends.Refused(f"cannot load page: {res['errorText'][:60]}")
        self.br.pump(lambda: self.nav["loaded"], NAV_TIMEOUT)
        frame, loader, final = self._frame()
        try:
            self._http(final)
        except backends.Refused:
            raise backends.Refused("page left the allowed origin or failed to load")
        if backends.origin(final) != backends.origin(url0):
            raise backends.Refused("redirect leaves the origin")
        login = final != url0 and urllib.parse.urlparse(final).path.startswith("/login")
        status = self.nav.get("status")
        if not login and isinstance(status, int) and status >= 400:
            raise backends.Refused(f"HTTP {status}")
        html = ""
        if not login:
            ctx = self._world(frame)
            r = self.br.call("Runtime.callFunctionOn", {"functionDeclaration": DOC_JS, "executionContextId": ctx,
                                                        "returnByValue": True})
            html = r.get("result", {}).get("value") or ""
            if not isinstance(html, str) or len(html) > backends.BODY_CAP:
                raise backends.Refused("page content missing or too large")
        return (url0 if login else final), html, self.nav.get("rev"), login, frame, loader

    def survey(self, url):
        self._http(url)
        self._ensure()
        try:
            final, html, rev, login, _, _ = self._page(url)
        except (CDPError, OSError, TimeoutError) as e:
            raise backends.Refused(f"browser failure: {type(e).__name__}")
        return backends.snapshot(final, html, rev, login)

    def submit(self, url, form_id, values, action, pin):
        """Re-launch if needed, re-survey, re-pin, then release exactly one POST through the Fetch gate.
        Changed only before that POST is released; after it, any failure is OutcomeUnknown (marker kept)."""
        self._http(url)
        action = backends.same_origin_action(url, action)
        body = urllib.parse.urlencode(values)
        self.continued, self.armed, self.post = False, None, {}
        try:
            self._ensure()
            final, html, rev, login, frame, loader = self._page(url)
            now = backends.snapshot(final, html, rev, login)
            for reason, changed in (("takeover", now["takeover"]), ("revision_changed", now["revision"] != pin["revision"]),
                                    ("dom_changed", now["dom_sha256"] != pin["dom_sha256"])):
                if changed:
                    raise backends.Changed(reason)
            if self._frame()[1] != loader:
                raise backends.Changed("dom_changed")
            self.armed = {"action": action, "body": body, "frame": frame}
            self.hook("before_post")
            ctx = self._world(frame)
            self.br.call("Runtime.callFunctionOn", {"functionDeclaration": POST_JS, "executionContextId": ctx,
                                                    "arguments": [{"value": action}, {"value": body}],
                                                    "returnByValue": True})
            self.br.pump(lambda: "status" in self.post or self.post.get("failed"), CALL_TIMEOUT)
            self.armed = None
            if self.post.get("failed") or "status" not in self.post:
                raise OutcomeUnknown("the approved request did not complete")
            status = self.post["status"]
            after = self.survey(url)
            return {"status": status, "after": after}
        except OutcomeUnknown:
            raise
        except backends.Changed:
            raise
        except BaseException as e:
            if self.continued:
                raise OutcomeUnknown(f"failed after the approved request was released: {type(e).__name__}") from None
            raise backends.Changed("browser_failed") from None  # nothing was released, so nothing was posted
        finally:
            self.armed = None
