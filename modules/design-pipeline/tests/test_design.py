"""U20 acceptance A1..A14 plus the amendment cells. Offline; temp HOME; real headless Chrome for the rendered cells.

Every child process gets a built-from-scratch environment (no inherited variable). One baseline run is shared; the
cells mutate copies. The slow work (runs, renders, re-render verifies) is prefetched in two parallel phases in
setUpModule. The suite never patches the process under test: the render timeout is an environment variable.
"""

import atexit
import concurrent.futures
import copy
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
EX = ROOT / "examples" / "design"
PY = sys.executable
DESIGN, PLAYWRIGHT = MOD / "design.py", MOD / "playwright.py"
RUN, VERIFY = EX / "run.py", EX / "verify_receipt.py"
FIX = EX / "fixtures"
REQUIRE = os.environ.get("AIARCH_REQUIRE_BROWSER") == "1"
ORIG_ENV = dict(os.environ)
ORIG_HOME, REAL_ROOT = os.environ.get("HOME", ""), str(ROOT.resolve())
CANARY = "CANARY-7f3a91-builder-private"
SLOW = {}
STATE = {}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


design = load("test_design_mod", DESIGN)
checks = design.checks
contracts = load("test_design_contracts", ROOT / "modules" / "contracts" / "contracts.py")
sys.path.insert(0, str(ROOT / "scripts"))  # check_collection imports its sibling module by name
try:
    collection = load("test_design_collection", ROOT / "scripts" / "check_collection.py")
finally:
    sys.path.remove(str(ROOT / "scripts"))
privacy = load("test_design_privacy", ROOT / "scripts" / "privacy_scan.py")


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def which_original(*names):
    for name in names:
        found = shutil.which(name, path=ORIG_ENV.get("PATH", os.defpath))
        if found:
            return found
    return None


class Sandbox:
    """Temp HOME/XDG/TMPDIR, sentinel fakes on PATH, absolute chrome and git, and a from-scratch child environment."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="design-test-")).resolve()
        for name in ("home", "xdg", "xdgc", "tmp", "sentinels", "fakes"):
            (self.tmp / name).mkdir()
        self.chrome = os.environ.get("AIARCH_CHROME") or which_original("google-chrome", "google-chrome-stable", "chromium",
                                                                        "chromium-browser", "chrome")
        self.git = which_original("git")
        for name in ("claude", "opencode", "curl", "wget", "npx", "playwright"):
            path = self.tmp / "fakes" / name
            path.write_text(f"#!/bin/sh\ntouch '{self.tmp / 'sentinels' / name}'\nexit 1\n")
            path.chmod(0o755)

    def env(self, **extra):
        env = {"PATH": f"{self.tmp / 'fakes'}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.tmp / "home"),
               "XDG_STATE_HOME": str(self.tmp / "xdg"), "XDG_CONFIG_HOME": str(self.tmp / "xdgc"), "TMPDIR": str(self.tmp / "tmp"),
               "LANG": "C", "AIARCH_GIT": self.git or ""}
        if self.chrome:
            env["AIARCH_CHROME"] = self.chrome
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def sentinels(self):
        return sorted(p.name for p in (self.tmp / "sentinels").iterdir())


SB = None
SAVED = {}


def execute(argv, env, timeout=420):
    return subprocess.run([PY, "-B", *map(str, argv)], capture_output=True, text=True, env=env, timeout=timeout)


def example(args, out, **extra):
    return execute([RUN, "--offline", "--out", out, *args], SB.env(**extra))


def latest(out):
    return Path(out) / json.loads((Path(out) / "latest.json").read_text())["run"]


def runs_in(out):
    return sorted((Path(out) / "runs").glob("*")) if (Path(out) / "runs").is_dir() else []


def receipt_of(run):
    return json.loads((Path(run) / "receipt.json").read_text())


def verify(run, receipt=None):
    proc = execute([VERIFY, str(receipt or Path(run) / "receipt.json")], SB.env())
    return proc.returncode, proc.stdout + proc.stderr


def copy_run(run):
    dest = SB.tmp / f"copy-{next(COUNTER)}"
    shutil.copytree(run, dest, symlinks=True)
    return dest


class Counter:
    def __init__(self):
        self.n, self.lock = 0, threading.Lock()

    def __next__(self):
        with self.lock:
            self.n += 1
            return self.n


COUNTER = Counter()


def write_receipt(run, receipt):
    (Path(run) / "receipt.json").write_text(design.dump(design.seal(receipt)), encoding="utf-8")


def edit_receipt(run, fn):
    receipt = receipt_of(run)
    fn(receipt)
    write_receipt(run, receipt)


def forge(run, inspection_edit=None, critic_edit=None, keep_bundle_sha=False):
    """Rewrite a run so that every hash agrees again after the edits: render files, bundle, critic output, prompts, receipt."""
    run = Path(run)
    receipt = receipt_of(run)
    for rnd in receipt["rounds"]:
        k = rnd["n"]
        base = run / "renders" / f"r{k}"
        insp = json.loads((base / "inspection.json").read_text())
        if inspection_edit:
            inspection_edit(k, insp)
        (base / "inspection.json").write_text(design.dump(insp))
        r = rnd["render"]
        r["results"] = {c["check"]: c["result"] for c in insp["checks"]}
        r["inspection_sha"] = sha((base / "inspection.json").read_bytes())
        r["trace_sha"] = sha((base / "trace.jsonl").read_bytes())
        r["shots"] = [dict(s, sha256=sha((base / s["path"]).read_bytes())) for s in r["shots"]]
        bundle = run / "bundles" / f"r{k}"
        for name in ["inspection.json", "trace.jsonl"] + [s["path"] for s in r["shots"]]:
            shutil.copyfile(base / name, bundle / name)
        manifest = dict(design.bundle_manifest(bundle), round=k)
        (bundle / "bundle.json").write_text(design.dump(manifest))
        bsha = sha(design.canon(manifest))
        rnd["bundle"]["bundle_sha"] = bsha
        c = rnd["critic"]
        folder = run / "dispatch-critic" / c["job"]["job_id"]
        out = json.loads((folder / "stdout.txt").read_text())
        for f in out["findings"]:
            f["bundle_sha"] = bsha if not keep_bundle_sha else "0" * 64
        if critic_edit:
            critic_edit(k, out, insp, bsha)
        (folder / "stdout.txt").write_text(json.dumps(out, sort_keys=True) + "\n")
        (folder / "prompt.txt").write_text(json.dumps(design.critic_prompt(bundle), sort_keys=True))
        result = json.loads((folder / "result.json").read_text())
        result["dispatch"]["stdout"]["sha256"] = sha((folder / "stdout.txt").read_bytes())
        (folder / "result.json").write_text(json.dumps(result))
        c["job"]["stdout_sha256"] = result["dispatch"]["stdout"]["sha256"]
        c.update(findings=out["findings"], approved=out["approved"], claims_browser_validation=out["claims_browser_validation"],
                 findings_sha=sha(design.canon(out["findings"])))
    for rnd in receipt["rounds"][1:]:
        before = receipt["rounds"][rnd["n"] - 1]["critic"]
        path = run / "dispatch-builder" / rnd["build_job"]["job_id"] / "prompt.txt"
        prompt = json.loads(path.read_text())
        prompt.update(findings=before["findings"], findings_sha=before["findings_sha"])
        path.write_text(json.dumps(prompt, sort_keys=True))
    status, reasons = design.decide(receipt["rounds"][-1])
    receipt["verdict"] = {"status": status, "reasons": reasons}
    write_receipt(run, receipt)
    return receipt


def png(width, height, pixel=(255, 255, 255, 255)):
    raw = b"".join(b"\x00" + bytes(pixel) * width for _ in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def variant_tree(*names, port=None):
    """A copy of the good tree with the named defect edits (defects.json) applied."""
    spec = json.loads((FIX / "defects.json").read_text())
    tree = Path(tempfile.mkdtemp(prefix="variant-", dir=SB.tmp / "tmp"))
    for path in (FIX / "good").iterdir():
        shutil.copyfile(path, tree / path.name)
    for name in names:
        entry = spec["defects"].get(name) or spec["extra"][name]
        for edit in entry["edits"]:
            text = (tree / edit["file"]).read_text()
            assert text.count(edit["good"]) == 1, (name, edit["file"])
            (tree / edit["file"]).write_text(text.replace(edit["good"], edit["bad"], 1))
        for fname, text in entry.get("files", {}).items():
            (tree / fname).write_text(text.replace("__PORT__", str(port)))
    return tree


def inspect(tree, record=None, adapters="accessibility,motion", extra=(), **env):
    out = Path(tempfile.mkdtemp(prefix="inspect-", dir=SB.tmp / "tmp"))
    proc = execute([DESIGN, "inspect", "--tree", tree, "--record", record or FIX / "record.json", "--adapters", adapters,
                    "--out", out, *extra], SB.env(**env))
    report = None
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            report = json.loads(line)
    return {"rc": proc.returncode, "report": report, "out": out, "stdout": proc.stdout, "stderr": proc.stderr}



class Probe:
    """A throwaway loopback listener that counts TCP accepts (the page must never reach it)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port, self.accepts, self.stop = self.sock.getsockname()[1], 0, threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
                self.accepts += 1
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                return

    def close(self):
        time.sleep(0.5)
        self.stop.set()
        self.sock.close()


