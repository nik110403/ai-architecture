"""Render and inspect a committed tree in real headless Chrome (stdlib only; loopback only).

The commit's files are read from the object store (hash-verified in Python) into a temp dir (no .git, no commit message)
and fulfilled to a fresh chrome.Browser (U10 launcher) over CDP (Fetch.fulfillRequest) at a fixed 127.0.0.1 origin where
nothing listens. Only same-origin GETs are answered, everything else is failed and logged, and loopback is not exempt from
the sink proxy, so a socket that skips Fetch (WebSocket, WebRTC) ends at the sink and is counted. Page scripts run
(U10's backend keeps them off), so use this on your own local source only. Every step is appended to trace.jsonl.
Nothing here judges the page; checks.py does.
"""
import base64
import ipaddress
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GIT_HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "gc.auto=0"]
FIXED_DATE = "2026-01-01T00:00:00Z"
TIMEOUT_ENV, FAULT_ENV = "AIARCH_RENDER_TIMEOUT", "AIARCH_DESIGN_FAULT"
MAX_TABS = 40
KEYS = {"Tab": (9, "Tab", ""), "Enter": (13, "Enter", "\r"), "Escape": (27, "Escape", ""), "Space": (32, "Space", " "),
        "ArrowDown": (40, "ArrowDown", ""), "ArrowUp": (38, "ArrowUp", "")}
# every response carries this: workers inherit it, so fetch, XHR, WebSocket, beacon and EventSource stay same-origin;
# report-uri makes Chrome POST each violation back, where the Fetch gate logs it (page scripts cannot wipe that log)
CSP_PATH = "/__aiarch-csp"
# The page's origin. Nothing listens there: every request to it is answered over CDP, and because loopback is not exempt
# from the sink proxy, a connection that is not answered that way (WebSocket, WebRTC, a worker's own socket) is seen there.
ORIGIN = "http://127.0.0.1:49173"
CSP = "connect-src 'self'; worker-src 'self'; form-action 'self'; base-uri 'self'; report-uri /__aiarch-csp"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".json": "application/json",
         ".png": "image/png", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}


class Refused(Exception):
    """Exit 2: the input is not something we render (non regular entries, bad scope)."""


class NotRun(Exception):
    """Exit 3: no browser rendered."""


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chrome = _load("aiarch_design_chrome", ROOT / "modules" / "browser-operations" / "chrome.py")
checks = _load("aiarch_design_checks", HERE / "checks.py")
backends = chrome.backends


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


# --- the tree -------------------------------------------------------------------------------------------------------

def git_env():
    env = {k: os.environ[k] for k in ("PATH", "AIARCH_GIT") if k in os.environ}
    env.update(LANG="C", HOME="/nonexistent", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1",
               GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_NAME="fixture",
               GIT_COMMITTER_EMAIL="fixture@example.invalid", GIT_AUTHOR_DATE=FIXED_DATE, GIT_COMMITTER_DATE=FIXED_DATE)
    return env


