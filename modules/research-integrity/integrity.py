#!/usr/bin/env python3
"""Research provenance, lifecycle and empirical integrity: init -> query -> check -> finalize -> release.

Every step writes a hash-chained receipt to <project>/.research/receipts/. Quote, claim and scaffold work
is delegated to the public provenote companion (bin/verify-quotes, bin/check-claims, bin/new-project,
bin/new-pipeline) as subprocesses; without it those checks are UNAVAILABLE, never PASS. This module owns
source/exhibit lineage, the statistical-reporting check (stats.py), the analysis runner and the
audit/referee/replication contracts. No network, no model call. Referee and audit results are recorded
attestations that are validated here, not reviews performed here.
Exit codes: 0 PASS, 1 FAIL / stale / receipt chain broken, 2 usage or refused input, 3 INCOMPLETE.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = ".research"
ZERO = "0" * 64
CHECKS = ("scaffold", "query", "sources", "quotes", "claims", "stats", "exhibits", "analysis",
          "audit", "referee", "replication")
LAYOUT = ("00_admin", "01_notes/lit", "02_sources", "03_code", "10_paper")
NAME_RE = re.compile(r"(\d{3})-(init|query|check|finalize|release)\.json")
HEX = re.compile(r"[0-9a-f]{64}")
EXHIBIT_REF = re.compile(r"\{\{exhibit:([A-Za-z0-9_-]+)\}\}")
CITE = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_:-]*)")
NO_COMPANION = "no companion given (--companion DIR)"
BIN_TIMEOUT = 120.0


class Exit(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code, self.message = code, message


def load_sibling(name, path):
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = spec_from_file_location(name, path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


stats = load_sibling("research_integrity_stats", HERE / "stats.py")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha_bytes(Path(path).read_bytes())


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)


def rel_ok(rel, what="path"):
    """A project-relative POSIX path: no absolute form, no `..`, no empty or `.` component."""
    if (not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel or rel.startswith("/")
            or re.match(r"[A-Za-z]:", rel) or any(part in ("", ".", "..") for part in rel.split("/"))):
        raise Exit(2, f"invalid {what}: {str(rel)[:60]!r}")
    return rel


def proj_path(project, rel, what="path"):
    """Project path for a relative name, refusing symlinks on the way."""
    rel_ok(rel, what)
    here = project
    for part in rel.split("/"):
        here = here / part
        if here.is_symlink():
            raise Exit(2, f"symlink refused: {rel}")
    return here


def walk_files(project):
    """Sorted project-relative paths of every regular file outside .research; symlinks refuse (exit 2)."""
    root = str(project)
    found = []
    for here, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(here, root)
        for name in list(dirs):
            if os.path.islink(os.path.join(here, name)):
                raise Exit(2, f"symlink refused: {os.path.normpath(os.path.join(rel_dir, name))}")
            if name == "__pycache__" or (rel_dir == "." and name == RES):
                dirs.remove(name)
        for name in files:
            full = os.path.join(here, name)
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.isfile(full):
                raise Exit(2, f"symlink or special file refused: {rel}")
            found.append(rel)
    return sorted(found)


def bind(project):
    return {rel: sha_file(project / rel) for rel in walk_files(project)}


def stale_paths(project, bound):
    now = bind(project)
    return sorted(rel for rel in set(now) | set(bound) if now.get(rel) != bound.get(rel))


# ---------------------------------------------------------------- receipts

def receipts_dir(project):
    return project / RES / "receipts"


def seal(record):
    return sha_bytes(canon({k: v for k, v in record.items() if k != "self_sha256"}).encode("utf-8"))


def load_chain(project):
    """Return (records, problems). Checks names, sequence, project id, prev hashes and self hashes."""
    folder = receipts_dir(project)
    if folder.is_symlink() or (project / RES).is_symlink():
        raise Exit(2, "symlink refused: .research")
    if not folder.is_dir():
        return [], ["no receipts (run init)"]
    problems, records, previous, project_id = [], [], ZERO, None
    names = sorted(entry.name for entry in folder.iterdir())
    for name in names:
        if not NAME_RE.fullmatch(name):
            problems.append(f"unexpected file in receipts: {name}")
    entries = [(NAME_RE.fullmatch(n), n) for n in names]
    entries = [(m, n) for m, n in entries if m]
    for position, (match, name) in enumerate(entries, 1):
        if int(match.group(1)) != position:
            problems.append(f"missing receipt: {position:03d}")
            return records, problems
        path = folder / name
        if path.is_symlink():
            raise Exit(2, f"symlink refused: {RES}/receipts/{name}")
        try:
            record = load_json(path)
            ok = isinstance(record, dict)
        except (OSError, ValueError):
            ok = False
        if not ok:
            problems.append(f"unreadable receipt: {name}")
            return records, problems
        if (record.get("version") != 1 or record.get("seq") != position
                or record.get("step") != match.group(2)):
            problems.append(f"receipt fields mismatch: {name}")
        if record.get("self_sha256") != seal(record):
            problems.append(f"receipt edited: {name}")
        if position == 1:
            project_id = record.get("project_id")
            if record.get("step") != "init":
                problems.append("first receipt is not init")
        elif record.get("step") == "init":
            problems.append(f"second init receipt: {name}")
        if record.get("project_id") != project_id or not isinstance(project_id, str):
            problems.append(f"receipt from another project: {name}")
        if record.get("prev_sha256") != previous:
            problems.append(f"broken chain at: {name}")
        previous = sha_file(path)
        records.append(record)
    return records, problems


def chain_or_exit(project):
    records, problems = load_chain(project)
    if problems:
        for problem in problems:
            print(f"receipt: {problem}", file=sys.stderr)
        raise Exit(1, "receipt chain invalid")
    return records


def append_receipt(project, step, status, payload, project_id=None):
    records, problems = load_chain(project) if step != "init" else ([], [])
    if problems:
        raise Exit(1, "receipt chain invalid")
    folder = receipts_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    seq = len(records) + 1
    previous = ZERO if not records else sha_file(folder / f"{seq - 1:03d}-{records[-1]['step']}.json")
    record = {"version": 1, "seq": seq, "step": step, "project_id": project_id or records[0]["project_id"],
              "prev_sha256": previous, "status": status,
              "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "payload": payload}
    record["self_sha256"] = seal(record)
    target = folder / f"{seq:03d}-{step}.json"
    temp = folder / f".tmp-{uuid.uuid4().hex}"
    temp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return record


# ---------------------------------------------------------------- subprocesses

def run_child(argv, cwd, env, timeout):
    """Run argv in its own session; kill the whole group on timeout. Returns (exit code, timed_out)."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return None, False
    timed_out = False
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    if timed_out:
        proc.communicate()
    return proc.returncode, timed_out