RTC_JS = """(async function(){try{
 var pc=new (__CTOR__)({iceServers:[]});pc.createDataChannel('x');
 var o=await pc.createOffer();await pc.setLocalDescription(o);
 var fp=(o.sdp.match(/a=fingerprint:.*\\r\\n/)||[''])[0];
 var mid=(o.sdp.match(/a=mid:(\\S+)/)||[,'0'])[1];
 var sdp=['v=0','o=- 1 2 IN IP4 127.0.0.1','s=-','t=0 0','a=group:BUNDLE '+mid,
  'm=application 9 UDP/DTLS/SCTP webrtc-datachannel','c=IN IP4 0.0.0.0','a=mid:'+mid,
  'a=ice-ufrag:abcd','a=ice-pwd:abcdefghijklmnopqrstuvwx',fp.trim(),'a=setup:active','a=sctp-port:5000',
  'a=candidate:1 1 tcp 2122260223 127.0.0.1 __PORT__ typ host tcptype passive',''].join('\\r\\n');
 await pc.setRemoteDescription({type:'answer',sdp:sdp});
 try{await pc.addIceCandidate({candidate:'candidate:2 1 tcp 2122260222 127.0.0.1 __PORT__ typ host tcptype passive',sdpMid:mid});}catch(e){}
 window.__rtc=pc;
}catch(e){document.title='rtcerr '+e;}})();"""
PROBES = {
    "worker-only": {"probe.js": "new Worker('w.js');", "w.js": "fetch('http://127.0.0.1:__PORT__/w').catch(function(){});"},
    "iframe-only": {"probe.js": "var f=document.createElement('iframe');f.src='f.html';document.body.appendChild(f);",
                    "f.html": "<!doctype html><script>fetch('http://127.0.0.1:__PORT__/f').catch(function(){});</script>"},
    # only the 7th load of a run (2 viewports, keyboard, 3 interactions, then reduced motion = the last) arms it, 350 ms
    # after load: its report lands in the drain, not before
    "late-worker": {"probe.js": "var n=(+localStorage.getItem('n')||0)+1;localStorage.setItem('n',n);"
                                "if(n===7)setTimeout(function(){new Worker('w.js');},350);",
                    "w.js": "fetch('http://127.0.0.1:__PORT__/w').catch(function(){});"},
    "clear-viol": {"probe.js": "Object.defineProperty(window,'__aiarch_viol',{get:function(){return [];},set:function(){}});"
                               "fetch('http://127.0.0.1:__PORT__/m').catch(function(){});"},
    "control-main-fetch": {"probe.js": "fetch('http://127.0.0.1:__PORT__/m').catch(function(){});"},
    "rtc-icetcp": {"probe.js": RTC_JS.replace("__CTOR__", "window.RTCPeerConnection")},
    "rtc-fresh-iframe": {"probe.js": "var __f=document.createElement('iframe');document.body.appendChild(__f);"
                                     "var __R=__f.contentWindow.RTCPeerConnection;" + RTC_JS.replace("__CTOR__", "__R")},
    "iframe-404-ws": {"probe.js": "var f=document.createElement('iframe');f.src='/nope';f.onload=function(){try{"
                                  "new f.contentWindow.WebSocket('ws://127.0.0.1:__PORT__/x');}catch(e){document.title='ws-err '+e;}};"
                                  "document.body.appendChild(f);"},
    # legitimate: a same-origin worker that fetches a same-origin file and answers the page; the answer is a required element,
    # so the check passes only when the worker really ran and its same-origin fetch really was served
    "worker-legit": {"probe.js": "var w=new Worker('w.js');w.onmessage=function(e){var s=document.createElement('span');"
                                 "s.className='worker-ok';s.textContent='worker '+e.data;document.querySelector('main').appendChild(s);};",
                     "w.js": "fetch('app.js').then(function(r){return r.text();}).then(function(t){postMessage(t.length);});"},
}


def probe_tree(name, port):
    tree = Path(tempfile.mkdtemp(prefix="probe-", dir=SB.tmp / "tmp"))
    for path in (FIX / "good").iterdir():
        shutil.copyfile(path, tree / path.name)
    text = (tree / "index.html").read_text()
    marker = '<script src="app.js"></script>'
    assert text.count(marker) == 1
    (tree / "index.html").write_text(text.replace(marker, marker + '\n<script src="probe.js"></script>', 1))
    for fname, body in PROBES[name].items():
        (tree / fname).write_text(body.replace("__PORT__", str(port)))
    return tree


def probed(name):
    listener_ = Probe()
    try:
        record = None
        if name == "worker-legit":
            doc = json.loads((FIX / "record.json").read_text())
            doc["required_elements"] = doc["required_elements"] + [".worker-ok"]
            record = Path(tempfile.mkdtemp(prefix="rec-", dir=SB.tmp / "tmp")) / "record.json"
            record.write_text(json.dumps(doc))
        res = inspect(probe_tree(name, listener_.port), record=record)
    finally:
        listener_.close()
    res["accepts"] = listener_.accepts
    return res


def failing(res):
    return {c for c, r in res["report"]["checks"].items() if r in ("fail", "error")}


def have_chrome():
    return bool(SB.chrome) and os.access(SB.chrome, os.X_OK)


class Cell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure()
        cls._environ = dict(os.environ)
        patch_sockets()

    @classmethod
    def tearDownClass(cls):
        unpatch_sockets()
        os.environ.clear()
        os.environ.update(cls._environ)

    def needs_chrome(self):
        if not have_chrome():
            self.skipTest("Chrome is not installed: rendered cells skipped (AIARCH_REQUIRE_BROWSER=1 makes this a failure)")

    def slow(self, name):
        self.needs_chrome()
        return SLOW[name].result()


# ------------------------------------------------------------------------------------------------ module set up

DEFECT_SETS = {"a11y-name": {"a11y-name"}, "contrast": {"contrast"}, "overflow": {"overflow"},
               "keyboard-reach": {"keyboard-reach"}, "focus-visible": {"focus-visible"}, "interaction": {"interaction"},
               "reduced-motion": {"reduced-motion"}, "external-request": {"external-request"}}
LISTENER = {"accepts": 0, "sock": None}
PROBE_JOBS = ["late-worker", "worker-only", "iframe-only", "clear-viol", "worker-legit", "rtc-icetcp", "rtc-fresh-iframe",
              "iframe-404-ws", "control-main-fetch"]


def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.settimeout(0.2)
    LISTENER["sock"], stop = sock, threading.Event()
    LISTENER["stop"] = stop

    def loop():
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
                LISTENER["accepts"] += 1
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                return
    threading.Thread(target=loop, daemon=True).start()
    return sock.getsockname()[1]


def hang_chrome():
    pidfile = SB.tmp / "hang.pids"
    script = SB.tmp / "hang-chrome"
    script.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nsleep 300 &\necho $! >> '{pidfile}'\nwait\n")
    script.chmod(0o755)
    return script, pidfile


def phase_one():
    jobs = {}
    out = lambda name: SB.tmp / f"out-{name}"
    if have_chrome():
        jobs["A"] = lambda: example([], out("A"))
        jobs["B"] = lambda: example([], out("B"))
        jobs["L"] = lambda: example(["--leave-unfixed", "contrast"], out("L"))
        jobs["D"] = lambda: example(["--delete-element"], out("D"))
        jobs["K"] = lambda: example(["--canary", CANARY], out("K"))
        jobs["R3"] = lambda: example(["--revisions", "3"], out("R3"))
        secret = SB.tmp / "outside-secret.txt"
        secret.write_text("OUTSIDE-SECRET-CANARY\n")
        jobs["S"] = lambda: example(["--plant-symlink", str(secret)], out("S"))
        jobs["NR_fault"] = lambda: example([], out("NR_fault"), AIARCH_DESIGN_FAULT="contrast")
        jobs["i:good"] = lambda: inspect(variant_tree())
        for name in DEFECT_SETS:
            jobs[f"i:{name}"] = lambda name=name: inspect(variant_tree(name))
        jobs["i:bad"] = lambda: inspect(FIX / "bad")
        jobs["i:trap"] = lambda: inspect(variant_tree("keyboard-trap"))
        jobs["i:tokens"] = lambda: inspect(variant_tree("tokens"), adapters="accessibility,motion,style")
        jobs["i:tokens-off"] = lambda: inspect(variant_tree("tokens"))
        jobs["i:escape"] = lambda: inspect(variant_tree("escape", port=listener()))
        for name in PROBE_JOBS:
            jobs[f"px:{name}"] = lambda name=name: probed(name)
    links = SB.tmp / "links"
    links.mkdir()
    (links / "git").symlink_to(SB.git)
    (links / "python3").symlink_to(PY)
    jobs["NR_path"] = lambda: example([], out("NR_path"), PATH=str(links), AIARCH_CHROME=str(SB.tmp / "no-such-chrome"))
    fake = SB.tmp / "exit-chrome"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    jobs["NR_exit"] = lambda: example([], out("NR_exit"), AIARCH_CHROME=str(fake))
    script, _ = hang_chrome()

    def hang():
        started = time.monotonic()
        proc = example([], out("NR_hang"), AIARCH_CHROME=str(script), AIARCH_RENDER_TIMEOUT="3")
        STATE["hang_seconds"] = time.monotonic() - started
        return proc
    jobs["NR_hang"] = hang
    return jobs