def git(repo, *argv, binary=False):
    exe = os.environ.get("AIARCH_GIT") or "git"
    try:
        proc = subprocess.run([exe, "-C", str(repo), *GIT_HARDEN, *argv], capture_output=True, env=git_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f"git unavailable: {error.__class__.__name__}")
    if proc.returncode != 0:
        raise Refused(f"git {argv[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:120]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


GIT_READ = ["-c", "core.commitGraph=false", "-c", "core.multiPackIndex=false", "-c", "core.hooksPath=/dev/null"]
OID = re.compile(r"[0-9a-f]{40}\Z")
MAX_OBJECT, MAX_ENTRIES, MAX_DEPTH, READ_SECONDS = 32 << 20, 5000, 24, 120


def guard_repo(repo):
    """lstat-refuse a run repo whose object store could point outside itself (links, alternates)."""
    dotgit = os.path.join(repo, ".git")
    try:
        if not stat.S_ISDIR(os.lstat(dotgit).st_mode):
            raise Refused("repo .git is not a real directory")
        objects = os.path.join(dotgit, "objects")
        if not stat.S_ISDIR(os.lstat(objects).st_mode):
            raise Refused("repo .git/objects is not a real directory")
        for name in os.listdir(objects):
            if stat.S_ISLNK(os.lstat(os.path.join(objects, name)).st_mode):
                raise Refused(f"repo object store holds a link: {name[:20]}")
        for name in ("alternates", "http-alternates"):
            if os.path.lexists(os.path.join(objects, "info", name)):
                raise Refused(f"repo object store has {name}")
    except OSError as error:
        raise Refused(f"repo object store unreadable: {error.__class__.__name__}")
    return os.path.realpath(objects)


class Objects:
    """Hash-verified, read-only view of a run repo's objects.

    Git never opens the run repo itself: a throwaway bare shim (no config, no hooks, no filters, no remotes) borrows
    its object directory through `alternates`, and only `cat-file --batch` runs against it. Python re-hashes every
    object, so a forged loose object, pack entry or commit-graph cannot change what is read.
    """

    def __init__(self, repo):
        objects = guard_repo(repo)
        self.tmp = tempfile.mkdtemp(prefix="design-shim-")
        self.proc = self.timer = None
        shim = os.path.join(self.tmp, "shim.git")
        try:
            self._git(["init", "-q", "--bare", "--template=", shim], cwd=self.tmp)
            os.makedirs(os.path.join(shim, "objects", "info"), exist_ok=True)
            with open(os.path.join(shim, "objects", "info", "alternates"), "w", encoding="utf-8") as handle:
                handle.write(objects + "\n")
            self.proc = subprocess.Popen([os.environ.get("AIARCH_GIT") or "git", f"--git-dir={shim}", *GIT_READ, "cat-file", "--batch"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         env=git_env(), cwd=self.tmp)
        except (OSError, Refused) as error:
            self.close()
            raise Refused(f"git unavailable: {error.__class__.__name__}" if isinstance(error, OSError) else str(error))
        self.timer = threading.Timer(READ_SECONDS, self.proc.kill)
        self.timer.daemon = True
        self.timer.start()

    @staticmethod
    def _git(argv, cwd):
        try:
            proc = subprocess.run([os.environ.get("AIARCH_GIT") or "git", *argv], capture_output=True, env=git_env(), cwd=cwd,
                                  timeout=60)
        except subprocess.TimeoutExpired:
            raise Refused("git unavailable: timeout")
        if proc.returncode != 0:
            raise Refused(f"git {argv[0]} failed")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.timer:
            self.timer.cancel()
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            for pipe in (self.proc.stdin, self.proc.stdout):
                pipe.close()
            self.proc = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, oid, kind):
        """Body of the object `oid` (40 hex) of type `kind`; Refused when missing, mistyped, oversized or not its own hash."""
        if not isinstance(oid, str) or not OID.match(oid):
            raise Refused("object id is not a 40 hex sha1")
        try:
            self.proc.stdin.write(oid.encode() + b"\n")
            self.proc.stdin.flush()
            head = self.proc.stdout.readline().split()
            if len(head) != 3 or head[0].decode("ascii", "replace") != oid:
                raise Refused(f"object missing: {oid[:12]}")
            label, size = head[1], int(head[2])
            if size > MAX_OBJECT:
                raise Refused(f"object too large: {oid[:12]}")
            body = self.proc.stdout.read(size)
            self.proc.stdout.read(1)
        except (OSError, ValueError):
            raise Refused("git object read failed")
        if len(body) != size:
            raise Refused("git object read failed")
        if hashlib.sha1(label + b" %d\0" % size + body).hexdigest() != oid:
            raise Refused(f"object hash mismatch: {oid[:12]}")
        if label != kind.encode():
            raise Refused(f"object is a {label.decode('ascii', 'replace')[:10]}, not a {kind}: {oid[:12]}")
        return body

    def commit(self, oid):
        """(tree oid, [parent oids]) read from the verified commit body."""
        tree, parents = None, []
        for line in self.get(oid, "commit").split(b"\n\n", 1)[0].decode("utf-8", "replace").split("\n"):
            key, _, value = line.partition(" ")
            if key == "tree" and tree is None and not parents and OID.match(value):
                tree = value
            elif key == "parent" and tree and OID.match(value):
                parents.append(value)
            elif key not in ("author", "committer", "encoding", "gpgsig", "mergetag") and not line.startswith(" "):
                raise Refused("commit header is malformed")
        if not tree:
            raise Refused("commit has no tree")
        return tree, parents

    def entries(self, oid):
        """[(mode, name, oid)] of a verified tree; a name that cannot be a plain path component is refused."""
        body, found, seen, i = self.get(oid, "tree"), [], set(), 0
        while i < len(body):
            space, nul = body.find(b" ", i), body.find(b"\0", i)
            if space < 0 or nul < space or nul + 21 > len(body):
                raise Refused("tree is malformed")
            mode, raw, child = body[i:space], body[space + 1:nul], body[nul + 1:nul + 21].hex()
            i = nul + 21
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise Refused("tree entry name is not UTF-8")
            if name in ("", ".", "..") or "/" in name or "\0" in name or name.rstrip(" .").casefold() == ".git":
                raise Refused(f"unsafe tree entry name: {name[:30]!r}")
            if name in seen:
                raise Refused(f"duplicate tree entry: {name[:30]!r}")
            seen.add(name)
            if mode not in (b"100644", b"100755", b"40000"):
                raise Refused(f"tree entry is not a regular file or directory: {name[:30]!r} mode {mode.decode('ascii', 'replace')[:8]}")
            found.append((mode, name, child))
        return found

    def write(self, tree, dest, depth=0, budget=None):
        budget = [MAX_ENTRIES] if budget is None else budget
        if depth > MAX_DEPTH:
            raise Refused("tree is nested too deeply")
        for mode, name, child in self.entries(tree):
            budget[0] -= 1
            if budget[0] < 0:
                raise Refused("tree has too many entries")
            path = os.path.join(dest, name)
            if mode == b"40000":
                os.mkdir(path, 0o755)
                self.write(child, path, depth + 1, budget)
                continue
            data = self.get(child, "blob")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if mode == b"100755" else 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)


