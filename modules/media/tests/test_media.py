"""Media module tests (A1..A14 of the build brief plus the acceptance amendments).

Every CLI call runs as `python3 -B media.py ...` with a scrubbed environment and a sitecustomize that makes any off-host
network call raise and leave a sentinel. The helper `cli` fails a test on a traceback or on that sentinel, so every test
asserts both. Rendered artifacts are built lazily, once per kind and per process (the project loop loads this file as a
plain module: no module-level fixture hook). AIARCH_REQUIRE_MEDIA=1 turns a skip for a missing tool into a failure.
"""
import ast
import atexit
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
EX = ROOT / "examples" / "media"
FIX = HERE / "fixtures"
PY = sys.executable
MEDIA, RUN, VERIFY = MOD / "media.py", EX / "run.py", EX / "verify_receipts.py"
REQUIRE = os.environ.get("AIARCH_REQUIRE_MEDIA") == "1"
ORIG_PATH = os.environ.get("PATH", os.defpath)
ORIG_HOME = os.environ.get("HOME", "")
CANARY = "ZZ-CANARY-" + hashlib.sha256(os.urandom(8)).hexdigest()[:16]
BASE_SHA = {"capabilities/registry.json": "1527e8c1074f8e67ca1ee6edad262a51d98850be563e429e4f058542ddd2e63d",
            "modules/contracts/mapping.json": "b21053e62ead5e142ac064ab64f50e11be697aa6aa293d3c08f3d89b7821eb58",
            "modules/contracts/sources.json": "5cbd6db1bf34a5f4a043d65a91425785f2846ac278cc6bb431654a2428003dd2"}
OBLIGATIONS = ["slides", "infographic", "visual-plan", "visual-recap", "system-atlas", "video", "illustration"]
KINDS = OBLIGATIONS[:6]
RENDERER_IDS = {"chrome-html", "chrome-pdf", "pdf-inspect", "ffmpeg-encode", "ffprobe-inspect", "remotion", "deck-runtime",
                "playwright", "hosted-plan-service", "atlas-build", "infographic-render", "plan-local-files",
                "illustration-backends"}
BLOCKED_RENDERERS = {"remotion": "licence", "deck-runtime": "not-shipped", "playwright": "no-driver",
                     "hosted-plan-service": "external-publication", "atlas-build": "not-shipped",
                     "infographic-render": "licence", "plan-local-files": "not-shipped", "illustration-backends": "provider"}
STATE = {}
SAVED = {}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def which(name):
    return shutil.which(name, path=ORIG_PATH)


NEEDED = ["git", "ffmpeg", "ffprobe", "pdfinfo", "pdftotext", "pdffonts", "fc-list"]


def have_chrome():
    return any(which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"))


def need_media():
    """Skip (or fail under AIARCH_REQUIRE_MEDIA=1) when a real renderer/tool is missing on this host."""
    missing = [n for n in NEEDED if not which(n)] + ([] if have_chrome() else ["chrome"])
    if missing:
        message = "media tools missing: " + ", ".join(missing)
        if REQUIRE:
            raise AssertionError(message + " (AIARCH_REQUIRE_MEDIA=1)")
        raise unittest.SkipTest(message)


class Sandbox:
    def __init__(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="media-test-"))
        t = Path(self.tmp)
        self.home, self.state, self.config, self.tmpdir = (str(t / n) for n in ("home", "state", "config", "tmp"))
        for p in (self.home, self.state, self.config, self.tmpdir, str(t / "home" / "notes")):
            os.makedirs(p, exist_ok=True)
        (t / "home" / "notes" / "corpus.md").write_text(f"private corpus {CANARY}\n")
        self.sentinel = str(t / "net-sentinel")
        self.counter = 0

    def fresh(self, name="d"):
        self.counter += 1
        path = os.path.join(self.tmp, f"{name}{self.counter}")
        os.makedirs(path)
        return path


def sandbox():
    if "sb" not in STATE:
        STATE["sb"] = Sandbox()
        atexit.register(lambda: shutil.rmtree(STATE["sb"].tmp, ignore_errors=True))
    return STATE["sb"]


def ensure_patched():
    if not SAVED:
        SAVED.update(connect=socket.socket.connect, create=socket.create_connection, lookup=socket.getaddrinfo)
    if socket.socket.connect is SAVED["connect"]:
        patch_sockets()


def patch_sockets():
    """In-process connections and lookups to anything but loopback raise; restored in every class's tearDownClass."""
    real_c, real_k, real_l = SAVED["connect"], SAVED["create"], SAVED["lookup"]
    local = ("127.0.0.1", "::1", "localhost", "")

    def addr_ok(address):
        return isinstance(address, tuple) and bool(address) and address[0] in local

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not addr_ok(address):
            raise OSError("test: off-host connection refused")
        return real_c(self, address)

    def create(address, *a, **k):
        if not addr_ok(address):
            raise OSError("test: off-host connection refused")
        return real_k(address, *a, **k)

    def lookup(host, *a, **k):
        if host not in local:
            raise OSError("test: off-host lookup refused")
        return real_l(host, *a, **k)
    socket.socket.connect, socket.create_connection, socket.getaddrinfo = connect, create, lookup


def unpatch_sockets():
    if SAVED:
        socket.socket.connect, socket.create_connection, socket.getaddrinfo = SAVED["connect"], SAVED["create"], SAVED["lookup"]


class R:
    def __init__(self, proc, parse):
        self.rc, self.out, self.err = proc.returncode, proc.stdout, proc.stderr
        self.doc = None
        if parse:
            try:
                self.doc = json.loads(self.out)
            except ValueError:
                self.doc = None

    @property
    def reason(self):
        return (self.doc or {}).get("reason")