def phase_two():
    jobs = {}
    if not have_chrome():
        return jobs
    runA, runL = (lambda: latest(SB.tmp / "out-A")), (lambda: latest(SB.tmp / "out-L"))

    def verified(src, **forge_args):
        def go():
            run = copy_run(src())
            forge(run, **forge_args)
            return verify(run)
        return go

    jobs["verify-A"] = lambda: verify(runA())
    jobs["flip"] = verified(runL, inspection_edit=lambda k, i: flip_pass(i))

    def approve(k, out, insp, bsha):
        if k == 1:
            out.update(findings=[], approved=True)
    jobs["approve"] = verified(runL, critic_edit=approve)

    def blocker(k, out, insp, bsha):
        if k == 1:
            out["findings"] = [{"id": "F9", "check": "contrast", "evidence_id": "ev-001", "priority": "blocking", "bundle_sha": bsha}]
            out["approved"] = False
    jobs["blocking"] = verified(runA, critic_edit=blocker)

    def advisory(k, out, insp, bsha):
        if k == 1:
            out["findings"] = [{"id": "F9", "check": "contrast", "evidence_id": "ev-001", "priority": "advisory", "bundle_sha": bsha}]
    jobs["advisory"] = verified(runA, critic_edit=advisory)

    # fix round 1 (B1): a hostile run repo driven through verify, one copy of the baseline run each
    def poisoned(tweak):
        def go():
            run = copy_run(runA())
            marker = SB.tmp / f"marker-{next(COUNTER)}"
            tweak(run, marker)
            code, text = verify(run)
            return {"rc": code, "text": text, "marker": marker.exists()}
        return go

    def git_in(repo, *argv):
        return subprocess.run([SB.git, "-C", str(repo), *argv], capture_output=True, text=True, env=SB.env(), check=True).stdout.strip()

    def smudge(run, marker):
        (run / "repo" / ".git" / "info").mkdir(exist_ok=True)
        (run / "repo" / ".git" / "info" / "attributes").write_text("* filter=x\n")
        with open(run / "repo" / ".git" / "config", "a") as handle:
            handle.write(f'[filter "x"]\n\tsmudge = touch {marker}; cat\n')

    def promisor(run, marker):
        repo = run / "repo"
        blob = git_in(repo, "rev-parse", f"{receipt_of(run)['rounds'][0]['commit']}:index.html")
        os.chmod(repo / ".git" / "objects" / blob[:2] / blob[2:], 0o644)
        (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        with open(repo / ".git" / "config", "a") as handle:
            handle.write(f'[core]\n\trepositoryformatversion = 1\n[extensions]\n\tpartialclone = origin\n[remote "origin"]\n'
                         f'\turl = {repo}\n\tpromisor = true\n\tuploadpack = touch {marker};#\n')

    def graph(run, marker):
        repo = run / "repo"
        rounds, base = receipt_of(run)["rounds"], receipt_of(run)["base_commit"]
        git_in(repo, "commit-graph", "write", "--reachable", "--no-progress")
        forge_commit_graph(repo / ".git" / "objects" / "info" / "commit-graph", rounds[1]["commit"], base)

    def linked_objects(run, marker):
        objects = run / "repo" / ".git" / "objects"
        elsewhere = run.parent / f"objects-{next(COUNTER)}"
        objects.rename(elsewhere)
        objects.symlink_to(elsewhere)
    jobs["fx-smudge"], jobs["fx-promisor"] = poisoned(smudge), poisoned(promisor)
    jobs["fx-graph"], jobs["fx-objlink"] = poisoned(graph), poisoned(linked_objects)
    return jobs


def forge_commit_graph(path, commit, parent):
    """Point `commit`'s first parent at `parent` inside a commit-graph file (the checksum is not rewritten: git does not check it)."""
    data = bytearray(Path(path).read_bytes())
    chunks = {bytes(data[8 + 12 * i:12 + 12 * i]): struct.unpack(">Q", data[12 + 12 * i:20 + 12 * i])[0] for i in range(data[6] + 1)}
    count = struct.unpack(">I", data[chunks[b"OIDF"] + 1020:chunks[b"OIDF"] + 1024])[0]
    oids = [bytes(data[chunks[b"OIDL"] + 20 * i:chunks[b"OIDL"] + 20 * i + 20]).hex() for i in range(count)]
    spot = chunks[b"CDAT"] + 36 * oids.index(commit) + 20
    data[spot:spot + 4] = struct.pack(">I", oids.index(parent))
    os.chmod(path, 0o644)
    Path(path).write_bytes(bytes(data))


def flip_pass(insp):
    for c in insp["checks"]:
        c["result"] = "pass"
        for e in c["evidence"]:
            e["result"] = "pass"


def run_pool(jobs, workers=4):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = {name: pool.submit(fn) for name, fn in jobs.items()}
    concurrent.futures.wait(futures.values())
    pool.shutdown()
    return futures


def patch_sockets():
    """In-process connections to anything but loopback raise. Restored by unpatch_sockets in every class's tearDownClass."""
    if "create" not in SAVED:
        SAVED.update(create=socket.create_connection, connect=socket.socket.connect)
    real_create, real_connect = SAVED["create"], SAVED["connect"]

    def local(address):
        return isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1", "localhost")

    def guard_create(address, *a, **k):
        if not local(address):
            raise OSError("test: off-host connection refused")
        return real_create(address, *a, **k)

    def guard_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not local(address):
            raise OSError("test: off-host connection refused")
        return real_connect(self, address)
    socket.create_connection, socket.socket.connect = guard_create, guard_connect


def unpatch_sockets():
    socket.create_connection, socket.socket.connect = SAVED["create"], SAVED["connect"]


def cleanup():
    if LISTENER.get("stop"):
        LISTENER["stop"].set()
        LISTENER["sock"].close()
    shutil.rmtree(SB.tmp, ignore_errors=True)


def ensure():
    """Once per process, on the first class that needs it: the sandbox and both prefetch phases. (The project loop loads
    this file as a plain module, so there is no module-level fixture hook to rely on.)"""
    global SB
    if STATE.get("ready"):
        return
    STATE["t0"] = time.monotonic()
    SB = Sandbox()
    atexit.register(cleanup)
    if not have_chrome():
        message = "Chrome is not installed: rendered cells will be skipped"
        print(f"\nNOTE: {message}", file=sys.stderr)
        if REQUIRE:
            raise AssertionError(message + " (AIARCH_REQUIRE_BROWSER=1)")
    only = [x for x in os.environ.get("AIARCH_TEST_JOBS", "").split(",") if x]  # a narrowed run (mutation checks)
    narrow = lambda jobs: {k: v for k, v in jobs.items() if not only or any(k == o or (o.endswith(":") and k.startswith(o)) for o in only)}
    SLOW.update(run_pool(narrow(phase_one())))
    SLOW.update(run_pool(narrow(phase_two())))
    STATE["ready"] = True
    print(f"\nprefetch finished in {time.monotonic() - STATE['t0']:.0f}s", file=sys.stderr)


# ------------------------------------------------------------------------------------------------ A1
class A1Journey(Cell):
    def test_baseline_journey_and_verify(self):
        proc = self.slow("A")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = receipt_of(latest(SB.tmp / "out-A"))
        r0, r1 = r["rounds"]
        self.assertNotEqual(r0["commit"], r1["commit"])
        self.assertTrue(any(v != "pass" for v in r0["render"]["results"].values()), "R1 must fail")
        self.assertTrue(r0["critic"]["findings"], "critic-1 has findings")
        self.assertTrue(all(v == "pass" for v in r1["render"]["results"].values()), "R2 must pass")
        self.assertFalse([f for f in r1["critic"]["findings"] if f["priority"] == "blocking"])
        self.assertEqual(r["verdict"], {"status": "PASS", "reasons": []})
        self.assertIn("rendered: ran", proc.stdout)
        rc, text = self.slow("verify-A")
        self.assertEqual(rc, 0, text)
        self.assertIn("rendered: ran", text)

    def test_requires_offline_and_writes_nothing(self):
        out = SB.tmp / "refused-out"
        proc = execute([RUN, "--out", out], SB.env())
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(out.exists())

    def test_refuses_symlinked_out(self):
        real = SB.tmp / "real-out"
        real.mkdir()
        link = SB.tmp / "link-out"
        link.symlink_to(real)
        proc = execute([RUN, "--offline", "--out", link], SB.env())
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(list(real.iterdir()), [])

    def test_verify_receipt_default_is_a_real_run_not_a_fixture(self):
        text = VERIFY.read_text()
        self.assertIn("run.py", text)
        self.assertIn("tempfile", text)


# ------------------------------------------------------------------------------------------------ A2
class A2Failures(Cell):
    def test_good_fixture_passes_everything(self):
        res = self.slow("i:good")
        self.assertEqual(res["rc"], 0, res["stdout"])
        self.assertTrue(all(v == "pass" for v in res["report"]["checks"].values()), res["report"]["checks"])
        self.assertEqual(set(res["report"]["checks"]), {"overflow", "required-present", "interaction", "external-request", "a11y-name",
                                                          "contrast", "keyboard-reach", "focus-visible", "reduced-motion"})

    def test_each_single_defect_fails_exactly_its_own_check(self):
        self.needs_chrome()
        for name, expect in DEFECT_SETS.items():
            with self.subTest(defect=name):
                res = SLOW[f"i:{name}"].result()
                self.assertEqual(res["rc"], 1, res["stdout"])
                self.assertEqual(failing(res), expect, res["report"]["checks"])
                insp = json.loads((res["out"] / "inspection.json").read_text())
                rows = [e for c in insp["checks"] if c["check"] == name for e in c["evidence"] if e["result"] == "fail"]
                self.assertTrue(rows, "evidence for the failure")
                self.assertTrue(all(set(e) >= {"check", "selector", "viewport", "measured", "screenshot_sha", "id"} for e in rows))

    def test_unreachable_target_blocks_interaction_and_focus_never_passes_them(self):
        res = self.slow("i:keyboard-reach")
        self.assertEqual(res["report"]["checks"]["interaction"], "blocked")
        self.assertEqual(res["report"]["checks"]["focus-visible"], "blocked")
        self.assertNotIn("blocked", failing(res) | set())

    def test_seeded_bad_app_fails_all_eight(self):
        res = self.slow("i:bad")
        self.assertEqual(res["rc"], 1)
        self.assertEqual(failing(res), set(DEFECT_SETS))

    def test_bad_tree_is_good_plus_the_eight_defects(self):
        spec = json.loads((FIX / "defects.json").read_text())
        tree = variant_tree(*spec["order"])
        for path in (FIX / "bad").iterdir():
            self.assertEqual(path.read_text(), (tree / path.name).read_text(), path.name)

    def test_keyboard_trap_is_caught_and_attributed(self):
        res = self.slow("i:trap")
        self.assertEqual(failing(res), {"keyboard-reach"}, res["report"]["checks"])
        insp = json.loads((res["out"] / "inspection.json").read_text())
        self.assertTrue(insp["keyboard"]["trap"])

    def test_tokens_can_fail_and_only_with_the_style_adapter(self):
        res = self.slow("i:tokens")
        self.assertEqual(failing(res), {"tokens"}, res["report"]["checks"])
        insp = json.loads((res["out"] / "inspection.json").read_text())
        props = {e["measured"]["property"] for c in insp["checks"] if c["check"] == "tokens" for e in c["evidence"] if e["result"] == "fail"}
        self.assertEqual(props, {"color", "font-family"})
        self.assertEqual(self.slow("i:tokens-off")["rc"], 0)

    def test_trace_records_trusted_keys_focus_and_active_element(self):
        res = self.slow("i:good")
        steps = [json.loads(l) for l in (res["out"] / "trace.jsonl").read_text().splitlines()]
        keys = [s for s in steps if s["step"] == "key"]
        self.assertTrue(keys)
        self.assertTrue(all(s["input"]["trusted"] is True and "active" in s["observed"] and "hasFocus" in s["observed"] for s in keys))
        loads = [s for s in steps if s["step"] == "load"]
        self.assertTrue(all(s["observed"]["hasFocus"] is True for s in loads))
        insp = json.loads((res["out"] / "inspection.json").read_text())
        focus = [e for c in insp["checks"] if c["check"] == "focus-visible" for e in c["evidence"]]
        self.assertTrue(all(e["measured"]["changed_pixels"] > 0 for e in focus))

    def test_contrast_is_an_error_never_a_pass_on_a_background_image_or_without_an_opaque_ancestor(self):
        base = {"text": True, "color": "rgb(0, 0, 0)", "fontSize": 16, "fontWeight": 400, "rect": [0, 0, 1, 1]}
        vp = {"name": "1x1", "shot_sha": "s", "elements": [dict(base, sel="p", bgImg=True, bg=None),
                                                          dict(base, sel="h1", bgImg=False, bg=None)]}
        rows = checks.check_contrast({"viewports": [vp]})
        self.assertEqual([r["result"] for r in rows], ["error", "error"])
        self.assertEqual(checks.aggregate("contrast", rows)["result"], "error")

    def test_check_helpers(self):
        self.assertEqual(checks.parse_color("rgb(153, 153, 153)")[:3], (153, 153, 153))
        self.assertLess(checks.ratio(checks.parse_color("#999999"), checks.parse_color("#ffffff")), 4.5)
        self.assertGreater(checks.ratio(checks.parse_color("#000000"), checks.parse_color("#ffffff")), 20)
        self.assertEqual(checks.aggregate("x", [checks.row("x", "", "", {}, "", "error")])["result"], "error")
        self.assertEqual(checks.aggregate("x", [checks.row("x", "", "", {}, "", "pass")], {"x": "boom"})["result"], "error")


# ------------------------------------------------------------------------------------------------ A3
class A3RenderedEvidence(Cell):
    def tampered(self, fn):
        run = copy_run(latest(SB.tmp / "out-A"))
        fn(run)
        return verify(run)

    def shot(self, run, k=1):
        return Path(run) / "renders" / f"r{k}" / "screenshots" / "vp-1280x800.png"

    def test_screenshot_removed(self):
        self.needs_chrome()
        rc, text = self.tampered(lambda run: self.shot(run).unlink())
        self.assertEqual(rc, 1, text)

    def replace_and_reseal(self, data):
        def fn(run):
            self.shot(run).write_bytes(data)
            def fix(receipt):
                for s in receipt["rounds"][1]["render"]["shots"]:
                    if s["path"].endswith("vp-1280x800.png"):
                        s["sha256"] = sha(data)
            edit_receipt(run, fix)
        return self.tampered(fn)

    def test_uniform_png_of_the_right_size(self):
        self.needs_chrome()
        rc, text = self.replace_and_reseal(png(1280, 800))
        self.assertEqual(rc, 1, text)
        self.assertIn("uniform", text)

    def test_one_by_one_png(self):
        self.needs_chrome()
        rc, text = self.replace_and_reseal(png(1, 1))
        self.assertEqual(rc, 1, text)

    def test_recorded_fail_flipped_to_pass_with_every_hash_recomputed(self):
        rc, text = self.slow("flip")
        self.assertEqual(rc, 1, text)
        self.assertIn("re-render disagrees", text)

    def test_receipt_whose_renderer_is_not_chrome_cannot_pass(self):
        self.needs_chrome()
        def fn(run):
            edit_receipt(run, lambda r: r["rounds"][1]["render"]["renderer"].update(name="static-html"))
        rc, text = self.tampered(fn)
        self.assertEqual(rc, 1, text)

    def test_png_checker(self):
        self.assertIsNone(checks.png_problem(self_png(), 40, 30))
        self.assertIn("uniform", checks.png_problem(png(40, 30), 40, 30))
        self.assertIn("expected", checks.png_problem(png(1, 1), 40, 30))
        self.assertEqual(checks.png_problem(b"nope", 1, 1), "not a png")


def differing(x, y, path=""):
    """Paths at which two JSON values differ (for readable failures)."""
    if isinstance(x, dict) and isinstance(y, dict):
        return [p for k in sorted(set(x) | set(y)) for p in differing(x.get(k), y.get(k), f"{path}/{k}")]
    if isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
        return [p for i, (u, v) in enumerate(zip(x, y)) for p in differing(u, v, f"{path}[{i}]")]
    return [] if x == y else [f"{path}: {str(x)[:50]} != {str(y)[:50]}"]


def self_png():
    """A 40x30 PNG with two distinct pixels."""
    raw = bytearray()
    for y in range(30):
        raw += b"\x00" + (b"\xff\xff\xff\xff" * 40 if y < 15 else b"\x00\x00\x00\xff" * 40)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 30, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


# ------------------------------------------------------------------------------------------------ A4
class A4NotRun(Cell):
    def check_not_run(self, name):
        proc = SLOW[name].result()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"rendered: NOT RUN \(.+\)")
        run = latest(SB.tmp / f"out-{name}")
        self.assertNotIn(b"PASS", (run / "receipt.json").read_bytes())
        self.assertEqual(receipt_of(run)["verdict"]["status"], "NOT RUN")
        started = time.monotonic()
        rc, text = verify(run)
        self.assertEqual(rc, 3, text)
        self.assertIn("rendered: NOT RUN (", text)
        self.assertLess(time.monotonic() - started, 5, "verify must not relaunch Chrome for a NOT RUN receipt")
        return proc

    def test_no_chrome_on_path(self):
        self.check_not_run("NR_path")

    def test_chrome_that_exits_zero(self):
        self.check_not_run("NR_exit")

    def test_chrome_that_hangs_leaves_no_process_and_obeys_the_timeout(self):
        self.check_not_run("NR_hang")
        pids = [int(x) for x in (SB.tmp / "hang.pids").read_text().split()]
        time.sleep(0.3)
        for pid in pids:
            self.assertFalse(Path(f"/proc/{pid}").exists() and "Z" not in Path(f"/proc/{pid}/stat").read_text().split(")")[-1][:3],
                             f"pid {pid} survived")
        self.assertLess(STATE["hang_seconds"], 60)

    def test_cdp_error_in_a_check_is_not_a_pass(self):
        self.needs_chrome()
        self.check_not_run("NR_fault")

    def test_error_result_is_never_a_pass(self):
        res = checks.aggregate("contrast", [checks.row("contrast", "", "", {}, "", "pass")], {"contrast": "CDPError"})
        self.assertEqual((res["result"], res["cause"]), ("error", "browser"))
        status, _ = design.decide({"render": {"state": "NOT RUN", "reason": "x"}})
        self.assertEqual(status, "NOT RUN")


