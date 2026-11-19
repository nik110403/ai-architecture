"""Shared pieces of the media module: sibling loading, exits, tool runs, spec validation, the Chrome session, out dirs.

Stdlib only. Chrome is driven through the design pipeline's Renderer (U10 launcher, Fetch gate, sink proxy); every
external tool is spawned through the documents module's `run_group` (own session, group kill on timeout)."""
import base64
import getpass
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TOOL_TIMEOUT_ENV, RENDER_TIMEOUT_ENV, REQUIRE_ENV = "AIARCH_MEDIA_TOOL_TIMEOUT", "AIARCH_RENDER_TIMEOUT", "AIARCH_REQUIRE_MEDIA"


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


documents = _load("aiarch_media_documents", ROOT / "modules" / "documents" / "documents.py")
render = _load("aiarch_media_render", ROOT / "modules" / "design-pipeline" / "render.py")
chrome, checks = render.chrome, render.checks
IFACE = json.loads((HERE / "interface.json").read_text(encoding="utf-8"))
LIM = IFACE["limits"]
STATUS = {0: "ok", 1: "failed", 2: "refused", 3: "unavailable", 4: "provider-refused"}


class Exit(Exception):
    """A finished command: exit code, machine-readable reason, extra result fields."""

    def __init__(self, code, reason, **extra):
        super().__init__(reason)
        self.code, self.reason, self.extra = code, reason, extra


def esc(value):
    return html.escape(str(value), quote=True)


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def fmt_num(value):
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# ---- json and files ---------------------------------------------------------------------------------------------

def _pairs(items):
    found = {}
    for key, value in items:
        if key in found:
            raise Exit(2, "duplicate key")
        found[key] = value
    return found


def parse_json(text, what="spec"):
    try:
        return json.loads(text, object_pairs_hook=_pairs)
    except ValueError:
        raise Exit(2, f"invalid {what}: not json")
    except RecursionError:
        raise Exit(2, f"invalid {what}: too deep")