def clean_env(**extra):
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUTF8": "1", "TZ": "UTC"}
    env.update(extra)
    return env


class Companion:
    """Handshake result and bin lookup for the provenote companion (identity via research-adapter)."""

    def __init__(self, directory, bin_timeout):
        self.dir = os.path.abspath(directory) if directory else None
        self.timeout = bin_timeout
        self.info = None
        self.reason = None
        if directory is None:
            self.reason = NO_COMPANION
            return
        path = HERE.parent / "research-adapter" / "research_adapter.py"
        if not path.is_file():
            self.reason = "research-adapter module not found next to research-integrity"
            return
        adapter = load_sibling("research_adapter_handshake", path)
        try:
            state = adapter.handshake(self.dir)
        except adapter.Exit as stop:
            raise Exit(2, stop.message)
        self.info = state["companion"]
        text = state["capabilities"]["34"]
        if self.info is None or text.startswith("unsupported"):
            self.reason = re.sub(r"^unavailable: ", "", text)

    def bin(self, name):
        """Path of a regular, non-symlink companion bin, or None."""
        folder = Path(self.dir) / "bin"
        path = folder / name
        if folder.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        return path

    def run(self, name, args, cwd):
        """Run one bin. Returns ('PASS'|'FAIL', reason)."""
        argv = [sys.executable, "-B", str(self.bin(name)), *args]
        code, timed_out = run_child(argv, cwd, clean_env(), self.timeout)
        if timed_out:
            return "FAIL", "timeout"
        if code == 0:
            return "PASS", ""
        if code is None or code < 0:
            return "FAIL", "crash"
        return "FAIL", "usage" if code == 2 else ("failed" if code == 1 else "crash")


def unavailable(companion, name):
    if companion.reason:
        return companion.reason
    if companion.bin(name) is None:
        return f"requires bin/{name} (a regular file) in the companion"
    return None


# ---------------------------------------------------------------- contracts

class Soft(Exception):
    """A contract problem that makes one check FAIL (never an exit-2 refusal)."""