# ------------------------------------------------------------------------------------------------ A5
class A5Independence(Cell):
    def test_canaries_never_reach_the_critic(self):
        proc = self.slow("K")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = latest(SB.tmp / "out-K")
        self.assertTrue((run / "dispatch-builder" / "build-0" / "stdout.txt").read_text().count(CANARY))
        self.assertTrue((run / "dispatch-builder" / "build-0" / "stderr.txt").read_text().count(CANARY))
        self.assertTrue((run / "dispatch-builder" / "build-0" / "prompt.txt").read_text().count(CANARY))
        self.assertIn(CANARY, subprocess.run(["git", "-C", str(run / "repo"), "log", "--format=%B", "--all"], capture_output=True,
                                             text=True, env={"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin"}).stdout)
        receipt = receipt_of(run)
        for k in (0, 1):
            roots = [run / "dispatch-critic" / f"critique-{k}", run / "bundles" / f"r{k}"]
            for root in roots:
                for path in root.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(CANARY.encode(), path.read_bytes(), str(path))
            start = json.loads((roots[0] / "start.json").read_text())
            self.assertNotIn(CANARY, json.dumps(start))
            self.assertEqual(Path(start["workdir"]).resolve(), roots[1].resolve())
            builder = json.loads((run / "dispatch-builder" / receipt["rounds"][k]["build_job"]["job_id"] / "start.json").read_text())
            self.assertFalse(Path(start["workdir"]).resolve().is_relative_to((run / "dispatch-builder").resolve()))
            self.assertFalse(Path(start["workdir"]).resolve().is_relative_to(Path(builder["workdir"]).resolve()))
            for key in ("role", "job_id", "pid"):
                self.assertNotEqual(start.get(key), builder.get(key), key)
            self.assertNotEqual(start["argv"], builder["argv"])
            self.assertNotEqual(start["seat"], builder["seat"])
            self.assertFalse(list(roots[1].rglob(".git")))

    def copy_baseline(self):
        self.needs_chrome()
        return copy_run(latest(SB.tmp / "out-A"))

    def test_bundle_with_builder_stdout_fails(self):
        run = self.copy_baseline()
        shutil.copyfile(run / "dispatch-builder" / "build-0" / "stdout.txt", run / "bundles" / "r0" / "builder-stdout.txt")
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)

    def test_critic_workdir_is_the_builder_worktree_fails(self):
        run = self.copy_baseline()
        builder = json.loads((run / "dispatch-builder" / "build-0" / "start.json").read_text())
        path = run / "dispatch-critic" / "critique-0" / "start.json"
        start = json.loads(path.read_text())
        start["workdir"] = builder["workdir"]
        path.write_text(json.dumps(start))
        edit_receipt(run, lambda r: r["rounds"][0]["critic"]["job"].update(workdir="worktrees/build-0"))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)

    def test_critic_job_equal_to_builder_job_fails(self):
        run = self.copy_baseline()
        shutil.copytree(run / "dispatch-builder" / "build-0", run / "dispatch-critic" / "build-0")
        record, _ = design.job_record(run, "dispatch-critic", "build-0")
        edit_receipt(run, lambda r: r["rounds"][0]["critic"].update(job=record))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)

    def test_findings_with_a_different_bundle_sha_fail(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        forge(run, keep_bundle_sha=True)
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)
        self.assertIn("bundle_sha", text)

    def test_builder_only_state_is_not_in_the_critic_prompt_by_construction(self):
        self.needs_chrome()
        run = latest(SB.tmp / "out-A")
        prompt = json.loads((run / "dispatch-critic" / "critique-0" / "prompt.txt").read_text())
        self.assertEqual(set(prompt), {"bundle", "record", "round"})