def read_capped(path, cap, reason="spec too large"):
    try:
        with open(path, "rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        raise Exit(2, "invalid spec: unreadable")
    if len(data) > cap:
        raise Exit(2, reason)
    return data


def regular_file(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def read_artifact(directory, name, cap=64 << 20):
    path = os.path.join(directory, name)
    if not regular_file(path):
        return None
    with open(path, "rb") as handle:
        return handle.read(cap)


def describe_file(out, rel, volatile=False):
    data = Path(out, rel).read_bytes()
    entry = {"path": rel, "sha256": sha(data), "bytes": len(data)}
    if volatile:
        entry["volatile"] = True
    return entry


def default_out():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ai-architecture", "media", "runs", time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])


def prepare_out(path):
    """The out dir (0700, created if absent); never a symlink, nothing below a symlink, nobody else's. Returns its realpath."""
    path = os.path.abspath(os.path.expanduser(path)) if path else default_out()
    if os.path.lexists(path):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Exit(2, "unsafe out dir")
    else:
        try:
            os.makedirs(path, mode=0o700)
        except OSError:
            raise Exit(2, "unsafe out dir")
    if os.path.realpath(path) != path:
        raise Exit(2, "unsafe out dir")
    return path


def fresh_dir(path):
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            raise Exit(2, "unsafe out dir")
        shutil.rmtree(path)
    os.makedirs(path, mode=0o700)
    return path


# ---- tools --------------------------------------------------------------------------------------------------------

def tool_seconds(default):
    try:
        value = float(os.environ.get(TOOL_TIMEOUT_ENV, ""))
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


def child_env(home):
    env = documents.child_env(home)
    env["TMPDIR"] = str(home)
    return env


def run_tool(argv, name, default=10, cwd=None, limit=8 << 20):
    """(returncode, stdout, stderr) of one spawn through run_group; a timeout or a failed start is a host blocker."""
    seconds = tool_seconds(default)
    with tempfile.TemporaryDirectory() as home:
        try:
            return documents.run_group([str(a) for a in argv], timeout=seconds, env=child_env(home), cwd=cwd, limit=limit)
        except subprocess.TimeoutExpired:
            raise Exit(3, f"tool unavailable: {name} (timeout)")
        except OSError:
            raise Exit(3, f"tool unavailable: {name}")


VERSION_ARGS = {"pdftotext": ["-v"], "pdfinfo": ["-v"], "pdffonts": ["-v"], "ffmpeg": ["-version"], "ffprobe": ["-version"]}
_OBS = {}


def backend_of(real):
    """A tool whose executable lies inside this collection is a fixture, never a real renderer."""
    root = str(ROOT.resolve())
    return "fixture" if real == root or real.startswith(root + os.sep) else "real"


def _observe(name, exe):
    if not exe:
        return {"status": "unavailable", "reason": "not found"}
    real = os.path.realpath(exe)
    try:
        code, out, err = run_tool([exe, *VERSION_ARGS.get(name, ["--version"])], name)
    except Exit as error:
        return {"status": "unavailable", "reason": "timeout" if "timeout" in error.reason else "cannot start"}
    found = re.search(r"\d+\.\d+(?:\.\d+)?", (out + err).decode("utf-8", "replace")[:400])
    if code != 0 or not found:
        return {"status": "unavailable", "reason": "version not recognized"}
    return {"status": "available", "version": found.group(0), "exe_sha256": render.exe_sha256(real), "backend": backend_of(real),
            "_exe": exe}


def observe(name):
    key = (name, os.environ.get("PATH"))
    if key not in _OBS:
        _OBS[key] = _observe(name, shutil.which(name))
    return _OBS[key]


def observe_chrome():
    key = ("chrome", os.environ.get("AIARCH_CHROME"), os.environ.get("PATH"))
    if key not in _OBS:
        _OBS[key] = _observe("chrome", chrome.find_chrome())
    return _OBS[key]


def public(observation, name=None):
    """An observation without its private keys, as a receipt row piece."""
    out = {k: v for k, v in observation.items() if not k.startswith("_")}
    if name:
        out = {"name": name, **out}
    return out


def unavailable_reason(kind, name, observation):
    why = observation.get("reason", "")
    return f"{kind} unavailable: {name}" + ("" if why in ("", "not found") else f" ({why})")


def font_names(family):
    """(available, postscript names) of one declared family, asked of fontconfig."""
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,60}", family):
        return False, set()
    exe = shutil.which("fc-list")
    if not exe:
        raise Exit(3, "tool unavailable: fc-list")
    code, out, _ = run_tool([exe, f":family={family}", "family", "postscriptname"], "fc-list")
    names = set()
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split(":")
        names.add(parts[-1].strip() if len(parts) > 1 else "")
    names.discard("")
    return bool(out.strip()), {n.split("=")[-1].strip() for n in names}


def load_adapters(directory=None):
    directory = Path(directory) if directory else HERE / "adapters"
    adapters = {}
    for name in ("slides", "visuals", "video", "illustration"):
        doc = parse_json(read_capped(directory / f"{name}.json", 1 << 16), "adapter")
        if doc.get("adapter") != name or not isinstance(doc.get("dependencies"), list):
            raise Exit(2, f"invalid adapter: {name}")
        adapters[name] = doc
    return adapters


def declared_fonts(adapter):
    return [d["family"] for d in adapter["dependencies"] if d.get("kind") == "font"]


def check_deps(adapter, kind):
    """First missing dependency of `adapter` for `kind` as an Exit(3, reason); returns the observed pieces otherwise."""
    for dep in adapter["dependencies"]:
        if kind not in dep.get("only", [kind]):
            continue
        what = dep.get("kind")
        if what == "renderer" and dep["name"] == "chrome":
            seen = observe_chrome()
            if seen["status"] != "available":
                raise Exit(3, unavailable_reason("renderer", "chrome", seen))
        elif what in ("renderer", "tool"):
            seen = observe(dep["name"])
            if seen["status"] != "available":
                raise Exit(3, unavailable_reason(what, dep["name"], seen))
        elif what == "font":
            if not font_names(dep["family"])[0]:
                raise Exit(3, f"font unavailable: {dep['family']}")


# ---- png helpers --------------------------------------------------------------------------------------------------

def rgb_rows(data):
    """Scanlines of a png as RGB bytes (alpha dropped)."""
    head = checks.png_header(data)
    for line in checks.png_rows(data):
        if head[3] == 6:
            out = bytearray(len(line) // 4 * 3)
            out[0::3], out[1::3], out[2::3] = line[0::4], line[1::4], line[2::4]
            yield bytes(out)
        else:
            yield line


def png_mad(a, b):
    """Mean absolute difference per channel (0..255) of two pngs, or None when the sizes differ or one cannot be decoded."""
    ha, hb = checks.png_header(a), checks.png_header(b)
    if ha is None or hb is None or ha[:2] != hb[:2]:
        return None
    try:
        total = count = 0
        for ra, rb in zip(rgb_rows(a), rgb_rows(b)):
            if ra != rb:
                total += sum(abs(x - y) for x, y in zip(ra, rb))
            count += len(ra)
    except Exception:
        return None
    return total / max(1, count)


def png_diff_pixels(a, b, threshold=16):
    """How many pixels differ by more than `threshold` in any channel (None when not comparable)."""
    ha, hb = checks.png_header(a), checks.png_header(b)
    if ha is None or hb is None or ha[:2] != hb[:2]:
        return None
    try:
        count = 0
        for ra, rb in zip(rgb_rows(a), rgb_rows(b)):
            if ra != rb:
                for i in range(0, len(ra), 3):
                    if abs(ra[i] - rb[i]) > threshold or abs(ra[i + 1] - rb[i + 1]) > threshold or abs(ra[i + 2] - rb[i + 2]) > threshold:
                        count += 1
    except Exception:
        return None
    return count


# ---- host paths and corpora ---------------------------------------------------------------------------------------

EMAIL = re.compile(rb"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,}")


def host_strings(*paths):
    """(plain strings, boundary-delimited names) that must never appear in an artifact."""
    plain = set()
    for path in paths:
        if path:
            plain |= {str(path), os.path.realpath(str(path))}
    tmp = tempfile.gettempdir()
    plain |= {tmp, os.path.realpath(tmp), str(ROOT), str(ROOT.resolve())}
    home = os.environ.get("HOME", "")
    if home not in ("", "/"):
        plain |= {home, os.path.realpath(home)}
    names = set()
    try:
        names.add(getpass.getuser())
    except (KeyError, OSError, ImportError):
        pass
    try:
        names.add(socket.gethostname())
    except OSError:
        pass
    return {p for p in plain if len(p) >= 5}, {n for n in names if len(n) >= 3 and n != "localhost"}


def host_hit(blob, strings):
    """A short description of the first host path, name or email in `blob` (bytes), else None."""
    plain, names = strings
    for item in sorted(plain):
        if item.encode("utf-8", "replace") in blob:
            return "path"
    for name in sorted(names):
        if re.search(rb"(?<![A-Za-z0-9])" + re.escape(name.encode()) + rb"(?![A-Za-z0-9])", blob):
            return "name"
    if EMAIL.search(blob):
        return "email"
    return None


# ---- the Chrome session -------------------------------------------------------------------------------------------

FACTS_JS = """function(){
var keys=['slide','stat','row','step','edge','file','node','loop','commit','scene','add','del','kind'];
var sel=keys.map(function(k){return '[data-'+k+']';}).join(',');
var out={entries:[],scroll:[document.documentElement.scrollWidth,document.documentElement.scrollHeight],
 inner:[window.innerWidth,window.innerHeight],text:(document.documentElement.textContent||'').replace(/\\s+/g,' ').trim()};
Array.prototype.slice.call(document.querySelectorAll(sel),0,1500).forEach(function(e){
 var r=e.getBoundingClientRect(),d={};keys.forEach(function(k){if(e.dataset&&k in e.dataset)d[k]=e.dataset[k];});
 out.entries.push({tag:e.tagName.toLowerCase(),data:d,text:(e.textContent||'').replace(/\\s+/g,' ').trim(),
  rect:[r.left,r.top,r.width,r.height]});});
return out;}"""
SEEK_JS = ("function(ms){document.getAnimations().forEach(function(a){a.currentTime=ms;});return new Promise(function(done){"
           "requestAnimationFrame(function(){requestAnimationFrame(function(){done(1);});});});}")


class Session:
    """One headless Chrome on a tree (the design pipeline's Renderer). Use as a context manager; a failure to start or drive
    the browser is a host blocker (exit 3), never a traceback."""

    def __init__(self, tree, viewport):
        self.tree, self.viewport = str(tree), viewport
        self.r = None
        self.product = ""

    def __enter__(self):
        exe = chrome.find_chrome()
        if not exe:
            raise Exit(3, "renderer unavailable: chrome")
        if os.environ.get(RENDER_TIMEOUT_ENV):
            try:
                chrome.NAV_TIMEOUT = int(os.environ[RENDER_TIMEOUT_ENV])
            except ValueError:
                pass
        try:
            self.r = render.Renderer(self.tree)
        except render.Refused:
            raise Exit(2, "outside input")
        try:
            self.r.start()
            self.product = self.r.br.call("Browser.getVersion", session=False).get("product", "")
            self.r.load(render.ORIGIN, "media", self.viewport, first=True)
        except (render.NotRun, chrome.CDPError, OSError, TimeoutError, render.Refused, chrome.backends.Refused):
            self.close()
            raise Exit(3, "renderer unavailable: chrome")
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.r is not None:
            try:
                self.r.close()
            finally:
                self.r = None

    def guard(self, fn, *args):
        try:
            return fn(*args)
        except (chrome.CDPError, OSError, TimeoutError, ValueError, KeyError):
            raise Exit(3, "renderer unavailable: chrome")

    def facts(self):
        return self.guard(self.r.ev, FACTS_JS)

    def shot(self):
        return self.guard(self.r.shot, "page")

    def seek(self, ms):
        self.guard(self.r.ev, SEEK_JS, ms)

    def fonts(self):
        """Families the browser really used for the text on the page (CSS.getPlatformFontsForNode)."""
        def collect():
            call = self.r.br.call
            call("CSS.enable")
            root = call("DOM.getDocument", {"depth": -1})["root"]
            ids = []

            def walk(node):
                kids = node.get("children", [])
                if node.get("nodeType") == 1 and any(k.get("nodeType") == 3 and k.get("nodeValue", "").strip() for k in kids):
                    ids.append(node["nodeId"])
                for kid in kids:
                    walk(kid)
            walk(root)
            found = set()
            for node_id in ids[:800]:
                for font in call("CSS.getPlatformFontsForNode", {"nodeId": node_id}).get("fonts", []):
                    found.add(font.get("familyName", ""))
            return sorted(found)
        return self.guard(collect)

    def pdf(self):
        def collect():
            call = self.r.br.call
            got = call("Page.printToPDF", {"preferCSSPageSize": True, "printBackground": True, "displayHeaderFooter": False,
                                          "transferMode": "ReturnAsStream"})
            handle, data = got["stream"], bytearray()
            while True:
                part = call("IO.read", {"handle": handle, "size": 200000})
                data += base64.b64decode(part["data"]) if part.get("base64Encoded") else part["data"].encode("latin-1")
                if part.get("eof") or len(data) > 64 << 20:
                    break
            call("IO.close", {"handle": handle})
            return bytes(data)
        return self.guard(collect)

    def external(self):
        """Everything the page tried to reach outside its own origin.
        Same three-part computation as the design pipeline's render_tree (fetch log not allowed + own sink attempts + CSP reports)."""
        r = self.r
        r.harvest()
        r.settle(0.3)
        mine = render.own_attempts(list(r.br.sink.attempts), render.page_text(self.tree))
        return sorted(set([f"{e['method']} {e['url'].split('?')[0]}" for e in r.fetch_log if not e["allowed"]]
                          + [f"proxy {a}" for a in mine] + [f"csp {v}" for v in r.external + r.reports]))


# ---- specs --------------------------------------------------------------------------------------------------------

ID = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
COLOUR = re.compile(r"#[0-9a-f]{6}\Z")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def bad(what):
    raise Exit(2, f"invalid spec: {what}")


def need_dict(value, what, allowed, required=()):
    if not isinstance(value, dict):
        bad(what)
    for key in sorted(value):
        if key not in allowed:
            raise Exit(2, f"unknown key: {key}")
    for key in required:
        if key not in value:
            bad(f"missing {key}")
    return value


def need_list(value, what, lo, hi):
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        bad(f"{what} needs {lo} to {hi} entries")
    return value


def text(value, what, limit=None, empty=False):
    limit = limit or LIM["text"]
    if not isinstance(value, str) or (not value and not empty) or len(value) > limit or CONTROL.search(value):
        bad(what)
    return value


def ident(value, what="id"):
    if not isinstance(value, str) or not ID.match(value):
        raise Exit(2, f"invalid id: {str(value)[:40]}")
    return value


def number(value, what, lo, hi, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        bad(what)
    if integer and not isinstance(value, int):
        bad(what)
    return value


def size_of(value, lo, hi):
    found = re.fullmatch(r"(\d{2,4})x(\d{2,4})", value) if isinstance(value, str) else None
    if not found:
        bad("size")
    w, h = int(found.group(1)), int(found.group(2))
    if not (lo[0] <= w <= hi[0] and lo[1] <= h <= hi[1]):
        bad("size out of range")
    return w, h


def colour(value):
    if isinstance(value, str) and ("://" in value or "url(" in value.lower()):
        raise Exit(2, "remote asset")
    if not isinstance(value, str) or not COLOUR.match(value):
        raise Exit(2, "invalid colour")
    return value


def asset(spec_dir, value):
    """A data URI for a PNG or SVG that lives inside the spec's own directory (regular file, no link, no URL)."""
    if not isinstance(value, str) or not value or CONTROL.search(value):
        bad("logo")
    if SCHEME.match(value) or value.startswith("//"):
        raise Exit(2, "remote asset")
    parts = value.replace("\\", "/").split("/")
    if os.path.isabs(value) or "\\" in value or any(p in ("", ".", "..") for p in parts):
        raise Exit(2, "outside input")
    cursor = spec_dir
    for part in parts:
        cursor = os.path.join(cursor, part)
        try:
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                raise Exit(2, "outside input")
        except OSError:
            raise Exit(2, "outside input")
    if not regular_file(cursor):
        raise Exit(2, "outside input")
    data = read_capped(cursor, LIM["asset_bytes"], "invalid asset: too large")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.lstrip()[:5].lower() in (b"<svg ", b"<?xml") or data.lstrip()[:4].lower() == b"<svg":
        mime = "image/svg+xml"
    else:
        raise Exit(2, "invalid asset: not png or svg")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def unique(items, key, what):
    seen = set()
    for item in items:
        if item[key] in seen:
            bad(f"duplicate {what}: {item[key]}")
        seen.add(item[key])


def validate(kind, doc, spec_dir, fonts):
    keys = IFACE["spec_keys"][kind]
    need_dict(doc, "spec must be an object", keys["required"] + keys["optional"], keys["required"])
    spec = dict(doc)
    spec["_logo"] = asset(spec_dir, doc["logo"]) if "logo" in doc else None
    if kind == "slides":
        spec["font"] = doc.get("font", fonts[0])
        if not isinstance(spec["font"], str) or spec["font"] not in fonts:
            raise Exit(2, f"undeclared font: {str(spec['font'])[:60]}")
        text(doc["title"], "title")
        for slide in need_list(doc["slides"], "slides", 1, LIM["slides"]):
            need_dict(slide, "slide", ("id", "title", "bullets"), ("id", "title", "bullets"))
            ident(slide["id"])
            text(slide["title"], "slide title")
            for bullet in need_list(slide["bullets"], "bullets", 0, LIM["bullets"]):
                text(bullet, "bullet")
        unique(doc["slides"], "id", "id")
    elif kind == "infographic":
        text(doc["title"], "title")
        spec["canvas"] = size_of(doc["size"], (640, 480), (LIM["infographic_max"], LIM["infographic_max"]))
        colour(doc["bar_color"])
        for stat_ in need_list(doc["stats"], "stats", 1, LIM["stats"]):
            need_dict(stat_, "stat", ("id", "label", "value"), ("id", "label", "value"))
            ident(stat_["id"])
            text(stat_["label"], "label")
            number(stat_["value"], "value", 0, 1e9)
        unique(doc["stats"], "id", "id")
        if max(s["value"] for s in doc["stats"]) <= 0:
            bad("no positive value")
        if (spec["canvas"][1] - 300) // len(doc["stats"]) < 40:
            bad("too many stats for the size")
    elif kind == "visual-plan":
        ident(doc["plan_id"])
        steps = need_list(doc["steps"], "steps", 1, LIM["steps"])
        for step in steps:
            need_dict(step, "step", ("id", "title", "needs"), ("id", "title", "needs"))
            ident(step["id"])
            text(step["title"], "step title")
            need_list(step["needs"], "needs", 0, LIM["steps"])
        unique(steps, "id", "id")
        known = {s["id"] for s in steps}
        for step in steps:
            for need in step["needs"]:
                if need not in known:
                    raise Exit(2, f"unknown step: {str(need)[:40]}")
                if need == step["id"]:
                    bad("a step cannot need itself")
        order, done = [], set()
        while len(done) < len(steps):
            ready = [s["id"] for s in steps if s["id"] not in done and all(n in done for n in s["needs"])]
            if not ready:
                bad("cycle in needs")
            done.update(ready)
        for item in need_list(doc["files"], "files", 0, LIM["plan_files"]):
            need_dict(item, "file", ("path", "step"), ("path", "step"))
            if not isinstance(item["path"], str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", item["path"]) \
                    or item["path"].startswith("/") or ".." in item["path"].split("/"):
                bad("file path")
            if item["step"] not in known:
                raise Exit(2, f"unknown step: {str(item['step'])[:40]}")
        unique(doc["files"], "path", "path")
    elif kind == "visual-recap":
        text(doc["title"], "title")
    elif kind == "system-atlas":
        nodes = need_list(doc["nodes"], "nodes", 1, LIM["nodes"])
        for node in nodes:
            need_dict(node, "node", ("id", "kind", "label"), ("id", "kind", "label"))
            ident(node["id"])
            if not isinstance(node["kind"], str) or not re.fullmatch(r"[a-z0-9-]{1,20}", node["kind"]):
                bad("node kind")
            text(node["label"], "node label", 60)
        unique(nodes, "id", "id")
        known = {n["id"] for n in nodes}
        edges = need_list(doc["edges"], "edges", 0, LIM["edges"])
        for edge in edges:
            need_dict(edge, "edge", ("from", "to", "label"), ("from", "to", "label"))
            for end in ("from", "to"):
                if not isinstance(edge[end], str) or edge[end] not in known:
                    raise Exit(2, f"unknown node: {str(edge[end])[:40]}")
            text(edge["label"], "edge label", 60, empty=True)
        if len({(e["from"], e["to"]) for e in edges}) != len(edges):
            bad("duplicate edge")
        for loop in need_list(doc["loops"], "loops", 0, LIM["loops"]):
            for member in need_list(loop, "loop", 2, LIM["nodes"]):
                if not isinstance(member, str) or member not in known:
                    raise Exit(2, f"unknown node: {str(member)[:40]}")
    elif kind == "video":
        spec["canvas"] = size_of(doc["size"], (160, 90), tuple(LIM["video_max"]))
        number(doc["fps"], "fps", 1, 30, True)
        number(doc["seconds"], "seconds", 1, 5, True)
        if doc["fps"] * doc["seconds"] > LIM["video_frames"]:
            bad("too many frames")
        for scene in need_list(doc["scenes"], "scenes", 0, LIM["scenes"]):
            need_dict(scene, "scene", ("at", "text"), ("at", "text"))
            number(scene["at"], "scene time", 0, doc["seconds"])
            text(scene["text"], "scene text", 40)
    return spec


def load_spec(kind, path, fonts):
    """The validated spec: data only, capped, no duplicate or unknown keys, assets only from the spec's own directory."""
    if kind not in IFACE["spec_keys"]:
        raise Exit(2, "unknown kind")
    doc = parse_json(read_capped(path, LIM["spec_bytes"]).decode("utf-8", "replace"))
    spec_dir = os.path.dirname(os.path.abspath(path))
    return validate(kind, doc, spec_dir, fonts)


# ---- git (hardened) -----------------------------------------------------------------------------------------------

REV = re.compile(r"[0-9A-Za-z._/][0-9A-Za-z._/-]{0,99}\Z")


def git_run(repo, *args, extra=None):
    exe = shutil.which("git")
    if not exe:
        raise Exit(3, "tool unavailable: git")
    with tempfile.TemporaryDirectory() as home:
        env = child_env(home)
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1", LC_ALL="C")
        env.update(extra or {})
        try:
            code, out, err = documents.run_group([exe, "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                                  "-c", "core.quotepath=false", *args], timeout=tool_seconds(30), env=env, limit=8 << 20)
        except subprocess.TimeoutExpired:
            raise Exit(3, "tool unavailable: git (timeout)")
        except OSError:
            raise Exit(3, "tool unavailable: git")
    return code, out


def resolve_rev(repo, rev):
    if not isinstance(rev, str) or not REV.match(rev):
        raise Exit(2, f"unknown revision: {str(rev)[:40]}")
    code, out = git_run(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    full = out.decode("utf-8", "replace").strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", full):
        raise Exit(2, f"unknown revision: {rev[:40]}")
    return full


def recap_data(repo, base, head):
    """{base, head, files: {path: (added, deleted)}, commits: [(short sha, subject)]} from tracked, committed history only."""
    if not repo or not os.path.isdir(repo):
        raise Exit(2, "invalid spec: recap needs --repo, --base and --head")
    base, head = resolve_rev(repo, base), resolve_rev(repo, head)
    code, out = git_run(repo, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, head, "--")
    if code != 0:
        raise Exit(2, "unknown revision: range")
    files = {}
    for item in out.decode("utf-8", "replace").split("\0"):
        if item:
            added, deleted, path = item.split("\t", 2)
            files[path] = (added, deleted)
    code, out = git_run(repo, "log", "--format=%H%x00%s", "--no-show-signature", f"{base}..{head}", "--")
    commits = [(line.split("\0")[0][:7], line.split("\0", 1)[1]) for line in out.decode("utf-8", "replace").splitlines() if "\0" in line]
    if len(files) > LIM["recap_files"]:
        raise Exit(2, "range too large")
    return {"base": base, "head": head, "files": files, "commits": commits[:LIM["recap_commits"]]}


def png_close(a, b):
    """Byte-equal, or visually the same render: mean difference <= 2/255 and almost no strongly differing pixels."""
    if a == b:
        return True
    head = checks.png_header(a)
    mad = png_mad(a, b)
    if mad is None or mad > 2:
        return False
    strong = png_diff_pixels(a, b, 64)
    return strong is not None and strong <= 0.002 * head[0] * head[1]