def contract(project, rel):
    path = proj_path(project, rel)
    if not path.is_file():
        raise Soft(f"missing: {rel}")
    try:
        return load_json(path)
    except ValueError as error:
        if "duplicate JSON key" in str(error):
            raise Exit(2, f"{rel}: {error}")
        raise Soft(f"invalid: {rel}")
    except OSError:
        raise Soft(f"invalid: {rel}")


def run_config(project, rel):
    data = contract(project, rel)
    keys = {"entry", "timeout_s", "outputs"}
    if not isinstance(data, dict) or not keys <= set(data) <= keys | {"cwd"}:
        raise Soft(f"invalid: {rel}")
    return validate_run(project, rel, data)


def validate_run(project, rel, data):
    entry, timeout, outputs = data["entry"], data["timeout_s"], data["outputs"]
    if (not isinstance(entry, list) or len(entry) < 2 or not all(isinstance(a, str) and a for a in entry)
            or entry[0] not in ("python3", "Rscript")):
        raise Soft(f"invalid entry in {rel}")
    for arg in entry[1:]:
        if not arg.startswith("-"):
            rel_ok(arg, f"{rel} entry")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise Soft(f"invalid timeout_s in {rel}")
    if not isinstance(outputs, list) or not outputs or not all(isinstance(o, str) for o in outputs):
        raise Soft(f"invalid outputs in {rel}")
    for out in outputs:
        proj_path(project, out, f"{rel} output")
    cwd = rel_ok(data.get("cwd", "."), f"{rel} cwd") if data.get("cwd", ".") != "." else "."
    if cwd != "." and not proj_path(project, cwd).is_dir():
        raise Soft(f"invalid cwd in {rel}")
    return {"entry": entry, "timeout_s": timeout, "outputs": outputs, "cwd": cwd}


SEED_VARS = ("PYTHONHASHSEED", "SEED", "RANDOM_SEED", "SOURCE_DATE_EPOCH")


def replication_config(project):
    """replication.json may only add runs/env; entry/outputs/cwd, if given, must equal analysis.json."""
    base = run_config(project, "analysis.json")
    rel = "replication.json"
    data = contract(project, rel)
    shared = ("entry", "timeout_s", "outputs", "cwd")
    if not isinstance(data, dict) or not set(data) <= set(shared) | {"runs", "env"}:
        raise Soft(f"invalid: {rel}")
    cfg = validate_run(project, rel, {**base, **{k: data[k] for k in shared if k in data}})
    for key in ("entry", "outputs", "cwd"):
        if key in data and cfg[key] != base[key]:
            raise Soft(f"{rel}: {key} differs from analysis.json")
    runs, env = data.get("runs", 1), data.get("env", {})
    if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= 10:
        raise Soft(f"invalid runs in {rel}")
    if (not isinstance(env, dict) or not set(env) <= set(SEED_VARS)
            or not all(isinstance(v, str) for v in env.values())):
        raise Soft(f"invalid env in {rel} (allowed: {', '.join(SEED_VARS)})")
    return {**cfg, "runs": runs, "env": env}


def code_files(project):
    return {rel: sha_file(project / rel) for rel in walk_files(project) if rel.startswith("03_code/")}


def exhibit_files(project):
    """Exhibit files named by exhibits.json (best effort; the exhibits check reports a bad contract)."""
    try:
        data = contract(project, "exhibits.json")
        items = data["exhibits"]
        return [proj_path(project, i["file"], "exhibit file") and i["file"] for i in items]
    except (Soft, Exit, KeyError, TypeError):
        return []