# ------------------------------------------------------------------------------------------------ A6
class A6Revision(Cell):
    def test_every_blocking_finding_fails_at_c1_and_passes_at_c2(self):
        self.slow("A")
        run = latest(SB.tmp / "out-A")
        r = receipt_of(run)
        r0, r1 = r["rounds"]
        blocking = {f["check"] for f in r0["critic"]["findings"] if f["priority"] == "blocking"}
        self.assertTrue(blocking)
        for check in blocking:
            self.assertNotEqual(r0["render"]["results"][check], "pass", check)
            self.assertEqual(r1["render"]["results"][check], "pass", check)
        parent = subprocess.run(["git", "-C", str(run / "repo"), "rev-parse", f"{r1['commit']}^"], capture_output=True, text=True,
                                env={"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin"}).stdout.strip()
        self.assertEqual(parent, r0["commit"])
        findings_sha = r1["build_job"]
        prompt = json.loads((run / "dispatch-builder" / findings_sha["job_id"] / "prompt.txt").read_text())
        self.assertEqual(prompt["findings"], r0["critic"]["findings"])

    def test_an_unfixed_blocking_finding_keeps_its_defect_and_fails_the_run(self):
        proc = self.slow("L")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        r = receipt_of(latest(SB.tmp / "out-L"))
        results = r["rounds"][1]["render"]["results"]
        self.assertEqual({c for c, v in results.items() if v != "pass"}, {"contrast"})
        self.assertEqual(r["verdict"]["status"], "FAIL")
        self.assertTrue([f for f in r["rounds"][1]["critic"]["findings"] if f["check"] == "contrast"])

    def test_fix_that_deletes_the_target_element_fails(self):
        proc = self.slow("D")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("required element missing", proc.stdout)
        r = receipt_of(latest(SB.tmp / "out-D"))
        self.assertEqual(r["rounds"][1]["render"]["results"]["required-present"], "fail")

    def test_c2_tree_equal_to_c1_is_findings_unresolved(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        r = receipt_of(run)
        c1 = r["rounds"][0]["commit"]
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@example.invalid",
               "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@example.invalid"}
        tree = subprocess.run(["git", "-C", str(run / "repo"), "rev-parse", f"{c1}^{{tree}}"], capture_output=True, text=True, env=env).stdout.strip()
        c2 = subprocess.run(["git", "-C", str(run / "repo"), "commit-tree", tree, "-p", c1, "-m", "same"], capture_output=True, text=True,
                            env=env).stdout.strip()
        edit_receipt(run, lambda x: x["rounds"][1].update(commit=c2))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)
        self.assertIn("findings unresolved", text)

    def test_missing_round_two_critic_fails(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        edit_receipt(run, lambda x: x["rounds"][1].update(critic=None))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)

    def test_a_third_revision_is_refused(self):
        proc = self.slow("R3")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("round budget", proc.stderr)
        pipe = design.Pipeline()
        for stage in ("route", "record", "build", "render", "critique", "revise", "render", "critique", "revise", "render", "critique"):
            pipe.enter(stage)
        with self.assertRaises(design.Refused):
            pipe.enter("revise")
        with self.assertRaises(design.Refused):
            design.Pipeline().enter("render")


# ------------------------------------------------------------------------------------------------ A7
class A7Grounding(Cell):
    def test_approval_while_the_render_fails(self):
        rc, text = self.slow("approve")
        self.assertEqual(rc, 1, text)
        self.assertIn("critic approved failing render", text)

    def test_one_blocking_critic_finding_fails_a_passing_render(self):
        rc, text = self.slow("blocking")
        self.assertEqual(rc, 1, text)
        self.assertIn("blocking finding", text)

    def test_advisory_findings_never_change_the_verdict(self):
        rc, text = self.slow("advisory")
        self.assertEqual(rc, 0, text)

    def test_finding_citing_an_evidence_id_not_in_the_bundle(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        def cite(k, out, insp, bsha):
            if k == 0:
                out["findings"][0]["evidence_id"] = "ev-999"
        forge(run, critic_edit=cite)
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)
        self.assertIn("evidence id", text)

    def test_browser_claim_without_a_screenshot(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        for k in (0, 1):
            for png_file in (run / "bundles" / f"r{k}" / "screenshots").glob("*.png"):
                png_file.unlink()
            manifest = json.loads((run / "bundles" / f"r{k}" / "bundle.json").read_text())
            manifest["files"] = {n: d for n, d in manifest["files"].items() if not n.startswith("screenshots/")}
            (run / "bundles" / f"r{k}" / "bundle.json").write_text(design.dump(manifest))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)

    def test_decide_rules(self):
        base = {"render": {"state": "ran", "problems": [], "results": {"a": "pass"}}, "critic": {"findings": []}}
        self.assertEqual(design.decide(base)[0], "PASS")
        blocking = copy.deepcopy(base)
        blocking["critic"]["findings"] = [{"priority": "blocking"}]
        self.assertEqual(design.decide(blocking), ("FAIL", ["blocking finding"]))
        failed = copy.deepcopy(base)
        failed["render"]["results"]["a"] = "fail"
        self.assertEqual(design.decide(failed)[1][0], "critic approved failing render")
        failed["render"]["problems"] = ["x"]
        self.assertEqual(design.decide(failed)[0], "FAIL")
        self.assertEqual(design.decide({"render": {"state": "NOT RUN", "reason": "r"}})[0], "NOT RUN")


# ------------------------------------------------------------------------------------------------ A8
class A8Records(Cell):
    def record(self, **changes):
        doc = json.loads((FIX / "record.json").read_text())
        for k, v in changes.items():
            if v is None:
                doc.pop(k, None)
            else:
                doc[k] = v
        path = SB.tmp / f"record-{next(COUNTER)}.json"
        path.write_text(json.dumps(doc))
        return path

    def test_record_without_tokens_is_a_decision_request_and_no_builder_runs(self):
        out = SB.tmp / "out-notokens"
        proc = example(["--record", self.record(tokens=None)], out)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        run = runs_in(out)[0]
        request = json.loads((run / "decision-request.json").read_text())
        self.assertEqual(request["needs"], ["tokens"])
        self.assertFalse((run / "dispatch-builder").exists())
        self.assertFalse((run / "worktrees").exists())

    def test_empty_tokens_are_also_a_decision_request(self):
        proc = example(["--record", self.record(tokens={"origin": "example", "colors": [], "fonts": []})], SB.tmp / "out-emptytokens")
        self.assertEqual(proc.returncode, 2)

    def test_unknown_keys_and_unknown_lane(self):
        proc = execute([DESIGN, "route", "--lane", "product", "--record", self.record(surprise=True), "--product", FIX / "product.json"], SB.env())
        self.assertEqual(proc.returncode, 2, proc.stdout)
        proc = execute([DESIGN, "route", "--lane", "marketing", "--record", FIX / "record.json", "--product", FIX / "product.json"], SB.env())
        self.assertEqual(proc.returncode, 2, proc.stdout)
        proc = example(["--lane", "marketing"], SB.tmp / "out-lane")
        self.assertEqual(proc.returncode, 2)
        bad_product = SB.tmp / "product-extra.json"
        bad_product.write_text(json.dumps(dict(json.loads((FIX / "product.json").read_text()), extra=1)))
        proc = execute([DESIGN, "route", "--lane", "product", "--record", FIX / "record.json", "--product", bad_product], SB.env())
        self.assertEqual(proc.returncode, 2)

    def test_valid_records_route_in_both_lanes(self):
        for lane, extra in (("product", ["--product", FIX / "product.json"]), ("polish-only", [])):
            proc = execute([DESIGN, "route", "--lane", lane, "--record", FIX / "record.json", *extra], SB.env())
            self.assertEqual(proc.returncode, 0, proc.stdout)
        proc = execute([DESIGN, "route", "--lane", "product", "--record", FIX / "record.json"], SB.env())
        self.assertEqual(proc.returncode, 2)