def child_env(path=None, extra=None):
    sb = sandbox()
    env = {"PATH": path or ORIG_PATH, "LANG": "C.UTF-8", "HOME": sb.home, "XDG_STATE_HOME": sb.state,
           "XDG_CONFIG_HOME": sb.config, "TMPDIR": sb.tmpdir, "ZZ_CANARY_SECRET": CANARY,
           "AIARCH_TEST_SENTINEL": sb.sentinel, "PYTHONPATH": str(FIX / "check_sitecustomize"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra or {})
    return env


def run_script(script, *args, path=None, extra=None, timeout=420, parse=True, stdin=None):
    sb = sandbox()
    proc = subprocess.run([PY, "-B", str(script), *args], capture_output=True, text=True, timeout=timeout,
                          env=child_env(path, extra), cwd=sb.tmp, input=stdin)
    if "Traceback" in proc.stderr or "Traceback" in proc.stdout:
        raise AssertionError("traceback from " + Path(script).name + ":\n" + (proc.stderr or proc.stdout)[-1500:])
    if os.path.exists(sb.sentinel):
        raise AssertionError("network sentinel set: " + Path(sb.sentinel).read_text())
    return R(proc, parse)


def cli(*args, **kw):
    if "--format" not in args and "--json" not in args:
        args = (*args, "--format", "json")
    return run_script(MEDIA, *args, **kw)


def write_json(path, doc):
    Path(path).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return str(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def specs():
    return {k: read_json(EX / "specs" / f"{k}.json") for k in KINDS}


def spec_file(kind, doc, where=None):
    where = where or sandbox().fresh("spec")
    return write_json(Path(where) / f"{kind}.json", doc)


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_png(width, height, seed=1):
    """A valid, non-uniform RGB PNG that is not any rendered page."""
    rows = b"".join(b"\x00" + b"".join(bytes(((x * seed) & 255, (y * 3 + seed) & 255, (x ^ y) & 255)) for x in range(width))
                    for y in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def png_size(data):
    return struct.unpack(">II", data[16:24])


def bin_without(*names):
    """A PATH dir that holds a link to every /usr/bin tool except `names` (a missing tool, everything else intact)."""
    d = sandbox().fresh("bin")
    for entry in os.listdir("/usr/bin"):
        if entry not in names:
            try:
                os.symlink(os.path.join("/usr/bin", entry), os.path.join(d, entry))
            except OSError:
                pass
    return d


def stub_dir(**scripts):
    """A temp bin dir whose files are copies of fakebin stubs (name=stub file); returns the dir."""
    d = sandbox().fresh("stub")
    for name, stub in scripts.items():
        shutil.copy(FIX / "fakebin" / stub, os.path.join(d, name))
        os.chmod(os.path.join(d, name), 0o755)
    return d


# ---- artifacts built once per process -------------------------------------------------------------------------------

def kind_art(kind):
    """(out dir, kind dir, spec path) of a CLI `build` of the example spec of `kind`. Built once."""
    need_media()
    key = "art-" + kind
    if key not in STATE:
        sb = sandbox()
        if "kinds-out" not in STATE:
            STATE["kinds-out"] = os.path.join(sb.tmp, "kinds-out")
        out = STATE["kinds-out"]
        spec = str(EX / "specs" / f"{kind}.json")
        extra = []
        if kind == "visual-recap":
            repo, base, head = recap_repo()
            extra = ["--repo", repo, "--base", base, "--head", head]
        res = cli("build", "--kind", kind, "--spec", spec, "--out", out, *extra, timeout=300)
        if res.rc != 0:
            raise AssertionError(f"baseline build of {kind} failed: rc={res.rc} {res.doc or res.err[-300:]}")
        STATE[key] = (out, os.path.join(out, kind), spec, res)
    return STATE[key]


def git(repo, *argv, env=None):
    e = {"PATH": ORIG_PATH, "HOME": sandbox().home, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C"}
    e.update(env or {})
    proc = subprocess.run(["git", "-C", repo, "-c", "commit.gpgsign=false", *argv], capture_output=True, text=True, env=e, timeout=60)
    if proc.returncode:
        raise AssertionError("git failed: " + proc.stderr)
    return proc.stdout.strip()


EMAIL = "dev" + "." + "person" + "@" + "mail" + "-example" + ".org"


def make_repo(parent=None, commits=2):
    """A disposable repo with `commits` commits touching 3 files, an untracked canary file and a tracked file outside the range.
    Returns (repo, base, head)."""
    parent = parent or sandbox().fresh("recap")
    repo = os.path.join(parent, "work")
    os.makedirs(repo)
    env = {"GIT_AUTHOR_NAME": "Some Author", "GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_NAME": "Some Author",
           "GIT_COMMITTER_EMAIL": EMAIL}
    git(repo, "init", "-q", "-b", "main")
    Path(repo, "outside.txt").write_text("outside the range\n")
    Path(repo, "a.txt").write_text("one\ntwo\n")
    Path(repo, "b.txt").write_text("alpha\n")
    git(repo, "add", "."), git(repo, "commit", "-q", "-m", "start", env=env)
    base = git(repo, "rev-parse", "HEAD")
    Path(repo, "a.txt").write_text("one\ntwo\nthree\nfour\n")
    Path(repo, "c.txt").write_text("new file\nsecond\n")
    git(repo, "add", "."), git(repo, "commit", "-q", "-m", "grow a and add c", env=env)
    Path(repo, "b.txt").write_text("beta\n")
    git(repo, "add", "."), git(repo, "commit", "-q", "-m", "rewrite b", env=env)
    Path(repo, "untracked-" + CANARY[-8:] + ".txt").write_text("untracked " + CANARY + "\n")
    return repo, base, git(repo, "rev-parse", "HEAD")


def recap_repo():
    if "recap" not in STATE:
        STATE["recap"] = make_repo()
    return STATE["recap"]


def copy_art(kind):
    """A writable copy of the baseline artifact dir of `kind`; returns (copy dir, spec path)."""
    _, kdir, spec, _ = kind_art(kind)
    dest = os.path.join(sandbox().fresh("copy"), kind)
    shutil.copytree(kdir, dest)
    return dest, spec


def inspect_cmd(kind, spec, artifact, **kw):
    extra = []
    if kind == "visual-recap":
        repo, base, head = kw.pop("repo", None) or recap_repo()
        extra = ["--repo", repo, "--base", base, "--head", head]
    return cli("inspect", "--kind", kind, "--spec", spec, "--artifact", artifact, *extra, **kw)


def edit(artifact, fn, name="index.html"):
    p = Path(artifact) / name
    p.write_text(fn(p.read_text(encoding="utf-8")), encoding="utf-8")


def baseline():
    """The A1 journey: examples/media/run.py into a fresh dir. Built once; the second run feeds the determinism cells."""
    need_media()
    if "base" not in STATE:
        out = os.path.join(sandbox().fresh("run"), "out")
        res = run_script(RUN, "--offline", "--out", out, "--format", "json", timeout=420)
        STATE["base"] = (out, res)
    return STATE["base"]


def second_run():
    need_media()
    if "second" not in STATE:
        out = os.path.join(sandbox().fresh("run"), "out")
        res = run_script(VERIFY, "--keep", out, "--format", "json", timeout=600)
        STATE["second"] = (out, res)
    return STATE["second"]


class Cell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sandbox()
        ensure_patched()

    @classmethod
    def tearDownClass(cls):
        unpatch_sockets()

    def setUp(self):
        ensure_patched()

    def assertExit(self, res, rc, reason, **named):
        self.assertIsNotNone(res.doc, f"no JSON on stdout (rc={res.rc}): {res.out[:200]!r} {res.err[-300:]!r}")
        self.assertEqual(res.rc, rc, f"{res.doc}")
        self.assertEqual(res.reason, reason, f"{res.doc}")
        for key, value in named.items():
            self.assertEqual(res.doc.get(key), value)


# ------------------------------------------------------------------------------------------------ A1
class A1Journey(Cell):
    def test_run_example_prints_obligations(self):
        out, res = baseline()
        self.assertEqual(res.rc, 0, f"{res.doc} {res.err[-400:]}")
        rows = {r["id"]: r for r in res.doc["rows"]}
        self.assertEqual(list(rows), OBLIGATIONS)
        for kind in KINDS:
            self.assertEqual(rows[kind]["status"], "EXERCISED", rows[kind])
        self.assertEqual((rows["illustration"]["status"], rows["illustration"]["blocker_class"]), ("BLOCKED", "provider"))
        self.assertEqual(res.doc["interface"]["backend"], "fixture")
        lines = res.doc["lines"]
        self.assertEqual(len(lines), 7 + 1)
        self.assertTrue(any(l.startswith("interface: fixture") for l in lines), lines)

    def test_renderers_text_output_lists_every_renderer(self):
        res = cli("renderers", "--format", "text")
        self.assertEqual(res.rc, 0, res.err[-300:])
        for rid in RENDERER_IDS:
            self.assertRegex(res.out, rf"(?m)^{re.escape(rid)}\b")

    def test_verify_receipts_reinspects_and_prints_table(self):
        out, res = second_run()
        self.assertEqual(res.rc, 0, f"{res.doc} {res.err[-400:]}")
        self.assertEqual([r["id"] for r in res.doc["rows"]], OBLIGATIONS)
        self.assertEqual({r["id"] for r in res.doc["renderers"]} >= RENDERER_IDS, True)
        self.assertEqual(res.doc["reinspected"], KINDS)

    def test_offline_flag_required_writes_nothing(self):
        out = os.path.join(sandbox().fresh("run"), "never")
        res = run_script(RUN, "--out", out, "--format", "json")
        self.assertExit(res, 2, "offline flag required")
        self.assertFalse(os.path.exists(out))


# ------------------------------------------------------------------------------------------------ A2
def pdf_tool(tool, *args):
    proc = subprocess.run([which(tool), *args], capture_output=True, text=True, timeout=30)
    return proc.stdout


class A2Slides(Cell):
    def test_deck_pdf_png_and_fonts(self):
        out, kdir, spec, res = kind_art("slides")
        doc = specs()["slides"]
        self.assertTrue(os.path.isfile(os.path.join(kdir, "index.html")))
        pdf = os.path.join(kdir, "deck.pdf")
        info = pdf_tool("pdfinfo", pdf)
        self.assertRegex(info, rf"Pages:\s+{len(doc['slides'])}\b")
        self.assertRegex(info, r"Page size:\s+1440 x 810 pts")
        for i, slide in enumerate(doc["slides"], 1):
            self.assertIn(slide["title"], pdf_tool("pdftotext", "-f", str(i), "-l", str(i), pdf, "-"), f"page {i}")
        fonts = [l.split() for l in pdf_tool("pdffonts", pdf).splitlines()[2:] if l.strip()]
        self.assertTrue(fonts)
        for f in fonts:
            self.assertTrue(f[0].split("+")[-1].startswith("DejaVuSans"), f)
            self.assertIn("yes", f[-7:-4], f"not embedded: {f}")
        png = Path(kdir, "slide-1.png").read_bytes()
        self.assertEqual(png_size(png), (1920, 1080))
        self.assertTrue(res.doc["inspection"]["checks"])

    def test_inspect_more_slides_than_pages(self):
        _, kdir, spec, _ = kind_art("slides")
        doc = specs()["slides"]
        doc["slides"].append({"id": "s4", "title": "Fourth", "bullets": ["x"]})
        res = inspect_cmd("slides", spec_file("slides", doc), kdir)
        self.assertExit(res, 1, "page count mismatch")

    def test_swapped_slides_fail_per_page_text(self):
        need_media()
        doc = specs()["slides"]
        doc["slides"][0], doc["slides"][1] = doc["slides"][1], doc["slides"][0]
        out = sandbox().fresh("swap")
        built = cli("build", "--kind", "slides", "--spec", spec_file("slides", doc), "--out", out)
        self.assertEqual(built.rc, 0, built.doc)
        res = inspect_cmd("slides", str(EX / "specs" / "slides.json"), os.path.join(out, "slides"))
        self.assertExit(res, 1, f"text missing: {specs()['slides']['slides'][0]['id']} page 1")

    def test_serif_edit_is_an_undeclared_font(self):
        art, spec = copy_art("slides")
        edit(art, lambda t: re.sub(r'font-family:\s*"DejaVu Sans"', "font-family: serif", t))
        res = inspect_cmd("slides", spec, art)
        self.assertEqual((res.rc, (res.reason or "").split(":")[0]), (1, "undeclared font"), res.doc)

    def test_spec_font_other_than_declared_is_refused(self):
        doc = specs()["slides"]
        doc["font"] = "Aptos"
        res = cli("build", "--kind", "slides", "--spec", spec_file("slides", doc), "--out", sandbox().fresh("o"))
        self.assertExit(res, 2, "undeclared font: Aptos")

    def test_spec_font_default_is_accepted(self):
        need_media()
        doc = specs()["slides"]
        doc["font"] = "DejaVu Sans"
        res = cli("build", "--kind", "slides", "--spec", spec_file("slides", doc), "--out", sandbox().fresh("o"), timeout=300)
        self.assertEqual(res.rc, 0, res.doc)

    def test_forty_slides_get_a_result_not_a_traceback(self):
        need_media()
        doc = {"title": "Max deck", "slides": [{"id": f"s{i}", "title": f"Slide number {i}", "bullets": [f"point {i} " * 8] * 4}
                                               for i in range(40)]}
        res = cli("build", "--kind", "slides", "--spec", spec_file("slides", doc), "--out", sandbox().fresh("o"), timeout=400)
        self.assertEqual(res.rc, 0, f"{res.doc} {res.err[-300:]}")


# ------------------------------------------------------------------------------------------------ A3
class A3Infographic(Cell):
    def test_png_size_labels_and_bars(self):
        out, kdir, spec, res = kind_art("infographic")
        png = Path(kdir, "infographic.png").read_bytes()
        self.assertEqual(png_size(png), (1080, 1350))
        checks = {c["check"]: c for c in res.doc["inspection"]["checks"]}
        for name in ("png", "text", "geometry", "pixel", "fonts", "external"):
            self.assertEqual(checks[name]["result"], "pass", checks.get(name))
        html = Path(kdir, "index.html").read_text()
        for stat in specs()["infographic"]["stats"]:
            self.assertIn(f'data-stat="{stat["id"]}"', html)

    def test_dropping_a_bar_is_text_missing(self):
        art, spec = copy_art("infographic")
        first = specs()["infographic"]["stats"][0]["id"]
        edit(art, lambda t: re.sub(rf'<div class="bar" data-stat="{first}"[^>]*></div>', "", t))
        res = inspect_cmd("infographic", spec, art)
        self.assertExit(res, 1, f"text missing: {first}")

    def test_bar_from_the_wrong_field_is_geometry(self):
        art, spec = copy_art("infographic")
        stats = specs()["infographic"]["stats"]
        target = stats[1]["id"]
        edit(art, lambda t: re.sub(rf'(data-stat="{target}" style="width:)[0-9.]+px', r"\g<1>11px", t))
        res = inspect_cmd("infographic", spec, art)
        self.assertExit(res, 1, f"bar geometry: {target}")

    def test_maximum_canvas_gets_a_result(self):
        need_media()
        doc = {"title": "Big", "size": "2000x2000", "bar_color": "#2a6fdb",
               "stats": [{"id": f"k{i}", "label": f"Label {i}", "value": i + 1} for i in range(12)]}
        res = cli("build", "--kind", "infographic", "--spec", spec_file("infographic", doc), "--out", sandbox().fresh("o"),
                  timeout=400)
        self.assertEqual(res.rc, 0, f"{res.doc} {res.err[-300:]}")


# ------------------------------------------------------------------------------------------------ A4
class A4Plan(Cell):
    def test_nodes_edges_and_file_map(self):
        out, kdir, spec, res = kind_art("visual-plan")
        checks = {c["check"]: c for c in res.doc["inspection"]["checks"]}
        for name in ("nodes", "edges", "files", "png", "pixel"):
            self.assertEqual(checks[name]["result"], "pass", checks.get(name))

    def test_removed_edge(self):
        art, spec = copy_art("visual-plan")
        edit(art, lambda t: re.sub(r'<path data-edge="[^"]+"[^>]*>', "", t, count=1))
        self.assertExit(inspect_cmd("visual-plan", spec, art), 1, "edge set mismatch")

    def test_extra_node(self):
        art, spec = copy_art("visual-plan")
        edit(art, lambda t: t.replace("</body>", '<div class="node" data-step="zz-extra">zz</div></body>'))
        self.assertExit(inspect_cmd("visual-plan", spec, art), 1, "node set mismatch")

    def test_unknown_need_is_refused(self):
        doc = specs()["visual-plan"]
        doc["steps"][1]["needs"] = ["ghost"]
        res = cli("build", "--kind", "visual-plan", "--spec", spec_file("visual-plan", doc), "--out", sandbox().fresh("o"))
        self.assertExit(res, 2, "unknown step: ghost")

    def test_file_naming_unknown_step_is_refused(self):
        doc = specs()["visual-plan"]
        doc["files"][0]["step"] = "ghost"
        res = cli("build", "--kind", "visual-plan", "--spec", spec_file("visual-plan", doc), "--out", sandbox().fresh("o"))
        self.assertExit(res, 2, "unknown step: ghost")


# ------------------------------------------------------------------------------------------------ A5
class A5Recap(Cell):
    def test_rows_equal_numstat_and_nothing_private(self):
        need_media()
        repo, base, head = make_repo()
        out = sandbox().fresh("o")
        res = cli("build", "--kind", "visual-recap", "--spec", str(EX / "specs" / "visual-recap.json"), "--out", out,
                  "--repo", repo, "--base", base, "--head", head, timeout=300)
        self.assertEqual(res.rc, 0, f"{res.doc} {res.err[-300:]}")
        numstat = git(repo, "diff", "--numstat", "--no-renames", base, head).splitlines()
        self.assertEqual(len(numstat), 3)
        html = Path(out, "visual-recap", "index.html").read_text()
        for line in numstat:
            add, dele, path = line.split("\t")
            self.assertIn(f'data-file="{path}"', html)
            self.assertIn(f'data-add="{add}" data-del="{dele}"', html)
        everything = res.out + res.err + "".join(p.read_bytes().decode("latin1") for p in Path(out).rglob("*") if p.is_file())
        for secret in (CANARY, CANARY[-8:], EMAIL, "Some Author", "outside.txt", repo):
            self.assertNotIn(secret, everything)

    def test_commit_after_build_is_numstat_mismatch(self):
        need_media()
        repo, base, head = make_repo()
        out = sandbox().fresh("o")
        built = cli("build", "--kind", "visual-recap", "--spec", str(EX / "specs" / "visual-recap.json"), "--out", out,
                    "--repo", repo, "--base", base, "--head", head, timeout=300)
        self.assertEqual(built.rc, 0, built.doc)
        Path(repo, "a.txt").write_text("changed again\n")
        git(repo, "add", "."), git(repo, "commit", "-q", "-m", "late", env={"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x.invalid",
                                                                               "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x.invalid"})
        new_head = git(repo, "rev-parse", "HEAD")
        res = cli("inspect", "--kind", "visual-recap", "--spec", str(EX / "specs" / "visual-recap.json"),
                  "--artifact", os.path.join(out, "visual-recap"), "--repo", repo, "--base", base, "--head", new_head, timeout=300)
        self.assertExit(res, 1, "numstat mismatch: a.txt")

    def test_unknown_revision_is_refused(self):
        repo, base, head = make_repo()
        res = cli("build", "--kind", "visual-recap", "--spec", str(EX / "specs" / "visual-recap.json"), "--out", sandbox().fresh("o"),
                  "--repo", repo, "--base", "0" * 40, "--head", head)
        self.assertEqual((res.rc, (res.reason or "").split(":")[0]), (2, "unknown revision"), res.doc)


# ------------------------------------------------------------------------------------------------ A6
class A6Atlas(Cell):
    def test_dom_and_twin_equal_spec(self):
        out, kdir, spec, res = kind_art("system-atlas")
        doc = specs()["system-atlas"]
        twin = Path(kdir, "atlas.txt").read_text().splitlines()
        self.assertEqual(twin, sorted(twin))
        self.assertEqual(len(twin), len(doc["nodes"]) + len(doc["edges"]) + len(doc["loops"]))
        checks = {c["check"]: c for c in res.doc["inspection"]["checks"]}
        for name in ("nodes", "edges", "twin", "png", "pixel"):
            self.assertEqual(checks[name]["result"], "pass", checks.get(name))

    def test_twin_edited_by_one_line(self):
        art, spec = copy_art("system-atlas")
        edit(art, lambda t: t.replace(t.splitlines()[0], t.splitlines()[0] + " x", 1), name="atlas.txt")
        self.assertExit(inspect_cmd("system-atlas", spec, art), 1, "text twin mismatch")

    def test_loop_with_unknown_node_is_refused(self):
        doc = specs()["system-atlas"]
        doc["loops"].append(["gateway", "ghost"])
        res = cli("build", "--kind", "system-atlas", "--spec", spec_file("system-atlas", doc), "--out", sandbox().fresh("o"))
        self.assertExit(res, 2, "unknown node: ghost")


# ------------------------------------------------------------------------------------------------ A7
def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        if size < header:
            return
        yield kind.decode("latin1"), pos + header, pos + size
        pos += size


def mp4_avc1(data):
    """(top level box names, (w, h) of the avc1 sample entry)."""
    top = {k: (a, b) for k, a, b in boxes(data)}
    found = None
    if "moov" in top:
        stack = [top["moov"]]
        while stack:
            a, b = stack.pop()
            for kind, s, e in boxes(data, a, b):
                if kind in ("trak", "mdia", "minf", "stbl"):
                    stack.append((s, e))
                elif kind == "stsd":
                    for k2, s2, e2 in boxes(data, s + 8, e):
                        if k2 == "avc1":
                            found = struct.unpack(">HH", data[s2 + 24:s2 + 28])
    return set(top), found


def ffprobe_stream(path):
    proc = subprocess.run([which("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                           "stream=codec_name,width,height,pix_fmt,nb_read_frames", "-of", "json", path],
                          capture_output=True, text=True, timeout=60)
    return json.loads(proc.stdout)["streams"][0]


class A7Video(Cell):
    def test_probe_boxes_and_frames(self):
        out, kdir, spec, res = kind_art("video")
        doc = specs()["video"]
        mp4 = os.path.join(kdir, "video.mp4")
        stream = ffprobe_stream(mp4)
        w, h = (int(x) for x in doc["size"].split("x"))
        count = doc["fps"] * doc["seconds"]
        self.assertEqual((stream["codec_name"], stream["width"], stream["height"], stream["pix_fmt"], int(stream["nb_read_frames"])),
                         ("h264", w, h, "yuv420p", count))
        names, avc1 = mp4_avc1(Path(mp4).read_bytes())
        self.assertTrue({"ftyp", "moov", "mdat"} <= names, names)
        self.assertEqual(avc1, (w, h))
        frames = sorted(Path(kdir, "frames").glob("frame*.png"))
        self.assertEqual(len(frames), count)
        checks = {c["check"]: c for c in res.doc["inspection"]["checks"]}
        for name in ("probe", "container", "frames", "motion", "pixel"):
            self.assertEqual(checks[name]["result"], "pass", checks.get(name))
        self.assertNotEqual(frames[0].read_bytes(), frames[-1].read_bytes())

    def test_recorded_ffmpeg_argv(self):
        _, kdir, _, res = kind_art("video")
        argv = json.loads(Path(kdir, "inspection.json").read_text())["ffmpeg_argv"]
        self.assertIn("-protocol_whitelist", argv)
        self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file")
        self.assertFalse(any("://" in a for a in argv), argv)
        self.assertLess(argv.index("-protocol_whitelist"), argv.index("-i"))

    def test_ffmpeg_stub_sees_whitelist_and_no_url(self):
        need_media()
        d = stub_dir(ffmpeg="ffmpeg_logging.sh")
        res = cli("build", "--kind", "video", "--spec", str(EX / "specs" / "video.json"), "--out", sandbox().fresh("o"),
                  path=d + os.pathsep + ORIG_PATH, timeout=300)
        log = Path(d, "argv.log").read_text().split("\n")
        self.assertIn("-protocol_whitelist", log)
        self.assertEqual(log[log.index("-protocol_whitelist") + 1], "file")
        self.assertFalse(any("://" in a for a in log))

    def test_two_builds_are_byte_identical(self):
        out, kdir, _, _ = kind_art("video")
        out2, _ = second_run()
        for rel in ("video.mp4", "frames/frame0000.png", "frames/frame0019.png"):
            self.assertEqual(sha_file(os.path.join(kdir, rel)), sha_file(os.path.join(out2, "video", rel)), rel)

    def test_png_kinds_are_byte_identical_across_runs(self):
        out2, _ = second_run()
        for kind, name in (("infographic", "infographic.png"), ("visual-plan", "visual-plan.png"),
                           ("system-atlas", "system-atlas.png"), ("slides", "slide-1.png")):
            _, kdir, _, _ = kind_art(kind)
            self.assertEqual(sha_file(os.path.join(kdir, name)), sha_file(os.path.join(out2, kind, name)), kind)

    def test_ffmpeg_that_writes_nothing(self):
        need_media()
        d = stub_dir(ffmpeg="ffmpeg_null.sh")
        res = cli("build", "--kind", "video", "--spec", str(EX / "specs" / "video.json"), "--out", sandbox().fresh("o"),
                  path=d + os.pathsep + ORIG_PATH, timeout=300)
        self.assertExit(res, 1, "no output")

    def test_a_different_clip_is_a_frame_mismatch(self):
        _, kdir, spec, _ = kind_art("video")
        other = specs()["video"]
        other["scenes"] = [{"at": 0.0, "text": "Completely different"}] + other["scenes"][:1]
        other["scenes"][1]["at"] = 0.5
        out = sandbox().fresh("other")
        built = cli("build", "--kind", "video", "--spec", spec_file("video", other), "--out", out, timeout=300)
        self.assertEqual(built.rc, 0, built.doc)
        art, _ = copy_art("video")
        shutil.copy(os.path.join(out, "video", "video.mp4"), os.path.join(art, "video.mp4"))
        res = inspect_cmd("video", spec, art, timeout=300)
        self.assertEqual(res.rc, 1, res.doc)
        self.assertRegex(res.reason, r"^(frame mismatch: \d+|frame count mismatch)$")

    def test_a_spec_without_animation_has_no_motion(self):
        need_media()
        doc = specs()["video"]
        doc["scenes"] = []
        res = cli("build", "--kind", "video", "--spec", spec_file("video", doc), "--out", sandbox().fresh("o"), timeout=300)
        self.assertExit(res, 1, "no motion")


# ------------------------------------------------------------------------------------------------ A8
def providers_file(describe_args=(), sentinel=None, name="stub", extra_entry=None, describe_path=None):
    sb = sandbox()
    d = sb.fresh("prov")
    sentinel = sentinel or os.path.join(d, "spawned")
    stub = str(FIX / "fake_provider.py")
    entry = {"name": name, "argv": [PY, "-B", stub, "--sentinel", sentinel],
             "describe_argv": describe_path or [PY, "-B", stub, "--describe", *describe_args]}
    entry.update(extra_entry or {})
    return write_json(Path(d) / "providers.json", {"providers": [entry]}), sentinel, d


def illustrate(prov, *flags, prompt=None, out=None, extra=None, path=None, name="stub"):
    sb = sandbox()
    prompt = prompt or write_text(os.path.join(sb.fresh("pr"), "prompt.txt"), "a synthetic prompt about nothing real")
    out = out or sb.fresh("ill")
    return cli("illustrate", "--provider", name, "--providers", prov, "--prompt-file", prompt, "--out", out, *flags,
               extra=extra, path=path, timeout=120), out


def write_text(path, text):
    Path(path).write_text(text)
    return path


class A8Illustration(Cell):
    def test_stub_generates_a_fixture_png_and_blocks_the_obligation(self):
        prov, sentinel, _ = providers_file()
        res, out = illustrate(prov, "--allow-provider")
        self.assertEqual(res.rc, 0, f"{res.doc} {res.err[-300:]}")
        self.assertEqual((res.doc["backend"], res.doc["backend_reason"]), ("fixture", "fixture generation"))
        art = res.doc["artifact"][0]
        png = Path(out, art["path"]).read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(art["sha256"], hashlib.sha256(png).hexdigest())
        self.assertTrue(os.path.exists(sentinel))

    def test_no_allow_provider_spawns_nothing(self):
        prov, sentinel, _ = providers_file()
        res, _ = illustrate(prov)
        self.assertExit(res, 2, "provider not allowed")
        self.assertFalse(os.path.exists(sentinel))

    def test_network_and_cost_refusals_before_generate(self):
        for args, reason in ((("--network", "true"), "provider needs network"), (("--cost", "paid"), "provider costs money"),
                             (("--cost", "quota"), "provider costs money")):
            with self.subTest(args=args):
                prov, sentinel, _ = providers_file(args)
                res, _ = illustrate(prov, "--allow-provider")
                self.assertExit(res, 4, reason)
                self.assertFalse(os.path.exists(sentinel))

    def test_describe_wins_over_providers_json_claims(self):
        prov, sentinel, _ = providers_file(("--network", "true"), extra_entry={"claims": {"network": False, "cost": "none"}})
        res, _ = illustrate(prov, "--allow-provider")
        self.assertExit(res, 4, "provider needs network")
        self.assertFalse(os.path.exists(sentinel))

    def test_hanging_describe_is_not_observed_and_leaves_no_process(self):
        d = stub_dir(hang="hang.sh")
        prov, sentinel, _ = providers_file(describe_path=[os.path.join(d, "hang")])
        t0 = time.monotonic()
        res, _ = illustrate(prov, "--allow-provider", extra={"AIARCH_MEDIA_TOOL_TIMEOUT": "1"})
        self.assertLess(time.monotonic() - t0, 5.5)
        self.assertExit(res, 4, "provider not observed")
        self.assertFalse(os.path.exists(sentinel))
        pid = int(Path(d, "grandchild.pid").read_text())
        time.sleep(0.3)
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    def test_garbage_describe_is_not_observed(self):
        prov, sentinel, _ = providers_file(("--mode", "garbage"))
        res, _ = illustrate(prov, "--allow-provider")
        self.assertExit(res, 4, "provider not observed")
        self.assertFalse(os.path.exists(sentinel))

    def test_child_env_is_allow_listed(self):
        prov, _, _ = providers_file()
        res, out = illustrate(prov, "--allow-provider")
        self.assertEqual(res.rc, 0, res.doc)
        names = set(next(Path(out).rglob("*.env")).read_text().split())
        self.assertTrue(names <= {"PATH", "LANG", "HOME", "TMPDIR", "LC_CTYPE"}, names)
        blob = res.out + res.err + "".join(p.read_bytes().decode("latin1") for p in Path(out).rglob("*") if p.is_file())
        self.assertNotIn("ZZ_CANARY_SECRET", blob)
        self.assertNotIn(CANARY, blob)

    def test_licence_record_is_derived_from_provenance(self):
        res = cli("renderers", "--providers", providers_file()[0])
        self.assertEqual(res.rc, 0, res.doc)
        rec = next(r for r in res.doc["renderers"] if r["id"] == "illustration-backends")
        self.assertEqual(rec["status"], "blocked")
        lic = res.doc["illustration_licence"]
        self.assertEqual((lic["licence"], lic["notice_required"], lic["licence_gate"]), ("MIT", True, "pending U02 row"))
        prov = (MOD / "PROVENANCE.md").read_text()
        self.assertIn(lic["licence_sha256"], prov)
        self.assertIn(lic["notice_sha256"], prov)


# ------------------------------------------------------------------------------------------------ A9
def rows_by_id(res):
    return {r["id"]: r for r in res.doc["rows"]}


class A9Enablement(Cell):
    def test_enable_one_keeps_seven_rows(self):
        need_media()
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "infographic", timeout=300)
        self.assertEqual(res.rc, 0, res.doc)
        rows = rows_by_id(res)
        self.assertEqual(list(rows), OBLIGATIONS)
        self.assertEqual(rows["infographic"]["status"], "EXERCISED")
        for kind in OBLIGATIONS:
            if kind != "infographic":
                self.assertEqual((rows[kind]["status"], rows[kind]["blocker_class"]), ("BLOCKED", "not-enabled"), kind)

    def test_require_media_exit_codes(self):
        out = sandbox().fresh("o")
        res = cli("receipts", "--out", out, "--enable", "illustration", extra={"AIARCH_REQUIRE_MEDIA": "1"})
        self.assertEqual(res.rc, 3, res.doc)
        self.assertEqual(len(res.doc["rows"]), 7)

    def test_require_media_with_only_the_provider_blocker_is_zero(self):
        need_media()
        res = cli("receipts", "--out", sandbox().fresh("o"), extra={"AIARCH_REQUIRE_MEDIA": "1"}, timeout=420)
        self.assertEqual(res.rc, 0, f"{res.doc}")
        self.assertEqual([r["id"] for r in res.doc["rows"] if r["status"] == "BLOCKED"], ["illustration"])

    def test_missing_ffmpeg_blocks_only_video(self):
        need_media()
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "video,infographic", path=bin_without("ffmpeg"), timeout=300)
        rows = rows_by_id(res)
        self.assertEqual((rows["video"]["status"], rows["video"]["reason"]), ("BLOCKED", "renderer unavailable: ffmpeg"))
        self.assertEqual(rows["infographic"]["status"], "EXERCISED", rows["infographic"])

    def test_missing_pdftotext_blocks_only_slides(self):
        need_media()
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "slides,infographic", path=bin_without("pdftotext"), timeout=300)
        rows = rows_by_id(res)
        self.assertEqual((rows["slides"]["status"], rows["slides"]["reason"]), ("BLOCKED", "tool unavailable: pdftotext"))
        self.assertEqual(rows["infographic"]["status"], "EXERCISED", rows["infographic"])

    def test_missing_chrome_blocks_six_rows(self):
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
        res = cli("receipts", "--out", sandbox().fresh("o"), path=bin_without(*names),
                  extra={"AIARCH_CHROME": "/nonexistent/chrome"}, timeout=120)
        rows = rows_by_id(res)
        for kind in KINDS:
            self.assertEqual((rows[kind]["status"], rows[kind]["reason"]), ("BLOCKED", "renderer unavailable: chrome"), kind)
        self.assertEqual(rows["illustration"]["blocker_class"], "provider")

    def test_missing_font_blocks_that_adapter(self):
        adapters = os.path.join(sandbox().fresh("ad"), "adapters")
        shutil.copytree(MOD / "adapters", adapters)
        p = Path(adapters, "visuals.json")
        p.write_text(p.read_text().replace("DejaVu Sans", "NoSuchFont"))
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "infographic", "--adapters", adapters)
        rows = rows_by_id(res)
        self.assertEqual((rows["infographic"]["status"], rows["infographic"]["reason"]), ("BLOCKED", "font unavailable: NoSuchFont"))

    def test_renderers_lists_blocked_rows_and_spawns_nothing(self):
        d = stub_dir(remotion="sentinel.sh", playwright="sentinel.sh", bun="sentinel.sh", node="sentinel.sh")
        res = cli("renderers", "--json", path=d + os.pathsep + ORIG_PATH)
        self.assertEqual(res.rc, 0, res.doc)
        rows = {r["id"]: r for r in res.doc["renderers"]}
        self.assertEqual(set(rows), RENDERER_IDS)
        for rid, klass in BLOCKED_RENDERERS.items():
            self.assertEqual((rows[rid]["status"], rows[rid]["blocker_class"]), ("blocked", klass), rid)
            self.assertTrue(rows[rid]["setup"])
        self.assertFalse(os.path.exists(os.path.join(d, "ran")))

    def test_obligation_rows_name_their_upstream_renderers(self):
        need_media()
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "infographic")
        rows = rows_by_id(res)
        for kind, row in rows.items():
            self.assertIsInstance(row["upstream"], list, kind)
            self.assertTrue(set(row["upstream"]) <= RENDERER_IDS | {"remotion"}, row["upstream"])
        self.assertEqual(rows["infographic"]["upstream"], ["infographic-render"])
        self.assertIn("atlas-build", rows["system-atlas"]["upstream"])
        self.assertIn("plan-local-files", rows["visual-plan"]["upstream"])
        self.assertIn("deck-runtime", rows["slides"]["upstream"])
        self.assertIn("remotion", rows["video"]["upstream"])


# ------------------------------------------------------------------------------------------------ A10
def bad_spec_cases():
    s = specs()
    cases = []

    def add(label, kind, doc, reason, files=None, raw=None):
        cases.append((label, kind, doc, reason, files or {}, raw))
    inf = s["infographic"]
    add("dotdot", "infographic", dict(inf, logo="../x.png"), "outside input")
    add("absolute", "infographic", dict(inf, logo="/etc/hostname"), "outside input")
    add("symlink", "infographic", dict(inf, logo="link.png"), "outside input", files={"link.png": ("link", "target.png"), "target.png": make_png(8, 8)})
    add("https", "infographic", dict(inf, logo="https://example.invalid/x.png"), "remote asset")
    add("scheme-relative", "infographic", dict(inf, logo="//host/x.png"), "remote asset")
    add("file-scheme", "infographic", dict(inf, logo="file:///etc/hostname"), "remote asset")
    add("css-url", "infographic", dict(inf, bar_color="url(http://203.0.113.9/x)"), "remote asset")
    add("bad-colour", "infographic", dict(inf, bar_color="red"), "invalid colour")
    add("unknown-key", "infographic", dict(inf, extra_field=1), "unknown key: extra_field")
    add("duplicate-key", "infographic", None, "duplicate key", raw='{"title": "a", "title": "b"}')
    add("too-large", "infographic", None, "spec too large", raw=json.dumps({"title": "x" * (300 * 1024)}))
    return cases


class A10Untrusted(Cell):
    def test_refusals_write_no_artifact(self):
        for label, kind, doc, reason, files, raw in bad_spec_cases():
            with self.subTest(label):
                d = sandbox().fresh("spec")
                for name, content in files.items():
                    if isinstance(content, tuple):
                        os.symlink(content[1], os.path.join(d, name))
                    else:
                        Path(d, name).write_bytes(content)
                p = os.path.join(d, "spec.json")
                Path(p).write_text(raw if raw is not None else json.dumps(doc))
                out = os.path.join(sandbox().fresh("o"), "out")
                res = cli("build", "--kind", kind, "--spec", p, "--out", out)
                self.assertExit(res, 2, reason)
                self.assertFalse(os.path.exists(os.path.join(out, kind)), "an artifact was written")

    def test_script_title_is_literal_text_in_every_builder(self):
        need_media()
        payload = "<script>window.__pwned=1</script>"
        s = specs()
        s["slides"]["title"] = payload
        s["slides"]["slides"][0]["title"] = payload
        s["infographic"]["title"] = payload
        s["infographic"]["stats"][0]["label"] = payload
        s["visual-plan"]["steps"][0]["title"] = payload
        s["visual-recap"]["title"] = payload
        s["system-atlas"]["nodes"][0]["label"] = payload
        s["video"]["scenes"][0]["text"] = payload
        repo = make_repo()
        probe = load("test_media_probe", FIX / "check_probe.py")
        for kind in KINDS:
            with self.subTest(kind):
                out = sandbox().fresh("o")
                extra = ["--repo", repo[0], "--base", repo[1], "--head", repo[2]] if kind == "visual-recap" else []
                res = cli("build", "--kind", kind, "--spec", spec_file(kind, s[kind]), "--out", out, *extra, timeout=300)
                self.assertEqual(res.rc, 0, f"{kind}: {res.doc}")
                text, pwned = probe.main_world(os.path.join(out, kind))
                self.assertIn(payload, text, kind)
                self.assertEqual(pwned, "undefined", kind)


# ------------------------------------------------------------------------------------------------ A11
class A11Offline(Cell):
    def test_external_image_is_an_external_request(self):
        art, spec = copy_art("infographic")
        edit(art, lambda t: t.replace("</body>", '<img src="http://203.0.113.9/x.png"></body>'))
        self.assertExit(inspect_cmd("infographic", spec, art), 1, "external request")

    def test_external_is_empty_for_every_kind(self):
        for kind in KINDS:
            _, kdir, _, res = kind_art(kind)
            checks = {c["check"]: c for c in res.doc["inspection"]["checks"]}
            self.assertEqual(checks["external"]["result"], "pass", kind)
            self.assertEqual(checks["external"]["measured"], [], kind)

    def test_hanging_pdftotext_is_unavailable_within_five_seconds(self):
        need_media()
        d = stub_dir(pdftotext="hang.sh")
        t0 = time.monotonic()
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "slides", path=d + os.pathsep + ORIG_PATH,
                  extra={"AIARCH_MEDIA_TOOL_TIMEOUT": "1"}, timeout=60)
        self.assertLess(time.monotonic() - t0, 5.5)
        self.assertEqual(rows_by_id(res)["slides"]["reason"], "tool unavailable: pdftotext (timeout)")
        pid = int(Path(d, "grandchild.pid").read_text())
        time.sleep(0.3)
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    def test_static_scan_and_env_reads(self):
        banned = re.compile(r"\burllib\b|http\.client|\bssl\b|\brequests\b|shell\s*=\s*True|os\.system")
        allowed = {"PATH", "HOME", "XDG_STATE_HOME", "AIARCH_CHROME", "AIARCH_MEDIA_TOOL_TIMEOUT", "AIARCH_RENDER_TIMEOUT",
                   "AIARCH_REQUIRE_MEDIA"}
        files = [p for d in (MOD, EX) for p in d.rglob("*") if p.is_file() and "tests" not in p.relative_to(d).parts
                 and "__pycache__" not in p.parts and p.suffix in (".py", ".json", ".md")]
        self.assertTrue(len(files) > 8)
        for p in files:
            self.assertIsNone(banned.search(p.read_text(encoding="utf-8")), f"{p.name}: {banned.search(p.read_text())}")
        for p in (q for q in files if q.suffix == ".py"):
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                key = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    f = node.func
                    base = ast.unparse(f.value)
                    if (base in ("os.environ", "environ") and f.attr in ("get", "pop", "setdefault")) or (base == "os" and f.attr == "getenv"):
                        key = ast.unparse(node.args[0]) if node.args else "?"
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in ("os.environ", "environ"):
                    key = ast.unparse(node.slice)
                if key is not None:
                    lit = key.strip("'\"")
                    consts = {"TIMEOUT_ENV", "REQUIRE_ENV", "CHROME_ENV", "TOOL_TIMEOUT_ENV", "RENDER_TIMEOUT_ENV"}
                    self.assertTrue(lit in allowed or key.split(".")[-1] in consts, f"{p.name}: env read {key}")
            text = p.read_text()
            self.assertIsNone(re.search(r"dict\(os\.environ\)|os\.environ\.copy\(\)|\*\*os\.environ", text), p.name)