def run_entry(project, cfg, env=None):
    """Run a declared entry in a fresh temp copy from which every declared output and exhibit file was removed.

    Returns (status, detail, run dict or None). PASS means every declared output was produced by the run and
    equals the committed file.
    """
    argv0 = cfg["entry"][0]
    if argv0 == "python3":
        exe = sys.executable
    else:
        exe = shutil.which(argv0, path=os.environ.get("PATH", ""))
        if exe is None:
            return "UNAVAILABLE", f"{argv0} not found on PATH", None
    temp = Path(tempfile.mkdtemp(prefix="ri-"))
    try:
        work = temp / "p"
        shutil.copytree(project, work, symlinks=True, ignore=shutil.ignore_patterns(RES, "__pycache__"))
        for rel in [*cfg["outputs"], *exhibit_files(project)]:
            gone = work / rel
            if gone.is_symlink() or gone.is_file():
                gone.unlink()
        started = time.monotonic()
        code, timed_out = run_child([exe, *cfg["entry"][1:]], work / cfg["cwd"],
                                    clean_env(HOME=str(work), **(env or {})), cfg["timeout_s"])
        wall = round(time.monotonic() - started, 3)
        run = {"entry": cfg["entry"], "cwd": cfg["cwd"], "exit": code, "wall_s": wall, "outputs": {}}
        if timed_out:
            return "FAIL", f"timeout after {cfg['timeout_s']} s", run
        if code != 0:
            return "FAIL", f"exit {code}", run
        fails = []
        for out in cfg["outputs"]:
            target, committed = work / out, project / out
            if not target.is_file() or target.is_symlink():
                fails.append(f"missing output {out}")
                continue
            run["outputs"][out] = sha_file(target)
            if not committed.is_file() or committed.is_symlink():
                fails.append(f"missing committed output {out}")
            elif sha_file(committed) != run["outputs"][out]:
                fails.append(f"output differs {out}")
        if fails:
            return "FAIL", "; ".join(fails), run
        return "PASS", "", run
    finally:
        shutil.rmtree(temp, ignore_errors=True)


# ---------------------------------------------------------------- checks

def verdict(fails, unavail, ok="ok"):
    if fails:
        return "FAIL", "; ".join(fails)
    if unavail:
        return "UNAVAILABLE", "; ".join(unavail)
    return "PASS", ok


def check_scaffold(project, chain):
    fails = []
    init = chain[0]["payload"]["scaffold"]
    if init["status"] != "PASS":
        fails.append(f"init: {init['detail']}")
    for rel in LAYOUT:
        if not (project / rel).is_dir():
            fails.append(f"missing: {rel}")
    return verdict(fails, [], f"layout ok ({init['mode']})")


def check_query(chain):
    found = [r for r in chain if r["step"] == "query"]
    if not found:
        return "UNAVAILABLE", "no query receipt (run query --index INDEX)"
    last = found[-1]
    return last["status"], "" if last["status"] == "PASS" else last["payload"].get("detail", "")


def check_sources(project):
    bound = walk_files(project)
    sources = [r for r in bound if r.startswith("02_sources/md/") and r.endswith(".md")]
    if not sources:
        return "FAIL", "empty: sources (02_sources/md)"
    manifest = "02_sources/manifest.csv"
    if manifest not in bound:
        return "FAIL", f"missing: {manifest}"
    try:
        with open(project / manifest, newline="", encoding="utf-8-sig") as handle:
            stems = {row.get("stem") for row in csv.DictReader(handle)}
    except (OSError, UnicodeDecodeError, csv.Error):
        return "FAIL", f"invalid: {manifest}"
    fails = [f"not in manifest: {r}" for r in sources if Path(r).stem not in stems]
    return verdict(fails, [], f"{len(sources)} sources bound")


def note_files(project):
    return [r for r in walk_files(project) if r.startswith("01_notes/lit/") and r.endswith(".md")]


def check_quotes(project, companion):
    notes = note_files(project)
    if not notes:
        return "FAIL", "empty: notes (01_notes/lit)"
    reason = unavailable(companion, "verify-quotes")
    if reason:
        return "UNAVAILABLE", reason
    fails = []
    for rel in notes:
        state, why = companion.run("verify-quotes", ["--strict", str(project / rel)], str(project))
        if state != "PASS":
            fails.append(f"{rel}: {why}")
    return verdict(fails, [], f"{len(notes)} notes verified")


def draft_text(project):
    path = proj_path(project, "10_paper/draft.md")
    if not path.is_file():
        raise Soft("missing: 10_paper/draft.md")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise Soft("invalid: 10_paper/draft.md")


def check_claims(project, companion):
    try:
        text = draft_text(project)
    except Soft as problem:
        return "FAIL", str(problem)
    if not CITE.search(text):
        return "FAIL", "empty: draft citations"
    reason = unavailable(companion, "check-claims")
    if reason:
        return "UNAVAILABLE", reason
    state, why = companion.run("check-claims", ["--strict-quotes", "--root", str(project),
                                                str(project / "10_paper" / "draft.md")], str(project))
    return ("PASS", "claims verified") if state == "PASS" else ("FAIL", f"10_paper/draft.md: {why}")