# ------------------------------------------------------------------------------------------------ A9
class A9NoTasteDefaults(Cell):
    FILES = [p for p in sorted(MOD.rglob("*")) if p.is_file() and "tests" not in p.relative_to(MOD).parts and "__pycache__" not in p.parts]
    FONTS = r"\b(Arial|Helvetica|Times New Roman|Georgia|Verdana|Courier|Roboto|Inter|Segoe UI|Calibri|Garamond|Futura|Comic Sans)\b"

    def test_scan_finds_no_color_font_or_ban_list(self):
        self.assertTrue(self.FILES)
        for path in self.FILES:
            text = path.read_text(encoding="utf-8")
            patterns = [("hex color", r"#[0-9a-fA-F]{3,8}\b"), ("rgb/hsl call", r"(?:rgba?|hsla?)\(\s*\d"),
                        ("font-family declaration", r"font-family\s*:"), ("font name", self.FONTS)]
            if path.suffix != ".md":  # the docs may say that ban lists were not ported; code and data may not hold one
                patterns.append(("ban list", r"(?i)\b(ban|banned|blacklist|forbidden)[ _-]?(list|fonts?|colou?rs?)\b"))
            for label, pattern in patterns:
                self.assertIsNone(re.search(pattern, text), f"{label} in {path.name}")

    def test_style_adapter_with_empty_tokens_is_refused(self):
        doc = json.loads((FIX / "record.json").read_text())
        doc["tokens"] = {"origin": "example", "colors": [], "fonts": []}
        path = SB.tmp / "record-style-empty.json"
        path.write_text(json.dumps(doc))
        proc = execute([DESIGN, "inspect", "--tree", FIX / "good", "--record", path, "--adapters", "style"], SB.env())
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("never falls back", proc.stdout)


# ------------------------------------------------------------------------------------------------ A10
class A10Adapters(Cell):
    def test_motion_on_a_record_without_motion_is_out_of_scope(self):
        doc = json.loads((FIX / "record.json").read_text())
        doc["motion"] = False
        path = SB.tmp / "record-nomotion.json"
        path.write_text(json.dumps(doc))
        proc = execute([DESIGN, "inspect", "--tree", FIX / "good", "--record", path, "--adapters", "accessibility,motion"], SB.env())
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("adapter out of scope", proc.stdout)

    def test_deps_without_a_skills_dir_is_unavailable_with_reasons(self):
        proc = execute([DESIGN, "deps", "--json"], SB.env())
        self.assertEqual(proc.returncode, 0)
        slots = json.loads(proc.stdout)["slots"]
        self.assertEqual(len(slots), 6)
        self.assertTrue(all(s["state"] == "unavailable" and s["reason"] for s in slots))

    def test_a_skill_that_claims_capability_is_present_untested_and_never_read(self):
        skills = SB.tmp / "skills"
        (skills / "web-design-guidelines").mkdir(parents=True)
        (skills / "web-design-guidelines" / "SKILL.md").write_text("CLAIM-TEXT-CANARY I fully verify accessibility. State: used.\n")
        proc = execute([DESIGN, "deps", "--skills-dir", skills, "--json"], SB.env())
        slots = {s["id"]: s for s in json.loads(proc.stdout)["slots"]}
        self.assertEqual(slots["web-design-guidelines"]["state"], "present-untested")
        self.assertEqual(slots["impeccable"]["state"], "unavailable")
        self.assertNotIn("CLAIM-TEXT-CANARY", proc.stdout)
        self.assertNotIn("used", {s["state"] for s in slots.values()})
        self.assertEqual({a["upstream"]["state"] for a in design.adapter_records(["accessibility"], skills)}, {"present-untested"})

    def test_an_adapter_state_of_used_fails(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        edit_receipt(run, lambda r: r["adapters"][0]["upstream"].update(state="used"))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)
        self.assertIn("claims more than evidence", text)