# ------------------------------------------------------------------------------------------------ A12
def forbidden_strings(out):
    sb = sandbox()
    items = {"out": os.path.realpath(out), "tmp": sb.tmp, "tmpdir": sb.tmpdir, "home": sb.home, "root": str(ROOT.resolve()),
             CANARY: CANARY, "user": getpass.getuser(), "host": socket.gethostname()}
    return items


class A12HostPaths(Cell):
    def test_no_host_path_or_corpus_in_any_output(self):
        out, res = baseline()
        sb = sandbox()
        pieces = [res.out, res.err]
        for p in Path(out).rglob("*"):
            if p.is_file() and ".git" not in p.relative_to(out).parts and "recap-repo" not in p.relative_to(out).parts:
                pieces.append(p.read_bytes().decode("latin1"))
        blob = "\n".join(pieces)
        for label, needle in forbidden_strings(out).items():
            if label in ("user", "host"):
                self.assertIsNone(re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", blob), label)
            else:
                self.assertNotIn(needle, blob, label)
        self.assertNotIn(os.path.realpath(sb.tmp), blob)
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob))
        for tool, args in (("pdfinfo", [os.path.join(out, "slides", "deck.pdf")]), ("pdftotext", [os.path.join(out, "slides", "deck.pdf"), "-"]),
                           ("ffprobe", ["-v", "error", "-show_entries", "format_tags", "-of", "default=nw=1", os.path.join(out, "video", "video.mp4")])):
            text = pdf_tool(tool, *args)
            self.assertNotIn(sb.tmp, text, tool)
            self.assertNotIn(sb.home, text, tool)
            self.assertNotIn(str(ROOT), text, tool)
            self.assertNotIn(CANARY, text, tool)

    def test_title_holding_the_temp_root_is_refused(self):
        need_media()
        doc = specs()["infographic"]
        doc["title"] = "report in " + sandbox().tmpdir
        res = cli("build", "--kind", "infographic", "--spec", spec_file("infographic", doc), "--out", sandbox().fresh("o"), timeout=300)
        self.assertExit(res, 1, "host path in artifact")