def check_stats(project):
    try:
        items = stats.check_text(draft_text(project))
    except Soft as problem:
        return "FAIL", str(problem)
    fails = [f"{i['text']} ({i['detail']})" for i in items if i["status"] == "FAIL"]
    odd = [f"unsupported-grammar: {i['text']}" for i in items if i["status"] == "unsupported-grammar"]
    return verdict(fails, odd, f"{sum(1 for i in items if i['status'] == 'PASS')} statistics consistent")


def check_analysis(project):
    try:
        cfg = run_config(project, "analysis.json")
    except Soft as problem:
        return "FAIL", str(problem), None
    status, detail, run = run_entry(project, cfg)
    if status == "PASS":
        run["scripts"] = code_files(project)
        return "PASS", "analysis ran", run
    return status, detail, (run if status == "FAIL" else None)


def check_replication(project):
    try:
        cfg = replication_config(project)
    except Soft as problem:
        return "FAIL", str(problem)
    for _ in range(cfg["runs"]):
        status, detail, _run = run_entry(project, cfg, cfg["env"])
        if status != "PASS":
            return status, detail
    return "PASS", f"rerun matches the committed outputs ({cfg['runs']}x)"


def check_exhibits(project, run):
    try:
        text = draft_text(project)
        data = contract(project, "exhibits.json")
    except Soft as problem:
        return "FAIL", str(problem)
    exhibits = data.get("exhibits") if isinstance(data, dict) and set(data) == {"exhibits"} else None
    if not isinstance(exhibits, list):
        return "FAIL", "invalid: exhibits.json"
    by_id = {}
    for item in exhibits:
        if (not isinstance(item, dict) or set(item) != {"id", "file", "sha256", "produced_by"}
                or not all(isinstance(v, str) for v in item.values())
                or not re.fullmatch(r"[A-Za-z0-9_-]+", item["id"]) or item["id"] in by_id):
            return "FAIL", "invalid: exhibits.json"
        by_id[item["id"]] = item
    scripts = code_files(project)
    fails, unavail = [], []
    for ref in EXHIBIT_REF.findall(text):
        if ref not in by_id:
            fails.append(f"unknown exhibit: {ref}")
    for ident, item in by_id.items():
        path = proj_path(project, item["file"], "exhibit file")
        proj_path(project, item["produced_by"], "produced_by")
        if not path.is_file():
            fails.append(f"missing file: {ident} ({item['file']})")
            continue
        actual = sha_file(path)
        if actual != item["sha256"]:
            fails.append(f"sha mismatch: {ident} ({item['file']})")
        if item["produced_by"] not in scripts:
            fails.append(f"unproduced: {ident} (script {item['produced_by']} is not under 03_code)")
        elif run is None:
            unavail.append(f"analysis run unavailable for {ident}")
        elif item["file"] not in run["outputs"]:
            fails.append(f"unproduced: {ident} ({item['file']} is not an analysis output)")
        elif run["outputs"][item["file"]] != actual:
            fails.append(f"run mismatch: {ident} (analysis output differs from the exhibit file)")
    return verdict(fails, unavail, f"{len(by_id)} exhibits linked")


def reviewed_problems(project, review):
    reviewed = review.get("reviewed")
    fails = []
    if (not isinstance(reviewed, dict) or not {"10_paper/draft.md", "exhibits.json"} <= set(reviewed)
            or not all(isinstance(v, str) and HEX.fullmatch(v) for v in reviewed.values())):
        return ["invalid reviewed (needs 10_paper/draft.md and exhibits.json with sha256)"]
    for rel, digest in sorted(reviewed.items()):
        path = proj_path(project, rel, "reviewed path")
        if not path.is_file() or sha_file(path) != digest:
            fails.append(f"stale-review: {rel}")
    return fails