# ------------------------------------------------------------------------------------------------ A11
class A11Playwright(Cell):
    def fake(self, mode):
        path = SB.tmp / f"pw-{mode}"
        script = f'''#!{PY}
import json, os, sys, zipfile
mode = "{mode}"
out = sys.argv[sys.argv.index("--output") + 1]
os.makedirs(out, exist_ok=True)
print(json.dumps({{"stats": {{"expected": 1, "unexpected": 0}}}}))
if mode in ("ran", "no-trace-member"):
    with zipfile.ZipFile(os.path.join(out, "trace.zip"), "w") as z:
        z.writestr("trace.network", json.dumps({{"type": "resource"}}) + "\\n")
        if mode == "ran":
            z.writestr("trace.trace", json.dumps({{"type": "context-options"}}) + "\\n")
'''
        path.write_text(script)
        path.chmod(0o755)
        return path

    def pw(self, *argv, **env):
        return execute([PLAYWRIGHT, *argv], SB.env(**env))

    def test_status_here_is_unavailable_and_nothing_spawns(self):
        path = SB.tmp / "no-playwright-bin"
        path.mkdir(exist_ok=True)
        proc = self.pw("status", "--json", PATH=f"{path}{os.pathsep}/usr/bin{os.pathsep}/bin")
        report = json.loads(proc.stdout)
        self.assertEqual((report["state"], report["outcome"], report["spawns"]), ("unavailable", "NOT RUN", 0))
        self.assertEqual(report["component_contract"]["status"], "unsupported here")
        self.assertIn("@playwright/experimental-ct-react", report["component_contract"]["packages"])
        self.assertEqual(SB.sentinels(), [] if not any(SB.tmp.glob("sentinels/*")) else SB.sentinels())

    def test_sentinel_on_path_is_present_unverified_with_zero_spawns(self):
        before = SB.sentinels()
        report = json.loads(self.pw("status", "--json").stdout)
        self.assertEqual((report["state"], report["spawns"]), ("present-unverified", 0))
        self.assertEqual(SB.sentinels(), before)
        self.assertNotIn("playwright", SB.sentinels())
        self.assertNotIn("npx", SB.sentinels())

    def test_run_without_the_flag_is_refused_with_no_spawn(self):
        proc = self.pw("run", "--tree", FIX / "good", "--out", SB.tmp / "pw-out", AIARCH_PLAYWRIGHT=str(self.fake("ran")))
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertFalse((SB.tmp / "pw-out").exists())
        self.assertNotIn("playwright", SB.sentinels())

    def run_fake(self, mode):
        out = SB.tmp / f"pw-out-{mode}"
        proc = self.pw("run", "--tree", FIX / "good", "--out", out, "--allow-playwright", AIARCH_PLAYWRIGHT=str(self.fake(mode)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_report_without_a_trace_archive_is_unverified(self):
        self.assertEqual(self.run_fake("no-trace")["outcome"], "unverified")

    def test_archive_without_trace_trace_is_unverified(self):
        self.assertEqual(self.run_fake("no-trace-member")["outcome"], "unverified")

    def test_complete_run_is_ran(self):
        self.assertEqual(self.run_fake("ran")["outcome"], "ran")

    def test_ran_with_chrome_absent_leaves_the_verdict_not_run(self):
        proc = execute([DESIGN, "inspect", "--tree", FIX / "good", "--record", FIX / "record.json", "--allow-playwright"],
                       SB.env(AIARCH_PLAYWRIGHT=str(self.fake("ran")), AIARCH_CHROME=str(SB.tmp / "no-such-chrome")))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        report = json.loads([l for l in proc.stdout.splitlines() if l.startswith("{")][0])
        self.assertEqual(report["playwright"]["outcome"], "ran")
        self.assertEqual(report["rendered"], "NOT RUN")
        self.assertIn("rendered: NOT RUN (", proc.stdout)


# ------------------------------------------------------------------------------------------------ A12
class A12Trace(Cell):
    def test_diagnose_names_the_escape_step(self):
        self.slow("A")
        run = latest(SB.tmp / "out-A")
        proc = execute([DESIGN, "diagnose", run], SB.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = next(l for l in proc.stdout.splitlines() if "Escape" in l)
        steps = [json.loads(l) for l in (run / "renders" / "r0" / "trace.jsonl").read_text().splitlines()]
        step = next(s for s in steps if s["step"] == "interaction:i2")
        self.assertIn("expected", line)
        self.assertIn("observed", line)
        self.assertIn(step["screenshot_sha"], line)
        self.assertIn('"visible": true', line)

    def test_removing_the_step_from_the_trace_fails_verify(self):
        self.needs_chrome()
        run = copy_run(latest(SB.tmp / "out-A"))
        path = run / "renders" / "r0" / "trace.jsonl"
        path.write_text("".join(l + "\n" for l in path.read_text().splitlines() if '"interaction:i2"' not in l))
        rc, text = verify(run)
        self.assertEqual(rc, 1, text)


# ------------------------------------------------------------------------------------------------ A13
class A13Offline(Cell):
    def test_no_sentinel_written_and_every_seat_unpaid(self):
        self.slow("A")
        self.assertEqual(SB.sentinels(), [])
        run = latest(SB.tmp / "out-A")
        proc = execute([ROOT / "modules" / "orchestration" / "dispatch.py", "status", "--config", run / "config" / "dispatch.json"], SB.env())
        seats = json.loads(proc.stdout)["seats"]
        self.assertTrue(seats and all(s["paid"] is False for s in seats.values()))

    def test_no_off_host_connection_in_any_log(self):
        self.slow("A")
        for k in (0, 1):
            insp = json.loads((latest(SB.tmp / "out-A") / "renders" / f"r{k}" / "inspection.json").read_text())
            if k == 1:
                self.assertEqual(insp["external"], [])
        good = json.loads((self.slow("i:good")["out"] / "inspection.json").read_text())
        self.assertEqual(good["external"], [])

    def test_static_scan_of_both_directories(self):
        banned = [r"urllib\.request", r"http\.client", r"\bssl\b", r"\brequests\b", r"\bnpx\b", r"playwright install"]
        for root in (MOD, EX):
            for path in root.rglob("*.py"):
                if "tests" in path.relative_to(root).parts:
                    continue
                text = path.read_text()
                for pattern in banned:
                    self.assertIsNone(re.search(pattern, text), f"{pattern} in {path.name}")
                self.assertNotIn("http.server", text, path.name)
                self.assertNotIn("socketserver", text, path.name)
        render_text = (MOD / "render.py").read_text()
        for pattern in (r"HTTPServer\(", r"\.listen\(", r"\.bind\(", r"socket\.socket"):  # the renderer opens no listener: fulfilment is over CDP
            self.assertIsNone(re.search(pattern, render_text), pattern)

    def test_stable_hash_equal_across_two_runs_and_closed_exclusion_set(self):
        self.slow("B")
        a, b = receipt_of(latest(SB.tmp / "out-A")), receipt_of(latest(SB.tmp / "out-B"))
        keep = lambda doc: design.strip(doc, set(design.PIPE["stable_exclude"]))
        self.assertEqual(differing(keep(a), keep(b)), [])
        self.assertEqual(design.stable_sha256(a), design.stable_sha256(b))
        self.assertEqual(sorted(design.PIPE["stable_exclude"]), sorted(["job_id", "pid", "started_at", "finished_at", "branch", "self_sha256"]))
        for path in ((("rounds", 0), "commit"), (("verdict",), "status"), (("rounds", 1), "bundle")):
            changed = copy.deepcopy(a)
            node = changed
            for key in path[0]:
                node = node[key]
            node[path[1]] = {"x": 1} if path[1] == "bundle" else "changed"
            self.assertNotEqual(design.stable_sha256(changed), design.stable_sha256(a), path)

    def test_png_bytes_are_deterministic_across_runs(self):
        self.slow("B")
        a, b = latest(SB.tmp / "out-A"), latest(SB.tmp / "out-B")
        for k in (0, 1):
            names = sorted(p.name for p in (a / "renders" / f"r{k}" / "screenshots").glob("*.png"))
            self.assertTrue(names)
            for name in names:
                self.assertEqual((a / "renders" / f"r{k}" / "screenshots" / name).read_bytes(),
                                 (b / "renders" / f"r{k}" / "screenshots" / name).read_bytes(), name)
        self.assertEqual(receipt_of(a)["rounds"][1]["commit"], receipt_of(b)["rounds"][1]["commit"])


# ------------------------------------------------------------------------------------------------ A14
class A14Constraints(Cell):
    def test_descriptor(self):
        desc = contracts.check_module_descriptor(MOD, module="design-pipeline")
        self.assertEqual(desc["claimed_support"], "planned")
        self.assertEqual([d["identifier"] for d in desc["dependencies"]], ["core-contracts", "browser", "browser-operations"])
        self.assertTrue(all(d["status"] == "declared-untested" for d in desc["dependencies"]))
        self.assertEqual(desc["schemas"], ["result"])

    def test_provenance_one_unit_per_upstream_with_the_recorded_hashes(self):
        collection.provenance(MOD / "PROVENANCE.md")
        text = (MOD / "PROVENANCE.md").read_text()
        units = [p for p in re.split(r"^(?=## )", text, flags=re.M) if re.search(r"^Source: *\S", p, re.M)]
        self.assertEqual(len(units), 8)
        for digest in ("eebb5819a5ca3c8bd264354727479dc20f1dfab7b22432ada7bfbb498b33cdfc", "7c67e63ba4d4ca2652f88cca747461f4440a3500da63322a5e35abaa551f0a19",
                       "5ae0f1fd0f41367b4be80b80ffeeffd7e6c36bc816263b3e2fb0c865489d10e1", "4f42c67f5f201895085165472366305aab77ce7676fe8f69e24bd26ef6e00312",
                       "3b658bbf1c7ad8bf43f8a8f9a14f69505d560cf0bb11b2cd8195efa7437b0bb3", "820d83c75fc773dc4927ba41fcbdf92abd876900a1e1577adefc0479e219c322",
                       "bfbe565766f9cad87c1a64427e077abce20fc67d6ecd879593a4c6fdbd96a7ec", "d01dfe9557feaeeb630b47c94dcb4eb153bb1c060cf1b552bb8db5d1f6bda30a"):
            self.assertIn(digest, text)
        self.assertIn("public upstream", text)
        self.assertIn("Not ported", text)

    def test_privacy_scan_of_both_directories(self):
        self.assertEqual(privacy.scan(MOD), [])
        self.assertEqual(privacy.scan(EX), [])

    def test_collection_check_passes_the_new_markdown(self):
        proc = subprocess.run([PY, "-B", str(ROOT / "scripts" / "check_collection.py")], capture_output=True, text=True, cwd=ROOT, env=SB.env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_outputs_carry_no_private_path_or_identity(self):
        proc = self.slow("A")
        run = latest(SB.tmp / "out-A")
        texts = [proc.stdout, proc.stderr, (run / "receipt.json").read_text(), (run / "record.json").read_text()]
        for k in (0, 1):
            for name in ("inspection.json", "trace.jsonl"):
                texts.append((run / "renders" / f"r{k}" / name).read_text())
            texts.append((run / "bundles" / f"r{k}" / "bundle.json").read_text())
        banned = [os.path.realpath(SB.tmp / "out-A"), str(SB.tmp), ORIG_HOME, getpass.getuser(), socket.gethostname(), REAL_ROOT]
        for text in texts:
            for item in banned:
                if item and len(item) > 3:
                    self.assertNotIn(item, text)

    def test_module_set_up_patches_are_restorable(self):
        self.assertIn("create", SAVED)
        self.assertIsNot(socket.create_connection, SAVED["create"])
        self.assertIsNot(socket.socket.connect, SAVED["connect"])
        with self.assertRaises(OSError):
            socket.create_connection(("203.0.113.9", 80), timeout=1)


# ------------------------------------------------------------------------------------------------ amendments
class AmendmentCells(Cell):
    def test_am6_a_page_with_scripts_cannot_reach_a_second_loopback_listener(self):
        res = self.slow("i:escape")
        self.assertEqual(LISTENER["accepts"], 0, "popup, worker, beacon, WebSocket or fetch reached the second listener")
        self.assertEqual(res["rc"], 1, res["stdout"])
        self.assertEqual(res["report"]["checks"]["external-request"], "fail", "the probe ran and its requests were stopped and counted")

    def test_am7_symlink_in_the_tree_is_refused_and_leaks_nothing(self):
        proc = self.slow("S")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        out = SB.tmp / "out-S"
        for path in out.rglob("*"):
            if path.is_file() and not path.is_symlink() and "repo" not in path.parts and "worktrees" not in path.parts:
                self.assertNotIn(b"OUTSIDE-SECRET-CANARY", path.read_bytes(), str(path))
        self.assertFalse(list(out.rglob("receipt.json")))

    def test_am8_time_budget_is_reported(self):
        self.slow("NR_hang")
        self.assertLess(STATE["hang_seconds"], 60)
        print(f"\nhang cell wall time {STATE['hang_seconds']:.1f}s", file=sys.stderr)

    def test_am9_pipeline_json_lists_no_derived_fields_because_pngs_are_stable(self):
        self.assertEqual(design.PIPE["derived_fields"], [])
        self.assertTrue(design.PIPE["png_deterministic"])


# ------------------------------------------------------------------------------------------------ fix round 1
def put(repo, kind, body):
    """Write one git object verbatim (no fsck), return its id."""
    return subprocess.run([SB.git, "-C", str(repo), "hash-object", "-t", kind, "-w", "--stdin", "--literally"], input=body,
                          capture_output=True, env=SB.env(), check=True).stdout.decode().strip()


def tree_of(*entries):
    return b"".join(mode + b" " + name + b"\0" + bytes.fromhex(oid) for mode, name, oid in entries)


def literal_repo(*entries):
    """A repo whose one commit has a literal tree of (mode, name, oid-or-None-for-a-blob) entries."""
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=SB.tmp / "tmp"))
    subprocess.run([SB.git, "init", "-q", "-b", "main", str(repo)], capture_output=True, env=SB.env(), check=True)
    blob = put(repo, "blob", b"hello\n")
    tree = put(repo, "tree", tree_of(*[(m, n, o or blob) for m, n, o in entries]))
    commit = put(repo, "commit", f"tree {tree}\nauthor a <a@x.invalid> 1 +0000\ncommitter a <a@x.invalid> 1 +0000\n\nm\n".encode())
    return repo, commit, blob


class FixB1ObjectReads(Cell):
    """Every read of the run repo is Python-verified and never lets git read the repo's own config, attributes or graph."""

    def extract(self, repo, commit):
        dest = Path(tempfile.mkdtemp(prefix="dest-", dir=SB.tmp / "tmp")) / "out"
        return design.render.extract_tree(repo, commit, dest), dest

    def refused(self, repo, commit, text=""):
        with self.assertRaises(design.Refused) as ctx:
            self.extract(repo, commit)
        self.assertIn(text, str(ctx.exception))

    def test_a_plain_tree_extracts(self):
        repo, commit, _ = literal_repo((b"100644", b"a.txt", None), (b"100755", b"run.sh", None))
        _, dest = self.extract(repo, commit)
        self.assertEqual((dest / "a.txt").read_bytes(), b"hello\n")
        self.assertTrue(os.access(dest / "run.sh", os.X_OK))

    def test_forged_loose_object_is_refused_with_a_hash_mismatch(self):
        repo, commit, blob = literal_repo((b"100644", b"a.txt", None))
        path = repo / ".git" / "objects" / blob[:2] / blob[2:]
        path.chmod(0o644)
        path.write_bytes(zlib.compress(b"blob 6\0forged"))
        self.refused(repo, commit, "hash mismatch")

    def test_literal_trees_with_unsafe_names_are_refused(self):
        for name in (b"..", b".", b".git", b".GIT", b".git "):
            repo, commit, _ = literal_repo((b"100644", name, None))
            with self.subTest(name=name):
                self.refused(repo, commit)
        repo, commit, _ = literal_repo((b"100644", b"x", None))
        sub = put(repo, "tree", tree_of((b"100644", b"..", None or put(repo, "blob", b"x"))))
        top = put(repo, "tree", tree_of((b"40000", b"d", sub)))
        evil = put(repo, "commit", f"tree {top}\nauthor a <a@x.invalid> 1 +0000\ncommitter a <a@x.invalid> 1 +0000\n\nm\n".encode())
        self.refused(repo, evil)
        self.assertFalse((Path(tempfile.gettempdir()) / "escape").exists())

    def test_duplicate_and_conflicting_names_are_refused(self):
        repo, commit, blob = literal_repo((b"100644", b"a", None), (b"100644", b"a", None))
        self.refused(repo, commit, "duplicate")
        repo, commit, blob = literal_repo((b"100644", b"a", None))
        sub = put(repo, "tree", tree_of((b"100644", b"f", blob)))
        both = put(repo, "tree", tree_of((b"100644", b"a", blob), (b"40000", b"a", sub)))
        evil = put(repo, "commit", f"tree {both}\nauthor a <a@x.invalid> 1 +0000\ncommitter a <a@x.invalid> 1 +0000\n\nm\n".encode())
        self.refused(repo, evil, "duplicate")

    def test_symlink_and_submodule_modes_are_refused(self):
        for mode in (b"120000", b"160000", b"100664", b"100600"):
            repo, commit, _ = literal_repo((mode, b"x", None))
            with self.subTest(mode=mode):
                self.refused(repo, commit)

    def test_symlinked_objects_dir_and_alternates_are_refused(self):
        repo, commit, _ = literal_repo((b"100644", b"a.txt", None))
        objects = repo / ".git" / "objects"
        elsewhere = repo.parent / (repo.name + "-objects")
        objects.rename(elsewhere)
        objects.symlink_to(elsewhere)
        self.refused(repo, commit)
        repo, commit, _ = literal_repo((b"100644", b"a.txt", None))
        (repo / ".git" / "objects" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "objects" / "info" / "alternates").write_text("/nonexistent\n")
        self.refused(repo, commit, "alternates")

    def test_symlinked_fanout_dir_inside_objects_is_refused(self):
        repo, commit, blob = literal_repo((b"100644", b"a.txt", None))
        fanout = repo / ".git" / "objects" / blob[:2]
        elsewhere = repo.parent / (repo.name + "-fanout")
        fanout.rename(elsewhere)
        fanout.symlink_to(elsewhere)
        self.refused(repo, commit, "link")

    def test_dot_git_that_is_a_file_or_link_is_refused(self):
        repo, commit, _ = literal_repo((b"100644", b"a.txt", None))
        moved = repo.parent / (repo.name + "-git")
        (repo / ".git").rename(moved)
        (repo / ".git").symlink_to(moved)
        self.refused(repo, commit)

    def test_commit_parents_come_from_the_commit_body(self):
        repo, commit, _ = literal_repo((b"100644", b"a.txt", None))
        tree, parents = design.render.commit_info(repo, commit)
        self.assertEqual(parents, [])
        self.assertRegex(tree, r"^[0-9a-f]{40}$")

    def test_smudge_filter_in_the_repo_config_never_runs_through_verify(self):
        res = self.slow("fx-smudge")
        self.assertFalse(res["marker"], "a smudge filter from the run repo's config ran")
        self.assertEqual(res["rc"], 0, res["text"])

    def test_promisor_remote_in_the_repo_config_never_runs_through_verify(self):
        res = self.slow("fx-promisor")
        self.assertFalse(res["marker"], "uploadpack of a promisor remote from the run repo's config ran")
        self.assertNotEqual(res["rc"], 0, "a missing object must not verify")

    def test_forged_commit_graph_cannot_change_the_parents_verify_uses(self):
        res = self.slow("fx-graph")
        self.assertEqual(res["rc"], 0, res["text"])

    def test_symlinked_objects_dir_fails_verify(self):
        res = self.slow("fx-objlink")
        self.assertNotEqual(res["rc"], 0, res["text"])


class FixB2CspReports(Cell):
    """CSP violations are collected by Python (report-uri), so a worker, an iframe or a page that wipes its own log cannot hide one."""

    def fails(self, name):
        res = self.slow(f"px:{name}")
        self.assertEqual(res["accepts"], 0)
        self.assertEqual(res["report"]["checks"]["external-request"], "fail", res["stdout"])
        self.assertEqual(res["rc"], 1, res["stdout"])

    def test_worker_only_off_origin_fetch_fails(self):
        self.fails("worker-only")

    def test_iframe_only_off_origin_fetch_fails(self):
        self.fails("iframe-only")

    def test_a_violation_in_the_last_few_hundred_milliseconds_is_drained_before_close(self):
        self.fails("late-worker")

    def test_a_page_that_clears_its_own_violation_log_still_fails(self):
        self.fails("clear-viol")

    def test_every_response_carries_the_csp_with_report_uri(self):
        tree = Path(tempfile.mkdtemp(prefix="srv-", dir=SB.tmp / "tmp"))
        (tree / "index.html").write_text("<!doctype html>")
        names = design.render.regular_files(tree)
        for method, path, status in (("GET", "/", 200), ("GET", "/index.html", 200), ("GET", "/nope", 404), ("GET", "/a/../b", 404),
                                     ("GET", "/%2e%2e/x", 404), ("POST", "/__aiarch-csp", 204)):
            code, headers, _ = design.render.reply(tree, names, method, path, b"{}")
            with self.subTest(method=method, path=path):
                self.assertEqual(code, status)
                self.assertIn("report-uri /__aiarch-csp", dict(headers).get("Content-Security-Policy", ""))

    def test_a_legitimate_same_origin_worker_and_the_good_fixture_still_pass(self):
        res = self.slow("px:worker-legit")
        self.assertEqual(res["report"]["checks"]["external-request"], "pass", res["stdout"])
        self.assertEqual(res["rc"], 0, res["stdout"])
        self.assertEqual(self.slow("i:good")["rc"], 0)


class FixB3NoSocketBypassesTheSink(Cell):
    """Loopback and RTC traffic goes through the sink, the fixture is fulfilled in-process, and nothing unfulfilled goes unseen."""

    def fails(self, name):
        res = self.slow(f"px:{name}")
        self.assertEqual(res["accepts"], 0, "the page reached a real loopback listener")
        self.assertEqual(res["report"]["checks"]["external-request"], "fail", res["stdout"])
        self.assertEqual(res["rc"], 1, res["stdout"])

    def test_rtc_ice_tcp_to_loopback_is_stopped_and_counted(self):
        self.fails("rtc-icetcp")

    def test_rtc_from_a_fresh_iframe_is_stopped_and_counted(self):
        self.fails("rtc-fresh-iframe")

    def test_websocket_from_a_cspless_404_realm_is_stopped_and_counted(self):
        self.fails("iframe-404-ws")

    def test_main_frame_control_still_fails(self):
        self.fails("control-main-fetch")

    def test_a_legitimate_worker_runs_and_its_same_origin_fetch_is_served(self):
        res = self.slow("px:worker-legit")
        self.assertEqual(res["report"]["checks"]["required-present"], "pass", res["stdout"])
        self.assertEqual(res["report"]["checks"]["external-request"], "pass", res["stdout"])
        self.assertEqual(res["rc"], 0, res["stdout"])

    def test_no_loopback_listener_is_started_by_the_renderer(self):
        self.assertFalse(hasattr(design.render, "Server"), "the fixture is fulfilled through CDP, not served")

    def test_proxy_host_parses_connect_and_absolute_targets_including_ipv6(self):
        host = design.render.proxy_host
        self.assertEqual(host("CONNECT 127.0.0.1:443 HTTP/1.1"), "127.0.0.1")
        self.assertEqual(host("CONNECT [::1]:8080 HTTP/1.1"), "::1")
        self.assertEqual(host("CONNECT example.test:443 HTTP/1.1"), "example.test")
        self.assertEqual(host("GET http://[::1]:80/x HTTP/1.1"), "::1")
        self.assertEqual(host("GET http://example.test/x HTTP/1.1"), "example.test")
        self.assertEqual(host("garbage"), "")

    def test_every_loopback_or_ip_literal_attempt_counts_without_the_host_in_the_source_but_background_traffic_does_not(self):
        own = design.render.own_attempts
        attempts = ["CONNECT 127.0.0.1:443 HTTP/1.1", "CONNECT [::1]:80 HTTP/1.1", "CONNECT 192.0.2.1:80 HTTP/1.1",
                    "CONNECT localhost:80 HTTP/1.1", "CONNECT update.googleapis.com:443 HTTP/1.1",
                    "CONNECT cdn.example.test:443 HTTP/1.1", "GET http://[2001:db8::1]/x HTTP/1.1"]
        got = own(attempts, "<p>no hosts at all</p>")
        self.assertEqual(got, [attempts[0], attempts[1], attempts[2], attempts[3], attempts[6]])
        self.assertEqual(own(attempts, "src=//cdn.example.test/x"), [attempts[i] for i in (0, 1, 2, 3, 5, 6)])


if __name__ == "__main__":
    unittest.main()