def commit_info(repo, commit):
    """(tree oid, parent oids) of `commit`, from the verified commit object."""
    with Objects(repo) as objects:
        return objects.commit(commit)


def extract_tree(repo, commit, dest):
    """The files of `commit` into dest, read and re-hashed in Python. Only plain files and directories are written."""
    try:
        with Objects(repo) as objects:
            tree, _ = objects.commit(commit)
            os.makedirs(dest, exist_ok=True)
            objects.write(tree, str(dest))
    except OSError as error:
        raise Refused(f"cannot write the tree: {error.__class__.__name__}")
    return Path(dest)


def regular_files(root):
    """Sorted relative paths of every regular file under root; a link or special file is a Refused."""
    found = []
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise Refused(f"non-regular entry: {name[:60]}")
            if stat.S_ISREG(mode):
                found.append(os.path.relpath(full, root))
    return sorted(found)


def reply(tree, names, method, path, body=b""):
    """(status, headers, body) the renderer fulfils a same-origin request with; every answer carries the CSP.

    GET serves a regular file of the tree (`names` = regular_files(tree), so never through a link); anything else that
    is not a CSP report is None (the caller fails and logs it)."""
    headers = [("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")]
    route = urllib.parse.urlparse(path).path
    if method == "POST" and route == CSP_PATH:
        return 204, headers, b""
    if method != "GET":
        return None
    rel = urllib.parse.unquote(route).lstrip("/") or "index.html"
    if rel.endswith("/"):
        rel += "index.html"
    parts = rel.split("/")
    if not all(p not in ("", ".", "..") and "\0" not in p for p in parts) or rel not in names:
        return 404, headers, b""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(os.path.join(tree, rel), flags)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
    except OSError:
        return 404, headers, b""
    kind = TYPES.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
    return 200, [("Content-Type", kind)] + headers, data


def violation_text(body):
    """'directive blocked-uri' from a csp-report POST body (the page-side collector prints the same shape)."""
    try:
        doc = json.loads(body)["csp-report"]
        return f"{doc.get('violated-directive', 'csp')} {str(doc.get('blocked-uri', ''))[:120]}".strip()
    except (ValueError, KeyError, TypeError):
        return "csp report"


# --- page side scripts (run in an isolated world, except VIOLATIONS) ---------------------------------------------------