def check_audit(project):
    try:
        audit = contract(project, "00_admin/audit.json")
    except Soft as problem:
        return "FAIL", str(problem)
    if not isinstance(audit, dict) or set(audit) != {"status", "reviewed", "findings"}:
        return "FAIL", "invalid: 00_admin/audit.json"
    if audit["status"] == "NOT RUN":
        return "UNAVAILABLE", "audit NOT RUN"
    if audit["status"] != "RUN" or not isinstance(audit["findings"], list):
        return "FAIL", "invalid audit status"
    fails = reviewed_problems(project, audit)
    for number, finding in enumerate(audit["findings"], 1):
        name = finding.get("id") if isinstance(finding, dict) else None
        label = f"finding {name if isinstance(name, str) else number}"
        if (not isinstance(finding, dict) or set(finding) != {"id", "disposition", "evidence", "evidence_sha256"}
                or finding["disposition"] not in ("fixed", "refuted", "accepted-risk")):
            fails.append(f"{label}: needs a fixed|refuted|accepted-risk disposition and evidence")
            continue
        path = proj_path(project, finding["evidence"], "evidence")
        if not path.is_file() or sha_file(path) != finding["evidence_sha256"]:
            fails.append(f"{label}: evidence missing or its sha256 differs")
    return verdict(fails, [], f"{len(audit['findings'])} findings dispositioned")


def check_referee(project):
    try:
        referee = contract(project, "00_admin/referee.json")
    except Soft as problem:
        return "FAIL", str(problem)
    if not isinstance(referee, dict) or set(referee) != {"status", "reviewed", "summary"}:
        return "FAIL", "invalid: 00_admin/referee.json"
    if referee["status"] == "NOT RUN":
        return "UNAVAILABLE", "referee NOT RUN"
    if referee["status"] != "RUN" or not isinstance(referee["summary"], str) or not referee["summary"].strip():
        return "FAIL", "invalid referee status or summary"
    return verdict(reviewed_problems(project, referee), [], "referee attestation bound")


def run_checks(project, companion, chain):
    results, details = {}, {}

    def put(name, outcome):
        status, detail = outcome
        results[name] = {"status": status, "detail": detail}

    put("scaffold", check_scaffold(project, chain))
    put("query", check_query(chain))
    put("sources", check_sources(project))
    put("quotes", check_quotes(project, companion))
    put("claims", check_claims(project, companion))
    put("stats", check_stats(project))
    status, detail, run = check_analysis(project)
    put("analysis", (status, detail))
    put("exhibits", check_exhibits(project, run))
    put("audit", check_audit(project))
    put("referee", check_referee(project))
    put("replication", check_replication(project))
    return {name: results[name] for name in CHECKS}, run


def overall(results):
    states = {r["status"] for r in results.values()}
    return "FAIL" if "FAIL" in states else ("INCOMPLETE" if states - {"PASS"} else "PASS")


# ---------------------------------------------------------------- commands

CODES = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3, "UNAVAILABLE": 3}


def open_project(args):
    project = Path(os.path.abspath(args.project))
    if project.is_symlink():
        raise Exit(2, "project directory is a symlink")
    return project


def cmd_init(args):
    project = open_project(args)
    if receipts_dir(project).exists():
        raise Exit(2, "already initialised")
    populated = project.exists() and (not project.is_dir() or any(project.iterdir()))
    companion = Companion(args.companion, args.bin_timeout)
    scaffold = {"status": "PASS", "detail": "ok", "mode": "adopted", "pipeline": args.pipeline}
    steps = []
    if not populated:
        steps.append(("new-project", [str(project)], "new-project"))
    if args.pipeline:
        steps.append(("new-pipeline", [str(project / "03_code"), "--lang", args.pipeline], "new-pipeline"))
    for name, _, _ in steps:
        reason = unavailable(companion, name)
        if reason:
            print(f"scaffold: UNAVAILABLE: {reason}")
            return 3
    for name, argv, _ in steps:
        target = Path(argv[0])
        target.parent.mkdir(parents=True, exist_ok=True)
        state, why = companion.run(name, argv, str(target.parent))
        scaffold["mode"] = "new-project" if name == "new-project" else scaffold["mode"]
        if state != "PASS":
            scaffold.update(status="FAIL", detail=f"{name}: {why}")
            break
    project.mkdir(parents=True, exist_ok=True)
    record = append_receipt(project, "init", scaffold["status"], {"scaffold": scaffold},
                            project_id=uuid.uuid4().hex)
    print(f"init: {scaffold['status']}  scaffold: {scaffold['detail']}  project_id: {record['project_id']}")
    return CODES[scaffold["status"]]


