#!/usr/bin/env python3
"""discovery-inbox: drive a discovery companion you installed through its own commands, and keep receipts only.

Stdlib only; no network code, no model. The companion owns candidate status, your plan pack owns the work, and this
module appends derived receipts (discovery.jsonl) to a 0700 out dir. Subcommands: sandbox, candidate, handoff, inbox
(list|intake), decide, roundtrip, companions. Exit codes: 0 ok, 1 failed or invalid, 2 refused, 3 a required companion
receipt is missing (AIARCH_REQUIRE_COMPANION=1).
"""

import argparse
import datetime
import hashlib
import html
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


companion = _load("di_companion", HERE / "companion.py")
knowledge = _load("di_knowledge", HERE.parent / "knowledge-adapters" / "knowledge.py")
Refuse = companion.Refuse
INTERFACE = json.loads((HERE / "interface.json").read_text())
LABELS = tuple(INTERFACE["body_labels"])
MAX_BRIEF = INTERFACE["max_brief_bytes"]
ID = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
FILE = re.compile(r"[A-Za-z0-9._-]+__[A-Za-z0-9._-]+\.md")
FIELD = re.compile(r"\*\*([^*:]+):\*\* ?(.*)")
SECTION = re.compile(r"^## Handoff: .*\n.*?(?=^## |\Z)", re.M | re.S)
STEWARD = HERE.parent / "stewardship" / "steward.py"
STATE_EXPORT = HERE.parent / "state-adapters" / "state_export.py"
VIEWER = HERE.parent / "state-adapters" / "viewer.py"
KNOWLEDGE = HERE.parent / "knowledge-adapters" / "knowledge.py"
OWNERS = {"21-graph": "knowledge-adapters", "22-brain": "knowledge-adapters", "64-static-viewer": "state-adapters",
          "64-external-viewer": "state-adapters", "70-discovery": "discovery-inbox"}
CAPABILITY = {"21-graph": "21", "22-brain": "22", "64-static-viewer": "64", "64-external-viewer": "64", "70-discovery": "70"}
SETUP = {
    "21-graph": "install a graph backend whose command resolves on PATH and pass it with --graphify",
    "22-brain": "provide a backend that speaks the brain JSON handshake (op handshake and status) and a permission file, "
                "then run the knowledge-adapters brain handshake against it with an attested network denial",
    "64-external-viewer": "install a viewer that reads a disposable state export (schema_version 1) without writing to any "
                          "live service, then describe it in a state-adapters companion manifest and pass --viewer-companion",
    "70-discovery": "install a discovery companion, describe it in a handoff-brief/1 manifest, and run roundtrip "
                    "with --backend companion against a disposable project",
}


class Fail(Exception):
    """A failed step (exit 1): the state was readable but the result is not acceptable."""


def esc(text, limit=2000):
    out = []
    for char in text:
        code = ord(char)
        if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp"):
            out.append(f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)[:limit]