VIOLATIONS = ("(function(){window.__aiarch_viol=[];document.addEventListener('securitypolicyviolation',"
              "function(e){window.__aiarch_viol.push(e.violatedDirective+' '+String(e.blockedURI).slice(0,120));});})();")
SEL_JS = ("function selOf(e){if(!e||!e.tagName)return '';var s=e.tagName.toLowerCase();if(e.id)return s+'#'+e.id;"
          "if(e.classList&&e.classList.length)s+='.'+Array.prototype.join.call(e.classList,'.');return s;}")
COLLECT_JS = "function(){" + SEL_JS + """
var out={doc:{scrollWidth:document.documentElement.scrollWidth,clientWidth:document.documentElement.clientWidth,
 innerWidth:window.innerWidth},elements:[]};
var all=Array.prototype.slice.call(document.body.querySelectorAll('*'),0,600);
all.forEach(function(e){
 var cs=getComputedStyle(e),r=e.getBoundingClientRect();
 if(cs.display==='none'||cs.visibility==='hidden'||r.width<=0||r.height<=0)return;
 var text=false;e.childNodes.forEach(function(n){if(n.nodeType===3&&n.textContent.trim())text=true;});
 var bg=null,img=false,a=e;
 while(a){var c=getComputedStyle(a);if(c.backgroundImage!=='none'){img=true;break;}
  var m=c.backgroundColor.match(/[\\d.]+/g);var al=m.length>3?parseFloat(m[3]):1;
  if(al===1){bg=c.backgroundColor;break;}a=a.parentElement;}
 var own=cs.backgroundColor.match(/[\\d.]+/g);
 var borders=[];['Top','Right','Bottom','Left'].forEach(function(s){
  if(cs['border'+s+'Style']!=='none'&&parseFloat(cs['border'+s+'Width'])>0)borders.push(cs['border'+s+'Color']);});
 out.elements.push({sel:selOf(e),rect:[r.left,r.top,r.right,r.bottom].map(function(v){return Math.round(v*10)/10;}),
  text:text,color:cs.color,bg:bg,bgImg:img,ownBg:(own.length>3?parseFloat(own[3]):1)===1?cs.backgroundColor:null,
  fontSize:parseFloat(cs.fontSize),fontWeight:parseFloat(cs.fontWeight)||400,fontFamily:cs.fontFamily,borders:borders});
});
return out;}"""
REQUIRED_JS = "function(sels){" + SEL_JS + """
var out={};sels.forEach(function(s){var e=document.querySelector(s),ok=false;
 if(e){var cs=getComputedStyle(e),r=e.getBoundingClientRect();ok=cs.display!=='none'&&cs.visibility!=='hidden'&&r.width>0&&r.height>0;}
 out[s]=ok;});return out;}"""
ACTIVE_JS = "function(targets){" + SEL_JS + """
var e=document.activeElement,body=(e===document.body||e===document.documentElement||!e);
var hit=null;targets.forEach(function(t){if(!body&&e.matches(t)&&hit===null)hit=t;});
return {active:body?'BODY':selOf(e),body:body,hasFocus:document.hasFocus(),target:hit};}"""
BOX_JS = "function(s){var e=document.querySelector(s);if(!e)return null;var r=e.getBoundingClientRect();return [r.left,r.top,r.width,r.height];}"
EXPECT_JS = "function(x){var e=document.querySelector(x.selector);if(!e)return {found:false};" + """
var cs=getComputedStyle(e),r=e.getBoundingClientRect();
var vis=cs.display!=='none'&&cs.visibility!=='hidden'&&r.width>0&&r.height>0;
return {found:true,visible:vis,attr:x.attr?e.getAttribute(x.attr[0]):null,focused:document.activeElement===e,
 hasClass:x.class?e.classList.contains(x.class):null};}"""
FREEZE_JS = "function(){document.getAnimations().forEach(function(a){a.pause();a.currentTime=0;});}"
MOTION_JS = "function(sels){var out={};sels.forEach(function(s){var e=document.querySelector(s);if(!e)return;" + \
            "var cs=getComputedStyle(e);out[s]={animation:cs.animationDuration,transition:cs.transitionDuration};});return out;}"

_exe_sha = {}


def exe_sha256(path):
    if path not in _exe_sha:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        _exe_sha[path] = digest.hexdigest()
    return _exe_sha[path]