def cmd_query(args):
    project = open_project(args)
    chain_or_exit(project)
    index = Path(args.index)
    if not index.is_file() or index.is_symlink():
        raise Exit(2, "index.json is not a regular file")
    try:
        data = load_json(index)
    except (OSError, ValueError) as error:
        raise Exit(2, f"invalid index.json: {error}")
    if (not isinstance(data, dict) or not isinstance(data.get("status"), str)
            or not isinstance(data.get("companion_receipt"), dict)):
        raise Exit(2, "invalid index.json: need status and companion_receipt")
    receipt = data["companion_receipt"]
    rel_ok(receipt.get("path"), "companion_receipt path")
    target = index.parent.parent / receipt["path"]
    payload = {"index_sha256": sha_file(index), "index_status": data["status"],
               "companion": data.get("companion"), "notes": len(data.get("notes", []))}
    if not target.is_file() or target.is_symlink() or sha_file(target) != receipt.get("sha256"):
        status, payload["detail"] = "FAIL", "companion receipt missing or its sha256 differs from the index"
    else:
        payload["companion_receipt_sha256"] = receipt["sha256"]
        if data["status"] == "ok":
            status, payload["detail"] = "PASS", "index bound"
        else:
            status, payload["detail"] = "UNAVAILABLE", f"index status is {data['status']} (need ok)"
    append_receipt(project, "query", status, payload)
    print(f"query: {status}  {payload['detail']}")
    return CODES[status]


def print_checks(results, state):
    for name, result in results.items():
        suffix = f": {result['detail']}" if result["detail"] and result["status"] != "PASS" else ""
        print(f"{name}: {result['status']}{suffix}")
    print(f"overall: {state}")


def cmd_check(args):
    project = open_project(args)
    chain = chain_or_exit(project)
    companion = Companion(args.companion, args.bin_timeout)
    bound = bind(project)
    results, run = run_checks(project, companion, chain)
    state = overall(results)
    append_receipt(project, "check", state, {
        "checks": results, "bound": bound, "analysis_run": run,
        "companion": companion.info if not companion.reason else None})
    print_checks(results, state)
    return CODES[state]


def last_check(project, step):
    """Validate the chain and the gate shared by finalize and release; returns (chain, check record)."""
    chain = chain_or_exit(project)
    last = chain[-1]
    if last["step"] == "check":
        if last["status"] != "PASS":
            raise Exit(CODES[last["status"]], f"refused: last check is {last['status']}")
        if step == "release":
            raise Exit(1, "refused: run finalize first")
        return chain, last
    if step == "release" and last["step"] == "finalize":
        return chain, chain[last["payload"]["check_seq"] - 1]
    raise Exit(1, f"refused: last receipt is {last['step']}; {'run check' if step == 'finalize' else 'run finalize'}")


def raise_stale(project, bound):
    stale = stale_paths(project, bound)
    for rel in stale:
        print(f"stale: {rel}")
    if stale:
        raise Exit(1, "bound files changed since the check")


def cmd_finalize(args):
    project = open_project(args)
    chain, check = last_check(project, "finalize")
    raise_stale(project, check["payload"]["bound"])
    append_receipt(project, "finalize", "PASS", {"check_seq": check["seq"], "bound": check["payload"]["bound"]})
    print(f"finalize: PASS  check receipt {check['seq']:03d}")
    return 0


def paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def note_quotes(text):
    """Quotes of a note as (page, text) pairs, read from the front matter lines."""
    quotes, page = [], None
    for line in text.split("\n"):
        match = re.match(r"\s*-\s+page:\s*(\S+)", line)
        if match:
            page = match.group(1).strip("\"'")
            continue
        match = re.match(r"\s+text:\s*(.*)$", line)
        if match and page is not None:
            raw = match.group(1).strip()
            try:
                value = json.loads(raw) if raw.startswith('"') else raw.strip("'")
            except ValueError:
                value = raw
            quotes.append((page, value))
            page = None
    return quotes