# ------------------------------------------------------------------------------------------------ A13
def tamper_copy():
    """A copy of the baseline out dir without the recap repo's object store weight: returns the new out dir."""
    out, _ = baseline()
    dest = os.path.join(sandbox().fresh("tamper"), "out")
    shutil.copytree(out, dest)
    return dest


def no_chrome_env():
    return {"AIARCH_CHROME": "/nonexistent/chrome"}


def no_chrome_path():
    return bin_without("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


def verify(out, *args, structural=False, **kw):
    kw.setdefault("timeout", 420)
    if structural:  # a structural failure must not need a browser at all
        kw.update(path=no_chrome_path(), extra=no_chrome_env())
    return run_script(VERIFY, out, "--format", "json", *args, **kw)


def update_row(out, rid, fn):
    p = Path(out, "receipts.json")
    doc = json.loads(p.read_text())
    for row in doc["rows"]:
        if row["id"] == rid:
            fn(row)
    p.write_text(json.dumps(doc, indent=1))


class A13Verifier(Cell):
    def test_tampered_artifact_byte(self):
        out = tamper_copy()
        target = Path(out, "infographic", "infographic.png")
        data = bytearray(target.read_bytes())
        data[-20] ^= 0xFF
        target.write_bytes(bytes(data))
        self.assertExit(verify(out, structural=True), 1, "sha mismatch: infographic")

    def test_inspect_needs_no_builder_metadata(self):
        art, spec = copy_art("infographic")
        Path(art, "inspection.json").write_text("not json")
        self.assertEqual(inspect_cmd("infographic", spec, art).rc, 0)
        os.remove(os.path.join(art, "inspection.json"))
        self.assertEqual(inspect_cmd("infographic", spec, art).rc, 0)

    def test_status_edited_without_artifacts(self):
        out = tamper_copy()

        def lie(row):
            row.update(status="EXERCISED", blocker_class=None, reason=None, artifact=[])
        update_row(out, "illustration", lie)
        self.assertExit(verify(out, structural=True), 1, "unsupported claim: illustration")

    def test_deleted_row(self):
        out = tamper_copy()
        p = Path(out, "receipts.json")
        doc = json.loads(p.read_text())
        doc["rows"] = [r for r in doc["rows"] if r["id"] != "system-atlas"]
        p.write_text(json.dumps(doc))
        self.assertExit(verify(out, structural=True), 1, "missing row: system-atlas")

    def test_blocked_row_reasons_must_be_in_the_vocabulary(self):
        for reason in (None, "because I said so"):
            with self.subTest(reason=reason):
                out = tamper_copy()
                update_row(out, "illustration", lambda row: row.update(reason=reason))
                self.assertExit(verify(out, structural=True), 1, "invalid reason: illustration")

    def test_fixture_row_marked_exercised(self):
        out = tamper_copy()
        update_row(out, "slides", lambda row: row["renderer"].update(backend="fixture"))
        self.assertExit(verify(out, structural=True), 1, "fixture generation: slides")

    def test_replacing_a_png_by_another_valid_png_is_caught_for_every_rendered_kind(self):
        for kind, name in (("slides", "slide-1.png"), ("infographic", "infographic.png"), ("visual-plan", "visual-plan.png"),
                           ("visual-recap", "visual-recap.png"), ("system-atlas", "system-atlas.png")):
            with self.subTest(kind):
                out = tamper_copy()
                target = Path(out, kind, name)
                width, height = png_size(target.read_bytes())
                target.write_bytes(make_png(width, height))

                def fix(row, rel=f"{kind}/{name}", blob=target.read_bytes()):
                    for art in row["artifact"]:
                        if art["path"] == rel:
                            art.update(sha256=hashlib.sha256(blob).hexdigest(), bytes=len(blob))
                update_row(out, kind, fix)
                res = verify(out, "--only", kind)
                self.assertEqual(res.rc, 1, res.doc)
                self.assertRegex(res.reason, r"^(pixel mismatch|bar pixels?): ")

    def test_replacing_the_video_with_another_consistent_clip_is_caught(self):
        out = tamper_copy()
        other = specs()["video"]
        other["scenes"] = [{"at": 0.0, "text": "Another clip"}]
        built = sandbox().fresh("o")
        res = cli("build", "--kind", "video", "--spec", spec_file("video", other), "--out", built, timeout=300)
        self.assertEqual(res.rc, 0, res.doc)
        shutil.rmtree(os.path.join(out, "video", "frames"))
        shutil.copytree(os.path.join(built, "video", "frames"), os.path.join(out, "video", "frames"))
        shutil.copy(os.path.join(built, "video", "video.mp4"), os.path.join(out, "video", "video.mp4"))

        def fix(row):
            for art in row["artifact"]:
                blob = Path(out, art["path"]).read_bytes() if os.path.exists(os.path.join(out, art["path"])) else None
                if blob is not None:
                    art.update(sha256=hashlib.sha256(blob).hexdigest(), bytes=len(blob))
        update_row(out, "video", fix)
        res = verify(out, "--only", "video")
        self.assertEqual(res.rc, 1, res.doc)
        self.assertRegex(res.reason, r"^(pixel mismatch: |frame mismatch: )")

    def test_conflict_follows_the_registry_claim_for_every_row(self):
        out, _ = baseline()
        doc = read_json(os.path.join(out, "receipts.json"))
        registry = {c["id"]: c for c in read_json(os.path.join(ROOT, "capabilities", "registry.json"))["capabilities"]}
        for row in doc["rows"]:
            support = registry[row["registry_claim"]["id"]]["support"]
            self.assertEqual(row["registry_claim"]["support"], support, row["id"])
            self.assertEqual(row["conflict"], (row["status"] == "EXERCISED") != (support != "planned"), row["id"])
            if row["status"] == "EXERCISED" and support == "partial":
                self.assertFalse(row["conflict"], row["id"])

    def test_stable_sha256_is_equal_across_runs_and_sensitive(self):
        out1, _ = baseline()
        out2, res2 = second_run()
        media = load("test_media_main", MEDIA)
        d1, d2 = read_json(os.path.join(out1, "receipts.json")), read_json(os.path.join(out2, "receipts.json"))
        self.assertEqual(media.stable_sha256(d1), media.stable_sha256(d2))
        changed = json.loads(json.dumps(d1))
        changed["rows"][0]["reason"] = "changed"
        self.assertNotEqual(media.stable_sha256(d1), media.stable_sha256(changed))
        noisy = json.loads(json.dumps(d1))
        noisy["run_id"] = "other"
        self.assertEqual(media.stable_sha256(d1), media.stable_sha256(noisy))

    def test_receipt_rows_have_the_documented_shape(self):
        out, _ = baseline()
        doc = read_json(os.path.join(out, "receipts.json"))
        for row in doc["rows"]:
            self.assertTrue({"id", "status", "blocker_class", "reason", "renderer", "artifact", "inspection", "registry_claim",
                             "conflict", "upstream"} <= set(row), row["id"])
            if row["status"] == "EXERCISED":
                self.assertEqual(row["renderer"]["backend"], "real")
                self.assertTrue(row["renderer"]["exe_sha256"] and row["renderer"]["product"])
                self.assertTrue(all(c["result"] == "pass" for c in row["inspection"]["checks"]))
                for art in row["artifact"]:
                    self.assertEqual(sha_file(os.path.join(out, art["path"])), art["sha256"])
                    self.assertFalse(os.path.isabs(art["path"]))

    def test_a_fixture_encoder_gives_a_fixture_row_never_exercised(self):
        need_media()
        d = sandbox().fresh("fixbin")
        os.symlink(str(FIX / "fakebin" / "ffmpeg_passthrough.sh"), os.path.join(d, "ffmpeg"))
        res = cli("receipts", "--out", sandbox().fresh("o"), "--enable", "video", path=d + os.pathsep + ORIG_PATH, timeout=300)
        row = rows_by_id(res)["video"]
        self.assertEqual((row["status"], row["reason"]), ("BLOCKED", "fixture generation"), row)
        self.assertEqual(row["renderer"]["backend"], "fixture")


# ------------------------------------------------------------------------------------------------ A14
class A14Constraints(Cell):
    def test_descriptor_passes(self):
        contracts = load("test_media_contracts", ROOT / "modules" / "contracts" / "contracts.py")
        doc = contracts.check_module_descriptor(MOD, module="media")
        self.assertEqual(doc["claimed_support"], "planned")
        self.assertEqual(doc["evidence"], [])
        self.assertEqual(sorted(d["identifier"] for d in doc["dependencies"]),
                         ["browser-operations", "core-contracts", "design-pipeline", "documents"])
        self.assertTrue(all(d["status"] == "declared-untested" for d in doc["dependencies"]))
        self.assertEqual(doc["schemas"], ["status"])

    def test_provenance_units(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            collection = load("test_media_collection", ROOT / "scripts" / "check_collection.py")
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        collection.provenance(MOD / "PROVENANCE.md")
        text = (MOD / "PROVENANCE.md").read_text()
        units = [u for u in re.split(r"^(?=## )", text, flags=re.M) if u.startswith("## ")]
        hashed = [u for u in units if re.search(r"^Source-sha256: [0-9a-f]{64}$", u, re.M)]
        self.assertGreaterEqual(len(hashed), 16)
        for u in hashed:
            self.assertRegex(u, r"(?m)^State: re-authored$")
        self.assertRegex(text, r"(?m)^## Not ported")
        for fragment in ("46fad5d196abe666058915ea82b1809a4c8497f7d58831c53bd413b674be4102",
                         "d5c6b57f6897b3595086e1483e3dfdc2ebc9e2c00071c8a622b570f2f2a8835c",
                         "a741308693a22d518031ff0b1837fc8975fcf01887cd84b718b87c3aa7fc01fd",
                         "a8d9ea427f41cdc1a5dc4afb6306b9c3e533baf5228635bd862e5a67f4e3c08b"):
            self.assertIn(fragment, text)

    def test_privacy_scan_and_tracked_files(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            scan = load("test_media_privacy", ROOT / "scripts" / "privacy_scan.py")
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        for d in (MOD, EX):
            for hit in scan.scan(d):
                self.fail(f"privacy: {d.name}/{hit}")
        for d in (MOD, EX):
            for p in d.rglob("*"):
                if "__pycache__" in p.parts:
                    continue
                self.assertFalse(p.is_symlink(), p)
                if p.is_file():
                    self.assertNotIn(b"\0", p.read_bytes(), p)
                    self.assertFalse(p.name.startswith("test_") and "tests" not in p.parts, p)

    def test_base_pins(self):
        for rel, sha in BASE_SHA.items():
            self.assertEqual(sha_file(ROOT / rel), sha, rel)

    def test_inspector_module_does_not_shadow_the_stdlib(self):
        self.assertFalse((MOD / "inspect.py").exists())
        self.assertTrue((MOD / "inspectors.py").is_file())

    def test_interface_and_adapters_are_complete(self):
        iface = read_json(MOD / "interface.json")
        self.assertEqual(iface["exit_codes"], {"0": "ok", "1": "inspection failed or tampered", "2": "refused or invalid",
                                              "3": "renderer or tool unavailable", "4": "provider refused"})
        self.assertEqual([o["id"] for o in iface["obligations"]], OBLIGATIONS)
        self.assertEqual(set(iface["blocked_renderers"]), set(BLOCKED_RENDERERS))
        for name in ("slides", "visuals", "video", "illustration"):
            adapter = read_json(MOD / "adapters" / f"{name}.json")
            self.assertEqual(adapter["adapter"], name)
            self.assertTrue(adapter["dependencies"])
        videos = read_json(MOD / "adapters" / "video.json")["dependencies"]
        self.assertIn("ffmpeg", json.dumps(videos))
        self.assertIn("libx264", json.dumps(videos))

    def test_readmes_state_the_contract(self):
        for readme in (MOD / "README.md", EX / "README.md"):
            text = readme.read_text().lower()
            for phrase in ("optional", "chrome", "ffmpeg", "blocked", "exercised", "stub", "audio", "removal"):
                self.assertIn(phrase, text, f"{readme.parent.name}: {phrase}")


def tearDownModule():  # not called by the project loop; the cells restore in their own tearDownClass
    unpatch_sockets()


if __name__ == "__main__":
    unittest.main()