def stable_sha256(record):
    """Hash of a receipt without its times and run id (paths never enter a receipt)."""
    body = {k: v for k, v in record.items() if k not in ("at", "run")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


# ---- out dir and receipts -------------------------------------------------------------------------------------------

def resolve_out(arg):
    if arg:
        return arg
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(state, "ai-architecture", "discovery", "runs", f"{stamp}-{os.getpid()}")


def append(outfd, record):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (json.dumps({**record, "at": stamp}, sort_keys=True) + "\n").encode()
    fd = os.open("discovery.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=outfd)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def receipts(outfd, event=None):
    try:
        fd = os.open("discovery.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=outfd)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as handle:
        records = [json.loads(line) for line in handle.read().decode().splitlines() if line.strip()]
    return [r for r in records if event is None or r.get("event") == event]


def file_receipt(outfd, name):
    """{path, sha256} of a receipt file inside the out dir (relative path only)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=outfd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return {"path": name, "sha256": hashlib.sha256(handle.read()).hexdigest()}


# ---- project, candidate record --------------------------------------------------------------------------------------

def check_project(args):
    if not args.project or not args.registry:
        raise Refuse("--project and --registry are required")
    project = Path(args.project)
    if project.is_symlink() or not project.is_dir():
        raise Refuse("project must be a real directory")
    real = project.resolve()
    targets = Path(args.targets_root).resolve() if args.targets_root else real.parent
    if real.parent != targets:
        raise Refuse("targets root does not contain the project")
    try:
        data = json.loads(Path(args.registry).read_text(encoding="utf-8"))
        registered = {Path(p["root"]).resolve() for p in data["projects"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise Refuse("registry is unreadable") from None
    if real not in registered:
        raise Refuse("project is not registered")
    return real, targets


def record_path(sandbox, candidate):
    return Path(sandbox) / "data" / "repos" / (candidate.replace("/", "__") + ".md")


def read_status(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    head = text.split("\n---", 1)[0]
    found = re.search(r"(?m)^status:\s*[\"']?([a-z-]+)[\"']?\s*$", head)
    return found.group(1) if found else None


def plain(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[^\x00-\x1f\x7f]{1,300}", value):
        raise Refuse(f"{label} must be one printable line of at most 300 characters")
    return value


def write_candidate(args, manifest, sandbox, project):
    cid = args.id
    if not ID.fullmatch(cid):
        raise Refuse("--id must look like owner/name")
    if args.mode == "pack" and not (project / "plans" / args.topic).is_dir():
        raise Refuse("pack mode needs an existing plans/<topic> directory")
    target, category = (project.name, "claude-system") if args.mode != "pseudo" else ("_inbox", "project-seed")
    pack = args.topic if args.mode == "pack" else ""
    path = record_path(sandbox, cid)
    current = read_status(path) if path.exists() else None
    if current and current != manifest["eligible_status"]:
        raise Fail(f"candidate already decided ({current}); not rewriting it")
    keys = INTERFACE["record_keys"]
    values = {"repo": cid, "status": manifest["eligible_status"], "category": category, "priority": "medium",
              "handoff-to": target, "handoff-pack": pack}
    lines = ["---"]
    for key in keys:
        lines.append(f"{key}: []" if key in ("tags", "need-tags") else f"{key}: {json.dumps(values[key])}")
    lines += ["---", f"**What:** {plain(args.what, '--what')}", f"**Next action:** {plain(args.next_action, '--next-action')}",
              "**Blocks on:** none", ""]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---- brief parsing and inbox scan -----------------------------------------------------------------------------------

class Unsupported(Exception):
    pass


def parse_brief(text):
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("## Handoff:")):
        i += 1
    head = re.fullmatch(r"# Handoff: (\S+)", lines[i]) if i < len(lines) else None
    if not head or not ID.fullmatch(head.group(1)):
        raise Unsupported("unsupported brief: no '# Handoff: owner/name' heading")
    fields = {}
    for line in lines[i + 1:]:
        if not line.strip():
            continue
        found = FIELD.fullmatch(line)
        if not found or found.group(1) not in LABELS or found.group(1) in fields:
            raise Unsupported(f"unsupported brief: unknown or repeated field in {esc(line[:40])!r}")
        fields[found.group(1)] = esc(found.group(2).strip())
    canonical = "\n".join(lines[i:]).strip() + "\n"
    return head.group(1), fields, hashlib.sha256(canonical.encode()).hexdigest()


def read_capped(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise Unsupported("unsupported brief: not readable (a symlink is refused)") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unsupported("unsupported brief: not a regular file")
        data = b""
        while len(data) <= MAX_BRIEF:
            chunk = os.read(fd, MAX_BRIEF + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_BRIEF:
            raise Unsupported(f"brief is over the {MAX_BRIEF // 1024} KiB cap")
        return data.decode("utf-8", "replace")
    finally:
        os.close(fd)


def scan_inbox(project):
    """Entries placed in the project's pack/inbox, read from disk (never from receipts). Returns (entries, refused)."""
    entries, news, refused = {}, [], []
    plans = project / "plans"

    def rel(path):
        return path.relative_to(project.parent).as_posix()

    def refuse(path, reason):
        refused.append({"file": esc(rel(path)), "reason": reason})

    def take(path, text, mode):
        try:
            cid, fields, digest = parse_brief(text)
        except Unsupported as error:
            return refuse(path, str(error))
        entry = {"id": cid, "brief_sha256": digest, "mode": mode, "placement": esc(rel(path)), "fields": fields, "conflict": None}
        if path.name.endswith(".new.md"):
            news.append((entry, path))
        else:
            entries.setdefault((cid, digest), entry)

    def file_in(path, mode, sections):
        base = path.name[:-7] + ".md" if path.name.endswith(".new.md") else path.name
        if not FILE.fullmatch(base) and path.name != "QUESTIONS.md":
            return refuse(path, "file name outside the allowed pattern")
        try:
            text = read_capped(path)
        except Unsupported as error:
            return refuse(path, str(error))
        if sections:
            for match in SECTION.finditer(text):
                take(path, match.group(0), mode)
        else:
            take(path, text, mode)

    if plans.is_dir() and not plans.is_symlink():
        for pack in sorted(plans.iterdir()):
            if pack.is_symlink() or not pack.is_dir():
                continue
            if pack.name == "_inbox":
                for path in sorted(pack.iterdir()):
                    if path.name.endswith(".md"):
                        file_in(path, "inbox", False)
                continue
            questions = pack / "QUESTIONS.md"
            if questions.is_symlink() or questions.is_file():
                file_in(questions, "pack", True)
            for path in sorted(pack.glob("*.new.md")):
                file_in(path, "pack", True)
    for entry, path in news:
        owners = [e for (cid, _), e in entries.items() if cid == entry["id"]]
        if not owners:
            refuse(path, "conflict file without an original")
        elif owners[0]["brief_sha256"] != entry["brief_sha256"] and owners[0]["conflict"] is None:
            owners[0]["conflict"] = {"original_sha256": owners[0]["brief_sha256"], "new_sha256": entry["brief_sha256"]}
    return sorted(entries.values(), key=lambda e: (e["id"], e["brief_sha256"])), refused


def intake(outfd, entries):
    seen = {(r["candidate_id"], r["brief_sha256"]) for r in receipts(outfd, "intake")}
    for entry in entries:
        key = (entry["id"], entry["brief_sha256"])
        if key not in seen:
            seen.add(key)
            append(outfd, {"event": "intake", "candidate_id": entry["id"], "brief_sha256": entry["brief_sha256"],
                           "mode": entry["mode"], "placement": entry["placement"]})
    known = {(r["candidate_id"], r["original_sha256"], r["new_sha256"]) for r in receipts(outfd, "conflict")}
    for entry in entries:
        c = entry["conflict"]
        if c and (entry["id"], c["original_sha256"], c["new_sha256"]) not in known:
            append(outfd, {"event": "conflict", "candidate_id": entry["id"], **c})


# ---- companion commands ---------------------------------------------------------------------------------------------

class Ctx:
    def __init__(self, args):
        self.args = args
        self.manifest = companion.load_manifest(args.companion) if args.companion else None
        self.sandbox = None
        self.runner = None
        self.identity = None

    def need_companion(self):
        if self.manifest is None:
            raise Refuse("--companion is required")
        return self.manifest

    def prepare(self, sandbox=None):
        manifest = self.need_companion()
        self.sandbox = sandbox or self.args.sandbox
        if not self.sandbox:
            raise Refuse("--sandbox is required")
        self.identity = companion.build_sandbox(manifest, self.sandbox)
        self.runner = companion.Runner(manifest, self.sandbox)
        return self.runner

    @property
    def spawns(self):
        return self.runner.count if self.runner else 0


def list_rows(runner, status=None):
    state, out = runner.spawn("list", [status] if status else [])
    if state != "ok":
        raise Fail("companion list failed")
    rows = {}
    for line in out.splitlines():
        tokens = line.split()
        for token in tokens[1:5]:
            if ID.fullmatch(token):
                rows.setdefault(token, tokens[0])
                break
    return rows


def do_handoff(ctx, project, targets):
    state, out = ctx.runner.spawn("handoff", ["--apply", "--targets-root", str(targets)])
    if state != "ok":
        raise Fail("companion handoff failed")
    return companion.parse_handoff(out)


def reverse(manifest, status):
    return next((name for name, value in manifest["dispositions"].items() if value == status), None)


def do_decide(ctx, outfd, project, cid, disposition, backend="fixture"):
    manifest = ctx.manifest
    if disposition not in manifest["dispositions"]:
        raise Refuse("--disposition must be one of " + ", ".join(companion.PUBLIC))
    entries, _ = scan_inbox(project)
    found = [e for e in entries if e["id"] == cid]
    if not found:
        raise Fail("candidate is not in the inbox")
    if len(found) > 1:
        raise Fail("candidate is ambiguous: more than one brief revision is placed")
    if found[0]["conflict"]:
        raise Fail("conflict unresolved: resolve the placed brief before deciding")
    target = manifest["dispositions"][disposition]
    current = read_status(record_path(ctx.sandbox, cid))
    if current == target:
        return {"id": cid, "disposition": disposition, "companion_value": target, "companion_ack": "confirmed",
                "unchanged": True, "spawns": 0}
    if current in manifest["dispositions"].values():
        raise Fail(f"already decided ({reverse(manifest, current)})")
    if current != manifest["eligible_status"]:
        raise Fail("candidate is not in the eligible state")
    state, out = ctx.runner.spawn("set", [cid, "status", target])
    if state != "ok":
        raise Fail("companion set failed")
    if list_rows(ctx.runner, target).get(cid) != target:
        raise Fail("disposition not confirmed by the companion's own list")
    result = {"id": cid, "disposition": disposition, "companion_value": target, "companion_ack": "confirmed", "unchanged": False,
              "spawns": ctx.spawns}
    append(outfd, {"event": "decision", "candidate_id": cid, "brief_sha256": found[0]["brief_sha256"], "disposition": disposition,
                   "companion_value": target, "companion_ack": "confirmed", "isolation": ctx.runner.attest(), "backend": backend})
    return result


def bootstrap(args):
    env = {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()}
    done = subprocess.run([sys.executable, "-B", str(STEWARD), "bootstrap", "--project", args.project, "--registry", args.registry,
                           "--topic", args.topic], capture_output=True, text=True, timeout=120, env=env)
    if done.returncode != 0:
        raise Refuse("stewardship bootstrap refused the project")


def cmd_roundtrip(args, ctx, outfd):
    ctx.need_companion()
    if not args.topic or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.topic):
        raise Refuse("--topic must be a plain token")
    if not args.project or not args.registry:
        raise Refuse("--project and --registry are required")
    bootstrap(args)
    project, targets = check_project(args)
    temp = None
    try:
        if not args.sandbox:
            temp = tempfile.TemporaryDirectory()
            args.sandbox = os.path.join(temp.name, "sandbox")
        ctx.prepare()
        write_candidate(args, ctx.manifest, ctx.sandbox, project)
        file_name = record_path(ctx.sandbox, args.id).name
        first = do_handoff(ctx, project, targets)
        place = [p for p in first["placements"] if p["repo"] == args.id]
        if not place:
            raise Fail("companion did not report a placement for the candidate")
        if place[0]["result"] == "not placed":
            append(outfd, {"event": "handoff", "candidate_id": args.id, "mode": args.mode, "placement": "not placed"})
            return {"placed": False, "candidate_id": args.id, "mode": args.mode, "placement": "not placed", "spawns": ctx.spawns}
        again = do_handoff(ctx, project, targets)
        rows = [r for r in again["rows"] if r["src"].endswith(file_name)]
        placed = [p for p in again["placements"] if p["repo"] == args.id]
        if not (rows and placed and rows[0]["result"] == "unchanged" and placed[0]["result"] == "unchanged"):
            raise Fail("duplicate handoff not idempotent")
        entries, _ = scan_inbox(project)
        intake(outfd, entries)
        mine = [e for e in entries if e["id"] == args.id]
        if len(mine) != 1:
            raise Fail("candidate is not in the inbox exactly once")
        result = do_decide(ctx, outfd, project, args.id, args.disposition, args.backend)
        receipt = {"event": "roundtrip", "candidate_id": args.id, "brief_sha256": mine[0]["brief_sha256"], "mode": args.mode,
                   "placement": place[0]["dest"], "disposition": args.disposition, "companion_value": result["companion_value"],
                   "companion_ack": result["companion_ack"], "duplicate_handoff": "unchanged", "identity": ctx.identity,
                   "isolation": ctx.runner.attest(), "backend": args.backend}
        append(outfd, receipt)
        return {k: v for k, v in receipt.items() if k != "event"} | {"spawns": ctx.spawns}
    finally:
        if temp:
            temp.cleanup()


# ---- companions table -----------------------------------------------------------------------------------------------

def child(argv, tmp, timeout=120):
    done = subprocess.run([sys.executable, "-B", *map(str, argv)], capture_output=True, text=True, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin", "HOME": tmp})
    return done.returncode, done.stdout


def graph_cell(graphify, kdir):
    """Run the host graph backend once under the Python-level denial and record it with knowledge-adapters' own CLI."""
    if not graphify or not os.path.isfile(graphify):
        return "graphify does not resolve (pass --graphify PATH)"
    interpreter = Path(graphify).resolve().parent / "python3"
    if not interpreter.exists():
        return "the graphify interpreter was not found"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "graph.json").write_text(json.dumps({"directed": False, "multigraph": False, "graph": {},
                                                    "nodes": [{"id": "alpha", "label": "alpha"}, {"id": "beta", "label": "beta"}],
                                                    "links": [{"source": "alpha", "target": "beta", "relation": "calls"}]}))
        (tmp / "wrapper.py").write_text(companion.DENY + "import sys\nfrom graphify.__main__ import main\nsys.argv[0] = 'graphify'\nmain()\n")
        (tmp / "probe.py").write_text(companion.DENY + companion.PROBE)
        state, out = companion.graph.run_child([str(interpreter), "-B", str(tmp / "probe.py")], [], 20)
        if out.decode("utf-8", "replace").strip() != "denied":
            return "network denial could not be verified for the graph backend"
        (tmp / "graph-config.json").write_text(json.dumps({
            "argv": [str(interpreter), "-B", str(tmp / "wrapper.py")], "timeout_s": 30, "name": "graphify", "version": "0.0",
            "license": "MIT", "network": False, "kind": "graphify", "graph": str(tmp / "graph.json")}))
        for step in (["graph", "handshake"], ["graph", "query", "--op", "explain", "--arg", "alpha"]):
            rc, _ = child([KNOWLEDGE, *step, "--config", tmp / "graph-config.json", "--isolation", "net-denied", "--out", kdir], str(tmp))
            if rc != 0:
                return "the graph run failed"
    return None


def registry_claims():
    try:
        data = json.loads((ROOT / "capabilities" / "registry.json").read_text())
        return {c["id"]: c["support"] for c in data["capabilities"]}
    except (OSError, ValueError, KeyError):
        return {}


def build_rows(args, outfd, out_path):
    claims, rows = registry_claims(), []
    kdir = os.path.join(out_path, "knowledge")
    os.makedirs(kdir, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        def exercised():
            rc, out = child([KNOWLEDGE, "exercised", "--out", kdir], tmp)
            if rc != 0:
                raise Fail("knowledge-adapters exercised failed")
            return json.loads(out)
        lanes = exercised()
        graph_reason = None
        if lanes["graph"]["status"] != "EXERCISED":
            graph_reason = graph_cell(args.graphify, kdir)
            lanes = exercised()
        # external viewer: the owning module's own listing, never recomputed here
        cmd = [VIEWER, "list", "--format", "json"] + (["--companion", args.viewer_companion] if args.viewer_companion else [])
        rc, out = child(cmd, tmp)
        if rc != 0:
            raise Fail("viewer list failed")
        entry = [v for v in json.loads(out)["viewers"] if v["name"] == "companion"][0]
        viewer_ok = bool(receipts(outfd, "viewer-check") and receipts(outfd, "viewer-check")[-1]["ok"])
        if not viewer_ok:
            viewer_ok = static_viewer_cell(tmp)
            append(outfd, {"event": "viewer-check", "ok": viewer_ok})
    graph_lane, brain_lane = lanes["graph"], lanes["brain"]
    knowledge_receipt = _knowledge_receipt(kdir)
    rows.append(("21-graph", graph_lane["status"] == "EXERCISED",
                 f"graph backend {graph_lane.get('version')} run with isolation {graph_lane.get('isolation')}"
                 if graph_lane["status"] == "EXERCISED" else graph_reason or graph_lane["reason"], knowledge_receipt))
    rows.append(("22-brain", brain_lane["status"] == "EXERCISED",
                 "brain backend handshake exercised" if brain_lane["status"] == "EXERCISED"
                 else "no backend speaking the brain handshake protocol is part of this collection", knowledge_receipt))
    rows.append(("64-static-viewer", viewer_ok,
                 "state export rendered by the in-tree viewer; page escapes markup and shows the project id" if viewer_ok
                 else "the export-then-render check failed", file_receipt(outfd, "discovery.jsonl")))
    rows.append(("64-external-viewer", False, entry["reason"], None))
    rows.append(("70-discovery",) + discovery_row(args, outfd))
    table = []
    for rid, ok, reason, receipt in rows:
        status = "EXERCISED" if ok else "MISSING"
        claim = claims.get(CAPABILITY[rid], "unknown")
        conflict = None
        if status == "EXERCISED" and claim in ("planned", "unknown", "none"):
            conflict = f"registry claims {claim}, observed EXERCISED"
        elif status == "MISSING" and claim not in ("planned", "unknown", "none"):
            conflict = f"registry claims {claim}, observed MISSING"
        table.append({"id": rid, "status": status, "owner": OWNERS.get(rid, ""), "reason": reason, "receipt": receipt,
                      "registry_claim": claim, "conflict": conflict, **({"setup": SETUP[rid]} if status == "MISSING" and rid in SETUP else {})})
    return table


def _knowledge_receipt(kdir):
    path = os.path.join(kdir, "knowledge.jsonl")
    if not os.path.isfile(path):
        return None
    return {"path": "knowledge/knowledge.jsonl", "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def static_viewer_cell(tmp):
    """Bootstrap a disposable project, export its state, render it, and check the page escapes and names the project."""
    base = Path(tempfile.mkdtemp(dir=tmp))
    project = base / "demo<i>&project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Demo\n")
    registry, state, page = base / "registry.json", base / "state.json", base / "page.html"
    for argv in ([STEWARD, "bootstrap", "--project", project, "--registry", registry, "--topic", "main"],
                 [STATE_EXPORT, "export", "--registry", registry, "--out", state], [VIEWER, "render", "--state", state, "--out", page]):
        if child(argv, str(base))[0] != 0:
            return False
    text = page.read_text(encoding="utf-8")
    return html.escape(project.name) in text and "<i>" not in text


def discovery_row(args, outfd):
    if not args.companion:
        return False, "no companion manifest given", None
    manifest = companion.load_manifest(args.companion)
    _, identity = companion.fetch(manifest)
    records = receipts(outfd, "roundtrip")
    real = [r for r in records if r.get("backend") == "companion"]
    good = [r for r in real if r.get("companion_ack") == "confirmed" and r.get("isolation") == "net-denied" and r.get("identity")]
    if good and good[-1]["identity"] == identity:
        return True, f"real-companion roundtrip in {good[-1]['mode']} mode: ack confirmed, isolation net-denied, identity matches", \
            file_receipt(outfd, "discovery.jsonl")
    if good:
        return False, "the receipt identity does not match the current companion files", None
    if records:
        return False, "only fixture receipts exist; a fixture proves the adapter, not the lane", None
    return False, "no roundtrip receipt for this companion yet", None


def cmd_companions(args, outfd, out_path):
    table = build_rows(args, outfd, out_path)
    if any(not row["owner"] for row in table):
        raise Fail("every row needs an owner")
    if args.json:
        print(json.dumps({"rows": table}, indent=2, sort_keys=True))
    else:
        for row in table:
            print(f"{row['id']}: {row['status']} ({row['owner']}) - {row['reason']}")
            if row["conflict"]:
                print(f"  conflict: {row['conflict']}")
            if "setup" in row:
                print(f"  setup: {row['setup']}")
    missing = next(r for r in table if r["id"] == "70-discovery")["status"] == "MISSING"
    return 3 if missing and os.environ.get("AIARCH_REQUIRE_COMPANION") == "1" else 0


# ---- CLI --------------------------------------------------------------------------------------------------------------

def parser():
    top = argparse.ArgumentParser(prog="discovery.py", description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    for flag in ("--out", "--companion", "--sandbox", "--registry", "--project", "--targets-root", "--topic", "--id", "--graphify",
                 "--viewer-companion"):
        common.add_argument(flag)
    common.set_defaults(id="example/widget", what="A disposable example candidate for the handoff roundtrip.",
                        next_action="Read the brief and record a disposition.")
    common.add_argument("--what", default=argparse.SUPPRESS)
    common.add_argument("--next-action", dest="next_action", default=argparse.SUPPRESS)
    common.add_argument("--mode", choices=("pack", "inbox", "pseudo"), default="pack")
    common.add_argument("--disposition", default="deferred")
    common.add_argument("--backend", choices=("fixture", "companion"), default="companion")
    common.add_argument("--json", action="store_true")
    sub = top.add_subparsers(dest="command", required=True)
    for name in ("sandbox", "candidate", "handoff", "decide", "roundtrip", "companions"):
        sub.add_parser(name, parents=[common])
    sub.add_parser("inbox", parents=[common]).add_argument("action", choices=("list", "intake"))
    return top


def run(args, ctx):
    cmd = args.command
    if cmd == "sandbox":
        ctx.prepare()
        return {"sandbox": "ready", "identity": ctx.identity}, 0
    if cmd == "candidate":
        project, _ = check_project(args)
        ctx.prepare()
        write_candidate(args, ctx.manifest, ctx.sandbox, project)
        return {"candidate": args.id, "mode": args.mode, "spawns": 0}, 0
    out_path = resolve_out(args.out)
    outfd = knowledge.open_out(out_path)
    try:
        if cmd == "companions":
            return None, cmd_companions(args, outfd, out_path)
        if cmd == "roundtrip":
            return cmd_roundtrip(args, ctx, outfd), 0
        project, targets = check_project(args)
        ctx.prepare()
        if cmd == "handoff":
            result = do_handoff(ctx, project, targets)
            append(outfd, {"event": "handoff", "staged": result["staged"], "placements": [p["result"] for p in result["placements"]]})
            return {**result, "spawns": ctx.spawns}, 0
        if cmd == "decide":
            return do_decide(ctx, outfd, project, args.id, args.disposition, args.backend), 0
        entries, refused = scan_inbox(project)
        intake(outfd, entries)
        if args.action == "list":
            status = list_rows(ctx.runner)
            for entry in entries:
                entry["companion_status"] = status.get(entry["id"])
                entry["disposition"] = reverse(ctx.manifest, entry["companion_status"])
        return {"entries": entries, "refused": refused, "spawns": ctx.spawns}, 0
    finally:
        os.close(outfd)


def main(argv=None):
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        result, rc = run(args, Ctx(args))
    except Refuse as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except knowledge.Refuse as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except Fail as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"failed: {error.strerror or type(error).__name__}", file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