def build_manifest(project, chain, check, state):
    notes = note_files(project)
    draft = paragraphs(draft_text(project))
    sources = []
    for rel in sorted(r for r in check["payload"]["bound"] if r.startswith("02_sources/md/")):
        linked = []
        for note in notes:
            text = (project / note).read_text(encoding="utf-8")
            ref = re.search(r"^source_md:\s*(\S+)", text, re.M)
            if not ref or ref.group(1).strip("\"'") != rel:
                continue
            key = Path(note).stem
            linked.append({
                "note": note,
                "quotes": [{"page": p, "text_sha256": sha_bytes(t.encode("utf-8"))}
                           for p, t in note_quotes(text)],
                "draft_paragraphs": [{"paragraph": n, "sha256": sha_bytes(p.encode("utf-8"))}
                                     for n, p in enumerate(draft, 1) if key in CITE.findall(p)]})
        sources.append({"file": rel, "sha256": check["payload"]["bound"][rel], "notes": linked})
    run = check["payload"]["analysis_run"]
    exhibits = []
    for item in contract(project, "exhibits.json")["exhibits"]:
        exhibits.append({
            "id": item["id"], "file": item["file"], "sha256": item["sha256"],
            "draft_paragraphs": [n for n, p in enumerate(draft, 1) if f"{{{{exhibit:{item['id']}}}}}" in p],
            "script": item["produced_by"], "script_sha256": run["scripts"][item["produced_by"]],
            "analysis_run": {"receipt": check["seq"], "exit": run["exit"],
                             "output_sha256": run["outputs"][item["file"]]}})
    folder = receipts_dir(project)
    return {"version": 1, "project_id": chain[0]["project_id"], "status": state,
            "companion": check["payload"]["companion"],
            "receipts": [{"seq": r["seq"], "step": r["step"],
                          "sha256": sha_file(folder / f"{r['seq']:03d}-{r['step']}.json")} for r in chain],
            "sources": sources, "exhibits": exhibits}


def out_dir(project, raw):
    out = Path(os.path.abspath(raw))
    if Path(raw).is_symlink() or out.is_symlink():
        raise Exit(2, "--out is a symlink")
    resolved = out.resolve()
    if resolved == project.resolve() or project.resolve() in resolved.parents or resolved in project.resolve().parents:
        raise Exit(2, "--out must lie outside the project")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Exit(2, "--out must be absent or an empty directory")
    return out


def cmd_release(args):
    project = open_project(args)
    out = out_dir(project, args.out)
    chain, check = last_check(project, "release")
    finalize = chain[-1]
    raise_stale(project, finalize["payload"]["bound"])
    manifest = build_manifest(project, chain, check, "PASS")
    out.mkdir(parents=True, exist_ok=True)
    temp = out / f".tmp-{uuid.uuid4().hex}"
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, out / "release-manifest.json")
    append_receipt(project, "release", "PASS", {"manifest_sha256": sha_bytes(text.encode("utf-8")),
                                                "bound": finalize["payload"]["bound"]})
    print("release: PASS  release-manifest.json written")
    return 0


def cmd_verify_receipt(args):
    project = open_project(args)
    records, problems = load_chain(project)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    bound = next((r["payload"]["bound"] for r in reversed(records) if "bound" in r["payload"]), None)
    stale = stale_paths(project, bound) if bound is not None else []
    for rel in stale:
        print(f"stale: {rel}")
    if stale:
        return 1
    print(f"receipts: {len(records)} verified")
    return 0


def cmd_replicate(args):
    project = open_project(args)
    chain_or_exit(project)
    before = bind(project)
    status, detail = check_replication(project)
    print(f"replicate: {status}" + (f": {detail}" if status != "PASS" else ""))
    return CODES[status] if bind(project) == before else 1


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Exit(2, message)


def parser():
    top = Parser(description=__doc__.split("\n\n")[0])
    sub = top.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("init", "query", "check", "finalize", "release", "verify-receipt", "replicate"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        if name in ("init", "check"):
            p.add_argument("--companion", metavar="DIR")
            p.add_argument("--bin-timeout", type=float, default=BIN_TIMEOUT)
        if name == "init":
            p.add_argument("--pipeline", choices=("python", "r"))
        if name == "query":
            p.add_argument("--index", required=True, metavar="FILE")
        if name == "release":
            p.add_argument("--out", required=True)
    return top


COMMANDS = {"init": cmd_init, "query": cmd_query, "check": cmd_check, "finalize": cmd_finalize,
            "release": cmd_release, "verify-receipt": cmd_verify_receipt, "replicate": cmd_replicate}


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        return COMMANDS[args.command](args)
    except Exit as stop:
        if stop.message:
            print(f"research-integrity: {stop.message}", file=sys.stderr)
        return stop.code


class _Adapter:
    """Generic describe/handle adapter (see modules/contracts)."""

    def describe(self):
        return {"module": "research-integrity", "companion": "provenote",
                "operations": ["init", "query", "check", "finalize", "release", "verify-receipt", "replicate"]}

    def handle(self):
        return {"companion": None, "integrated": False, "checks": {name: NO_COMPANION for name in ("quotes", "claims")}}


ADAPTER = _Adapter()

if __name__ == "__main__":
    sys.exit(main())