def parse_viewport(text):
    width, _, height = str(text).partition("x")
    return int(width), int(height)


def expect_met(expect, got):
    if not got.get("found"):
        return False
    if "visible" in expect and got["visible"] != expect["visible"]:
        return False
    if "attr" in expect and got["attr"] != expect["attr"][1]:
        return False
    if "class" in expect and not got["hasClass"]:
        return False
    if "focused" in expect and got["focused"] != expect["focused"]:
        return False
    return True


class SinkBrowser(chrome.Browser):
    """U10's launcher plus `--proxy-bypass-list=<-loopback>`: Chrome exempts loopback from a proxy by default, which is how
    RTC ICE-TCP and WebSocket handshakes reached a real 127.0.0.1 listener. With the exemption removed they arrive at the sink."""

    def argv(self, exe):
        flags = super().argv(exe)
        return flags[:-1] + ["--proxy-bypass-list=<-loopback>"] + flags[-1:]


chrome.Browser = SinkBrowser  # ChromeBackend._ensure looks the class up in this (privately loaded) module


class Renderer(chrome.ChromeBackend):
    """ChromeBackend with page scripts on. The sink proxy, resolver rules and target gating are U10's; the page is
    fulfilled in-process (Fetch.fulfillRequest) from the verified tree at ORIGIN, and any other request is failed and logged."""
    RUN_SCRIPTS = True

    def __init__(self, tree):
        super().__init__()
        self.tree, self.names, self.reports = tree, regular_files(tree), []
        self.ctx = self.frame = None
        self.errors, self.external, self.trace, self.shots = {}, [], [], {}
        self.out = None

    def settle(self, seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.br.pump(lambda: False, max(0.01, end - time.monotonic()))
            except TimeoutError:
                return

    def guard(self, name, fn):
        """Run one check's data collection; a browser failure becomes an `error` for that check, never silence."""
        try:
            if os.environ.get(FAULT_ENV) == name:
                raise chrome.CDPError("injected fault")
            return fn()
        except (chrome.CDPError, TimeoutError, OSError, ValueError, KeyError) as error:
            self.errors[name] = f"{type(error).__name__}: {str(error)[:80]}"
            return None

    def step(self, step, inp, expect, observed, shot_sha=""):
        self.trace.append({"step": step, "input": inp, "expect": expect, "observed": observed, "screenshot_sha": shot_sha})

    def start(self):
        try:
            self._ensure()
            call = self.br.call
            call("Page.bringToFront")
            call("Emulation.setFocusEmulationEnabled", {"enabled": True})

            call("DOM.enable")
            call("Accessibility.enable")
            call("Page.addScriptToEvaluateOnNewDocument", {"source": VIOLATIONS})
        except chrome.backends.Unavailable as error:
            raise NotRun(str(error)[:160])
        except (chrome.CDPError, OSError, TimeoutError) as error:
            raise NotRun(f"cannot drive chrome: {type(error).__name__}")

    def _event(self, msg):
        if msg["method"] == "Fetch.requestPaused":
            self.paused(msg)
        else:
            super()._event(msg)

    def paused(self, msg):
        """Answer one gated request: a same-origin GET (or a CSP report) is fulfilled from the tree, everything else fails."""
        p, sid = msg["params"], msg.get("sessionId", True)
        req = p["request"]
        parsed = urllib.parse.urlparse(req["url"])
        got = None
        if f"{parsed.scheme}://{parsed.netloc}" == ORIGIN:
            got = reply(self.tree, self.names, req["method"], parsed.path, req.get("postData") or "")
        if got and req["method"] == "POST":
            self.reports.append(violation_text(req.get("postData") or ""))
        else:
            self.fetch_log.append({"method": req["method"], "url": req["url"][:200], "allowed": got is not None,
                                   "frame": p.get("frameId")})
        if got:
            status, headers, body = got
            self.br.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": status,
                                                  "responseHeaders": [{"name": k, "value": v} for k, v in headers],
                                                  "body": base64.b64encode(body).decode()}, session=sid)
        else:
            self.br.send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "BlockedByClient"}, session=sid)

    def _attached(self, p):
        """Workers: Fetch cannot be enabled on a worker session (CDP has no such command there), so the gate U10 chains
        cannot be set and the worker would stay paused for ever. Resume it at once: its script came through the page's
        gate with the CSP, and whatever it opens that is not answered above ends at the sink and is counted."""
        info, sid = p.get("targetInfo", {}), p.get("sessionId")
        if sid and p.get("waitingForDebugger") and info.get("type") in ("worker", "shared_worker", "service_worker"):
            self.br.send("Runtime.runIfWaitingForDebugger", {}, session=sid)
            self.hook("child_target")
            return
        super()._attached(p)

    def harvest(self):
        """Violations the page recorded before it is replaced."""
        try:
            got = self.br.call("Runtime.evaluate", {"expression": "JSON.stringify(window.__aiarch_viol||[])",
                                                    "returnByValue": True})
            for item in json.loads(got.get("result", {}).get("value") or "[]"):
                if item not in self.external:
                    self.external.append(item)
        except (chrome.CDPError, ValueError, TypeError):
            pass

    def load(self, base, name, vp, reduce=False, first=False):
        width, height = vp
        self.harvest()
        call = self.br.call
        call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion",
                                                         "value": "reduce" if reduce else "no-preference"}]})
        _, _, _, _, frame, _ = self._page(f"{base}/index.html")
        self.frame = frame
        self.settle(0.7 if first else 0.12)
        self.ctx = self._world(frame)
        self.ev(FREEZE_JS)
        call("Page.bringToFront")
        self.step("load", {"viewport": name, "reduced_motion": reduce}, None, {"hasFocus": self.ev("function(){return document.hasFocus();}")})

    def ev(self, fn, *args):
        got = self.br.call("Runtime.callFunctionOn", {"functionDeclaration": fn, "executionContextId": self.ctx,
                                                      "arguments": [{"value": a} for a in args], "returnByValue": True,
                                                      "awaitPromise": True})
        if got.get("exceptionDetails"):
            raise chrome.CDPError("page script failed")
        return got.get("result", {}).get("value")

    def shot(self, name, clip=None):
        params = {"format": "png", "fromSurface": True}
        if clip:
            params["clip"] = {"x": clip[0], "y": clip[1], "width": clip[2], "height": clip[3], "scale": 1}
        data = base64.b64decode(self.br.call("Page.captureScreenshot", params)["data"])
        if clip is None:
            self.shots[name] = data
        return data

    def key(self, name):
        code, label, text = KEYS[name]
        base = {"key": " " if name == "Space" else name, "code": label, "windowsVirtualKeyCode": code,
                "nativeVirtualKeyCode": code}
        self.br.call("Input.dispatchKeyEvent", dict(base, type="keyDown", **({"text": text} if text else {})))
        self.br.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))
        self.settle(0.05)

    # -- per viewport data --
    def ax_nodes(self):
        nodes = self.br.call("Accessibility.getFullAXTree")["nodes"]
        found = []
        for node in nodes:
            role = (node.get("role") or {}).get("value", "")
            if node.get("ignored") or role not in checks.INTERACTIVE_ROLES:
                continue
            sel, label = role, ""
            try:
                desc = self.br.call("DOM.describeNode", {"backendNodeId": node["backendDOMNodeId"]})["node"]
                attrs = dict(zip(desc.get("attributes", [])[::2], desc.get("attributes", [])[1::2]))
                sel = desc["nodeName"].lower() + ("#" + attrs["id"] if attrs.get("id") else
                                                  "." + attrs["class"].split()[0] if attrs.get("class") else "")
                label = attrs.get("aria-label", "")
            except (chrome.CDPError, KeyError):
                pass
            found.append({"role": role, "name": (node.get("name") or {}).get("value", ""), "sel": sel, "aria_label": label})
        return found

    def viewport(self, base, record, vp, first):
        name = f"{vp[0]}x{vp[1]}"
        self.load(base, name, vp, first=first)
        data = self.shot(f"vp-{name}")
        item = {"name": name, "w": vp[0], "h": vp[1], "shot": f"screenshots/vp-{name}.png", "shot_sha": sha(data),
                "doc": {}, "elements": [], "ax": [], "required": {}}
        got = self.guard("contrast", lambda: self.ev(COLLECT_JS))
        if got:
            item.update(doc=got["doc"], elements=got["elements"])
        else:
            item["doc"] = {"scrollWidth": 0, "clientWidth": 0, "innerWidth": vp[0]}
        item["ax"] = self.guard("a11y-name", self.ax_nodes) or []
        item["required"] = self.guard("required-present", lambda: self.ev(REQUIRED_JS, record["required_elements"])) or {}
        self.step("inspect", {"viewport": name}, None, {"elements": len(item["elements"]), "ax": len(item["ax"])}, item["shot_sha"])
        return item

    def clip(self, box, vp):
        x, y = max(0, int(box[0]) - 6), max(0, int(box[1]) - 6)
        w, h = min(vp[0] - x, int(box[2]) + 12), min(vp[1] - y, int(box[3]) + 12)
        return [x, y, max(1, w), max(1, h)]

    def keyboard(self, base, record, vp):
        """Real Tab walk from a fresh load; unfocused and focused clips of each target for the focus-visible check."""
        targets = sorted({i["target"] for i in record["interactions"]})
        name = f"{vp[0]}x{vp[1]}"
        out = {"viewport": name, "reached": {t: False for t in targets}, "trap": "", "steps": []}
        focus = {}
        self.load(base, name, vp)
        unfocused = {}
        for t in targets:
            box = self.ev(BOX_JS, t)
            if box and box[2] > 0:
                unfocused[t] = (self.clip(box, vp), checks.png_pixels(self.shot("clip", self.clip(box, vp))))
        previous = None
        for index in range(MAX_TABS):
            self.key("Tab")
            state = self.ev(ACTIVE_JS, targets)
            self.step("key", {"key": "Tab", "trusted": True, "n": index + 1}, None, state)
            out["steps"].append(state)
            if state["body"] or not state["hasFocus"]:
                break
            if previous == state["active"]:
                out["trap"] = state["active"]
                break
            previous = state["active"]
            tgt = state["target"]
            if tgt and not out["reached"][tgt]:
                out["reached"][tgt] = True
                if tgt in unfocused:
                    data = self.shot("clip", unfocused[tgt][0])
                    diff = sum(a != b for a, b in zip(checks.png_pixels(data), unfocused[tgt][1]))
                    focus[tgt] = {"reached": True, "diff": diff, "has_focus": state["hasFocus"], "focused_sha": sha(data)}
        for t in targets:
            focus.setdefault(t, {"reached": out["reached"][t], "diff": 0, "has_focus": False, "focused_sha": ""})
        return out, focus

    def interactions(self, base, record, vp):
        name = f"{vp[0]}x{vp[1]}"
        out = []
        for item in record["interactions"]:
            entry = {"id": item["id"], "target": item["target"], "key": item["key"], "expect": item["expect"],
                     "reached": False, "ok": False, "observed": None, "shot_sha": ""}
            self.load(base, name, vp)
            for _ in range(MAX_TABS):
                self.key("Tab")
                state = self.ev(ACTIVE_JS, [item["target"]])
                if state["target"]:
                    entry["reached"] = True
                    break
                if state["body"] or not state["hasFocus"]:
                    break
            if entry["reached"]:
                for earlier in item.get("before", []):
                    self.key(earlier)
                self.key(item["key"])
                self.settle(0.15)
                got = self.ev(EXPECT_JS, item["expect"])
                entry["ok"] = expect_met(item["expect"], got)
                entry["observed"] = {k: v for k, v in got.items() if v is not None}
                data = self.shot(f"int-{item['id']}")
                entry["shot_sha"] = sha(data)
            self.step(f"interaction:{item['id']}", {"key": item["key"], "before": item.get("before", []), "target": item["target"], "trusted": True},
                      item["expect"], entry["observed"] if entry["reached"] else {"blocked": "keyboard-reach"},
                      entry["shot_sha"])
            out.append(entry)
        return out

    def motion(self, base, record, vp):
        self.load(base, f"{vp[0]}x{vp[1]}", vp, reduce=True)
        reduced = self.ev(MOTION_JS, record.get("motion_targets", []))
        self.step("reduced-motion", {"emulate": "prefers-reduced-motion: reduce"}, None, reduced)
        return {"emulated": True, "reduced": reduced}


def render_commit(repo, commit, record, out, adapters):
    """Render `commit` of repo. Writes out/{screenshots/*.png, inspection.json, trace.jsonl}. Returns the inspection."""
    with tempfile.TemporaryDirectory(prefix="design-tree-") as tree:
        extract_tree(repo, commit, tree)
        return render_tree(tree, record, out, adapters)


def render_tree(tree, record, out, adapters):
    out = Path(out)
    regular_files(tree)
    exe = chrome.find_chrome()
    if not exe:
        raise NotRun("chrome unavailable: " + chrome.SETUP[:90])
    if os.environ.get(TIMEOUT_ENV):
        chrome.NAV_TIMEOUT = int(os.environ[TIMEOUT_ENV])
    base = ORIGIN
    r = Renderer(tree)
    try:
        r.start()
        try:
            product = r.br.call("Browser.getVersion", session=False).get("product", "")
        except (chrome.CDPError, OSError, TimeoutError) as error:
            raise NotRun(f"cannot drive chrome: {type(error).__name__}")
        vps = [parse_viewport(v) for v in record["viewports"]]
        insp = {"version": 1, "renderer": {"name": "chrome", "product": product, "exe_sha256": exe_sha256(exe)},
                "viewports": [], "errors": r.errors}
        try:
            for index, vp in enumerate(vps):
                insp["viewports"].append(r.viewport(base, record, vp, index == 0))
        except (chrome.CDPError, OSError, TimeoutError) as error:
            raise NotRun(f"browser failed while rendering: {type(error).__name__}")
        kb = r.guard("keyboard-reach", lambda: r.keyboard(base, record, vps[0]))
        insp["keyboard"], insp["focus"] = kb if kb else ({"viewport": insp["viewports"][0]["name"], "reached": {}, "trap": "",
                                                         "steps": []}, {})
        if kb is None:
            r.errors.setdefault("focus-visible", r.errors["keyboard-reach"])
        insp["interactions"] = r.guard("interaction", lambda: r.interactions(base, record, vps[0])) or []
        insp["motion"] = {"emulated": False, "reduced": {}}
        if "motion" in adapters:
            insp["motion"] = r.guard("reduced-motion", lambda: r.motion(base, record, vps[0])) or insp["motion"]
        r.harvest()
        r.settle(0.5)  # late CSP reports (workers, frames) arrive as POSTs
        mine = own_attempts(list(r.br.sink.attempts), page_text(tree))
        insp["external"] = sorted(set([f"{e['method']} {e['url'].split('?')[0]}" for e in r.fetch_log if not e["allowed"]]
                                      + [f"proxy {a}" for a in mine] + [f"csp {v}" for v in r.external + r.reports]))
        shots, trace = r.shots, r.trace
    finally:
        r.close()
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    for name, data in shots.items():
        (out / "screenshots" / f"{name}.png").write_bytes(data)
    (out / "trace.jsonl").write_text("".join(json.dumps(t, sort_keys=True) + "\n" for t in trace), encoding="utf-8")
    return insp


def page_text(tree):
    parts = []
    for name in regular_files(tree):
        with open(os.path.join(tree, name), "rb") as handle:
            parts.append(handle.read(1 << 20).decode("utf-8", "replace"))
    return "\n".join(parts)


def proxy_host(line):
    """Host a sink request line names ('CONNECT host:443 ...', 'CONNECT [::1]:80 ...' or 'GET http://host/path ...'), or ''."""
    parts = line.split()
    if len(parts) < 2:
        return ""
    target = parts[1]
    if "://" in target:
        return urllib.parse.urlparse(target).hostname or ""
    if target.startswith("["):
        return target[1:target.find("]")] if "]" in target else ""
    return target.split(":")[0]


def literal_host(host):
    """True for `localhost` and any IP literal: a page has no business opening a socket to one."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost"


def own_attempts(attempts, text):
    """The sink attempts that are the page's: every one to loopback or an IP literal, plus any whose host the sources name.
    Chrome's own background traffic (updater, time sync) names neither and is left out."""
    return [a for a in attempts if proxy_host(a) and (literal_host(proxy_host(a)) or proxy_host(a) in text)]
