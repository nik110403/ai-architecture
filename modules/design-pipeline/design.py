#!/usr/bin/env python3
"""Design pipeline: records, rendered inspection, critic bundle, receipt and its verifier (stdlib only).

Subcommands:
  route --lane L --record R [--product P] [--run DIR]   validate the front door; a missing token set writes
                                                          decision-request.json and exits 2
  inspect --tree DIR --record R [--adapters a,b] [--out DIR] [--allow-playwright]
                                                          render in real Chrome and run the checks
  verify RECEIPT [--run DIR]                              re-render every round and re-derive the verdict
  diagnose RUN                                            name the failing trace steps of the first round
  deps [--skills-dir D] [--json]                          upstream slots: unavailable | present-untested, never used

Exit codes: 0 PASS; 1 FAIL or invalid receipt; 2 refused; 3 NOT RUN (no browser rendered). No path maps NOT RUN to 0.
The workers that fill the seats are authored stand-ins: a pass proves the contract held, not that a model judged well.
"""
import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load("aiarch_design_render", "render.py")
checks = render.checks
Refused, NotRun = render.Refused, render.NotRun
PIPE = json.loads((HERE / "pipeline.json").read_text(encoding="utf-8"))
ADAPT = json.loads((HERE / "adapters.json").read_text(encoding="utf-8"))
SCHEMAS = {n: json.loads((HERE / "schemas" / f"{n}.json").read_text(encoding="utf-8"))
           for n in ("design-record", "product-record", "findings")}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
RECEIPT_KEYS = {"version", "kind", "lane", "surface", "record_sha", "product_sha", "adapters", "repo", "base_commit",
                "rounds", "rendered", "verdict", "self_sha256"}
BUILDER_DIR, CRITIC_DIR = "dispatch-builder", "dispatch-critic"


class Failed(Exception):
    """Exit 1: the evidence did not hold."""


def need(condition, reason):
    if not condition:
        raise Failed(reason)


def valid(condition, reason):
    if not condition:
        raise Refused(reason)


# --- hashing --------------------------------------------------------------------------------------------------------

def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def dump(obj):
    return json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n"


def strip(obj, drop):
    if isinstance(obj, dict):
        return {k: strip(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [strip(v, drop) for v in obj]
    return obj


def stable_sha256(doc):
    """Hash without the closed set of per-run fields listed in pipeline.json."""
    return sha(canon(strip(doc, set(PIPE["stable_exclude"]) | set(PIPE["derived_fields"]))))


def seal(receipt):
    body = {k: v for k, v in receipt.items() if k != "self_sha256"}
    return dict(body, self_sha256=sha(canon(body)))


def read_json(path, what):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise Refused(f"{what} unreadable or not JSON")
    return value


# --- records --------------------------------------------------------------------------------------------------------

def check_value(value, spec, path, missing):
    kind = spec["type"]
    if kind == "string":
        valid(isinstance(value, str) and value, f"{path} must be a non-empty string")
        valid("enum" not in spec or value in spec["enum"], f"{path} must be one of {spec.get('enum')}")
    elif kind == "bool":
        valid(isinstance(value, bool), f"{path} must be true or false")
    elif kind == "list":
        valid(isinstance(value, list), f"{path} must be a list")
        for index, item in enumerate(value):
            check_value(item, spec["item"], f"{path}[{index}]", missing)
    elif kind == "object":
        valid(isinstance(value, dict), f"{path} must be an object")
        check_fields(value, spec.get("fields", {}), path, missing)


def check_fields(doc, fields, path, missing):
    unknown = sorted(set(doc) - set(fields))
    valid(not unknown, f"{path or 'record'}: unknown keys {unknown}")
    for name, spec in fields.items():
        if name not in doc:
            if spec.get("decision"):
                missing.append(name)
            else:
                valid(not spec.get("required"), f"{path + '.' if path else ''}{name} is required")
            continue
        check_value(doc[name], spec, f"{path + '.' if path else ''}{name}", missing)


def validate(doc, schema_name):
    """Missing decision fields (taste the project has not decided) as a list; any other defect is a Refused."""
    valid(isinstance(doc, dict), "record must be a JSON object")
    missing = []
    check_fields(doc, SCHEMAS[schema_name]["fields"], "", missing)
    return missing


def normalise(record):
    """The record with defaults filled in. Never invents tokens."""
    out = dict(record)
    out.setdefault("viewports", list(PIPE["default_viewports"]))
    out.setdefault("motion", False)
    out.setdefault("motion_targets", [])
    for text in out["viewports"]:
        valid(re.fullmatch(r"[1-9][0-9]{2,4}x[1-9][0-9]{2,4}", text), "viewport must look like 1280x800")
    return out


def tokens_empty(record):
    tokens = record.get("tokens")
    return not isinstance(tokens, dict) or not (tokens.get("colors") or tokens.get("fonts"))


def route(lane, record_doc, product_doc, run=None):
    """The front door. Returns the normalised record. An undecided token set is a decision request, never a guess."""
    valid(lane in PIPE["lanes"], f"unknown lane {lane!r}; use one of {PIPE['lanes']}")
    missing = validate(record_doc, "design-record")
    if lane == "product":
        valid(product_doc is not None, "lane product needs a product record")
        validate(product_doc, "product-record")
    if missing or tokens_empty(record_doc):
        request = {"version": 1, "kind": "decision-request", "needs": ["tokens"],
                   "question": "Which colors and fonts may this surface use? The pipeline has no defaults and builds nothing until the project decides."}
        if run is not None:
            Path(run, "decision-request.json").write_text(dump(request), encoding="utf-8")
        raise Refused("decision request: the record has no tokens")
    return normalise(record_doc)


class Pipeline:
    """Stage order from pipeline.json. At most `max_revisions` revision rounds; a third is refused."""

    def __init__(self):
        self.last, self.revisions = "start", 0

    def enter(self, stage):
        valid(stage in PIPE["next"].get(self.last, []), f"stage order: {self.last} -> {stage}")
        if stage == "revise":
            valid(self.revisions < PIPE["max_revisions"], "round budget: at most two revision rounds")
            self.revisions += 1
        self.last = stage


# --- adapters -------------------------------------------------------------------------------------------------------

def select_adapters(record, requested=None):
    names = list(requested) if requested else list(PIPE["default_adapters"])
    for name in names:
        valid(name in ADAPT["adapters"], f"unknown adapter {name!r}")
    if "accessibility" not in names:
        names.insert(0, "accessibility")
    if requested and "motion" in requested:
        valid(record.get("motion") is True, "adapter out of scope: motion applies only when the record sets motion")
        valid(record.get("motion_targets"), "adapter out of scope: motion needs motion_targets")
    if not requested and not record.get("motion"):
        names = [n for n in names if n != "motion"]
    if "style" in names:
        valid(not tokens_empty(record), "style adapter needs record tokens; it never falls back to a default")
    return names


def deps(skills_dir=None):
    """Upstream slots. Existence of <dir>/<id>/SKILL.md only; the file is never opened. Never `used`."""
    if skills_dir is not None:
        valid(Path(skills_dir).is_dir(), "skills dir is not a directory")
    out = []
    for slot in ADAPT["slots"]:
        if skills_dir is None:
            state, reason = "unavailable", "no skills dir given"
        else:
            probe = Path(skills_dir) / slot["id"] / "SKILL.md"
            try:
                present = stat.S_ISREG(os.lstat(probe).st_mode)
            except OSError:
                present = False
            state, reason = ("present-untested", "SKILL.md present; activation and behaviour untested") if present \
                else ("unavailable", "not found in the skills dir")
        out.append({"id": slot["id"], "role": slot["role"], "source": slot["source"], "license": slot["license"],
                    "state": state, "reason": reason, "effect": ADAPT["upstream_effect"]})
    return out


def adapter_records(names, skills_dir=None):
    states = {d["id"]: d["state"] for d in deps(skills_dir)}
    return [{"name": n, "upstream": {"id": ADAPT["adapters"][n]["upstream"], "state": states[ADAPT["adapters"][n]["upstream"]]}}
            for n in names]


# --- rendering ------------------------------------------------------------------------------------------------------

def shot_sizes(insp):
    first = insp["viewports"][0]
    sizes = {f"vp-{vp['name']}": (vp["w"], vp["h"]) for vp in insp["viewports"]}
    return sizes, (first["w"], first["h"])


def shot_problems(out, insp):
    """Every screenshot must be a PNG of the viewport size with at least two distinct pixels."""
    sizes, default = shot_sizes(insp)
    problems = []
    shots = sorted(Path(out, "screenshots").glob("*.png"))
    if not shots:
        problems.append("no screenshots")
    for path in shots:
        width, height = sizes.get(path.stem, default)
        why = checks.png_problem(path.read_bytes(), width, height)
        if why:
            problems.append(f"{path.name}: {why}")
    return problems


def do_render(repo, commit, record, out, adapters):
    """Render one commit into `out`. Returns the round's render record; state is `ran` or `NOT RUN` (with a reason)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        insp = render.render_commit(repo, commit, record, out, adapters)
    except NotRun as error:
        return {"state": "NOT RUN", "reason": str(error)[:160]}
    results = checks.run_checks(insp, record, adapters)
    insp = dict(insp, checks=results, adapters=adapters)
    (out / "inspection.json").write_text(dump(insp), encoding="utf-8")
    browser = [c for c in results if c["cause"] == "browser"]
    if browser:
        return {"state": "NOT RUN", "reason": f"check error: {browser[0]['check']}: "
                                              f"{insp['errors'].get(browser[0]['check'], 'browser failure')}"[:160]}
    shots = []
    sizes, default = shot_sizes(insp)
    for path in sorted((out / "screenshots").glob("*.png")):
        width, height = sizes.get(path.stem, default)
        shots.append({"path": f"screenshots/{path.name}", "sha256": sha(path.read_bytes()), "w": width, "h": height})
    return {"state": "ran", "dir": out.name, "renderer": insp["renderer"], "inspection_sha": sha((out / "inspection.json").read_bytes()),
            "trace_sha": sha((out / "trace.jsonl").read_bytes()), "results": {c["check"]: c["result"] for c in results},
            "shots": shots, "problems": shot_problems(out, insp)}


def evidence_ids(inspection):
    return {e["id"] for c in inspection["checks"] for e in c["evidence"]}


# --- the critic's bundle ---------------------------------------------------------------------------------------------

def copy_regular(src, dst):
    mode = os.lstat(src).st_mode
    valid(stat.S_ISREG(mode), "refusing to copy a link or special file into the bundle")
    shutil.copyfile(src, dst)


def bundle_files(directory):
    """Sorted relative paths of every file under `directory`; any link or special file raises Refused."""
    return render.regular_files(directory)


def bundle_manifest(directory):
    """bundle.json content for `directory` (every file except bundle.json itself, each with its sha256)."""
    files = {name: sha(Path(directory, name).read_bytes()) for name in bundle_files(directory) if name != "bundle.json"}
    return {"version": 1, "files": files}


def write_bundle(bundle, render_dir, repo, commit, record, n):
    """The critic's whole world: screenshots, inspection, trace, the source tree at the commit, the record, bundle.json."""
    bundle, render_dir = Path(bundle), Path(render_dir)
    (bundle / "screenshots").mkdir(parents=True)
    for path in sorted((render_dir / "screenshots").glob("*.png")):
        copy_regular(path, bundle / "screenshots" / path.name)
    for name in ("inspection.json", "trace.jsonl"):
        copy_regular(render_dir / name, bundle / name)
    render.extract_tree(repo, commit, bundle / "source")
    (bundle / "record.json").write_text(dump(record), encoding="utf-8")
    manifest = dict(bundle_manifest(bundle), round=n)
    (bundle / "bundle.json").write_text(dump(manifest), encoding="utf-8")
    return sha(canon(manifest))


def critic_prompt(bundle):
    manifest = read_json(Path(bundle, "bundle.json"), "bundle.json")
    return {"bundle": manifest, "record": read_json(Path(bundle, "record.json"), "record.json"), "round": manifest["round"]}


# --- dispatch job records (the files are the source of truth) ---------------------------------------------------------

def anchored(path):
    """worktrees/<job> or bundles/<round>: the job's directory relative to its run, wherever the run was moved to."""
    parts = Path(os.path.realpath(path)).parts
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] in ("worktrees", "bundles"):
            return "/".join(parts[index:])
    return os.path.basename(os.path.realpath(path))


def job_record(run, folder_name, job_id):
    """Record derived from <run>/<folder_name>/<job_id>/{result,start}.json and stdout.txt. Returns (record, stdout)."""
    valid(isinstance(job_id, str) and NAME.fullmatch(job_id), "malformed job id")
    folder = Path(run, folder_name, job_id)
    try:
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        start = json.loads((folder / "start.json").read_text(encoding="utf-8"))
        stdout = (folder / "stdout.txt").read_bytes()
        dispatch, outcome = result["dispatch"], result["result"]
        workdir = anchored(start["workdir"])
        worktree = start.get("worktree")
        record = {
            "role": dispatch["role"], "job_id": job_id, "seat": start["seat"], "stdout_sha256": sha(stdout),
            "started_at": dispatch["started_at"], "finished_at": outcome["finished_at"], "pid": dispatch["pid"],
            "workdir": workdir, "branch": worktree["branch"] if worktree else None,
            "argv_sha256": sha(canon(start["argv"])), "dir": folder_name,
        }
        succeeded = outcome["outcome"] == "succeeded" and dispatch["exit_code"] == 0
        digest = dispatch["stdout"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Failed(f"job record disagrees with dispatch files: {job_id} unreadable")
    need(succeeded, f"job {job_id} did not succeed")
    need(digest == record["stdout_sha256"], f"job record disagrees with dispatch files: {job_id} stdout")
    return record, stdout


def parse_object(stdout, what):
    try:
        value = json.loads(stdout)
    except ValueError:
        raise Failed(f"{what}: stdout is not JSON")
    need(isinstance(value, dict), f"{what}: stdout is not a JSON object")
    return value


# --- verdict --------------------------------------------------------------------------------------------------------

def decide(final):
    """(status, reasons) from the final round: {render, critic}. The critic can only add blockers or note a false approval."""
    rendered = final["render"]
    if rendered["state"] != "ran":
        return "NOT RUN", [f"rendered: NOT RUN ({rendered.get('reason', 'no browser')})"]
    reasons = [f"screenshot: {p}" for p in rendered["problems"]]
    failing = sorted(c for c, r in rendered["results"].items() if r != "pass")
    reasons += [f"check not passing: {c} ({rendered['results'][c]})" for c in failing]
    if rendered["results"].get("required-present", "pass") != "pass":
        reasons.append("required element missing")
    critic = final.get("critic")
    if critic is None:
        reasons.append("critic missing")
    else:
        blocking = [f for f in critic["findings"] if f["priority"] == "blocking"]
        if failing and not blocking:
            reasons.insert(0, "critic approved failing render")
        if not failing and blocking:
            reasons.append("blocking finding")
    return ("FAIL" if reasons else "PASS"), reasons


def print_rendered(final_render):
    if final_render["state"] == "ran":
        print("rendered: ran")
    else:
        print(f"rendered: NOT RUN ({final_render.get('reason', 'no browser')})")


# --- verify ---------------------------------------------------------------------------------------------------------

def tree_sha(repo, commit):
    try:
        return render.commit_info(repo, commit)[0]
    except Refused as error:
        raise Failed(f"round commit cannot be read from the repo: {error}")


def load_receipt(path, run_arg):
    receipt_file = Path(os.path.realpath(path))
    run = Path(os.path.realpath(run_arg)) if run_arg else receipt_file.parent
    valid(run.is_dir() and run in receipt_file.parents, "receipt path escapes the run")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise Failed("receipt unreadable or not JSON")
    need(isinstance(receipt, dict) and set(receipt) == RECEIPT_KEYS, "invalid receipt: fields")
    need(receipt["self_sha256"] == sha(canon({k: v for k, v in receipt.items() if k != "self_sha256"})),
         "receipt self hash mismatch")
    return receipt, run


def rel(run, text):
    need(isinstance(text, str) and text and not os.path.isabs(text) and ".." not in Path(text).parts and "\0" not in text,
         "path escapes the run")
    real = Path(os.path.realpath(run / text))
    need(real == run or run in real.parents, "path escapes the run")
    return real


def check_round_files(run, rnd, record):
    """Round files: hashes, PNG validity, bundle equal to the render output, and a closed bundle file set."""
    r = rnd["render"]
    base = rel(run, f"renders/r{rnd['n']}")
    need(base.is_dir(), "render directory missing")
    for key, name in (("inspection_sha", "inspection.json"), ("trace_sha", "trace.jsonl")):
        need((base / name).is_file() and sha((base / name).read_bytes()) == r[key], f"{name} sha mismatch")
    listed = {s["path"] for s in r["shots"]}
    on_disk = {f"screenshots/{p.name}" for p in (base / "screenshots").glob("*.png")} if (base / "screenshots").is_dir() else set()
    need(listed == on_disk and listed, "screenshot missing or unlisted")
    inspection = read_json(base / "inspection.json", "inspection.json")
    for shot in r["shots"]:
        data = (base / shot["path"]).read_bytes()
        need(sha(data) == shot["sha256"], f"screenshot sha mismatch: {shot['path']}")
        why = checks.png_problem(data, shot["w"], shot["h"])
        need(why is None, f"screenshot {shot['path']}: {why}")
    need(r["renderer"].get("name") == "chrome" and r["renderer"] == inspection.get("renderer"), "renderer is not recorded chrome")
    need({c["check"]: c["result"] for c in inspection["checks"]} == r["results"], "recorded results disagree with inspection")
    return base, inspection


def check_bundle(run, repo, rnd, base, record):
    b = rnd["bundle"]
    bundle = rel(run, b["dir"])
    need(bundle.is_dir(), "bundle missing")
    try:
        names = bundle_files(bundle)
    except Refused:
        raise Failed("bundle contains a link or special file")
    manifest = read_json(bundle / "bundle.json", "bundle.json")
    need(sha(canon(manifest)) == b["bundle_sha"], "bundle sha mismatch")
    need(".git" not in Path(bundle).parts and not any(Path(n).parts[0] == ".git" or ".git" in Path(n).parts for n in names),
         "bundle has .git")
    need(isinstance(manifest.get("files"), dict) and sorted(manifest["files"]) == [n for n in names if n != "bundle.json"],
         "bundle file set is not closed")
    for name, digest in manifest["files"].items():
        need(sha((bundle / name).read_bytes()) == digest, f"bundle file sha mismatch: {name}")
    allowed = ("screenshots/", "source/")
    need(all(n in ("inspection.json", "trace.jsonl", "record.json", "bundle.json") or n.startswith(allowed) for n in names),
         "bundle holds a file outside the closed set")
    for name in ["inspection.json", "trace.jsonl"] + [s["path"] for s in rnd["render"]["shots"]]:
        need((bundle / name).read_bytes() == (base / name).read_bytes(), f"bundle differs from render output: {name}")
    need(read_json(bundle / "record.json", "record") == record, "bundle record differs from the run record")
    with tempfile.TemporaryDirectory(prefix="design-verify-") as tmp:
        try:
            render.extract_tree(repo, rnd["commit"], tmp)
        except Refused as error:
            raise Failed(f"round commit cannot be read from the repo: {error}")
        expect = {f"source/{n}": sha(Path(tmp, n).read_bytes()) for n in render.regular_files(tmp)}
    got = {n: d for n, d in manifest["files"].items() if n.startswith("source/")}
    need(got == expect, "bundle source differs from the files of the commit")
    return bundle, manifest


def check_critic(run, rnd, bundle, base, inspection, builder_jobs):
    c = rnd["critic"]
    need(isinstance(c, dict), "critic missing")
    record, stdout = job_record(run, CRITIC_DIR, c["job"]["job_id"])
    need(record == c["job"], f"job record disagrees with dispatch files: {record['job_id']}")
    need(record["role"] == "design-critic", "critic job has the wrong role")
    for job in builder_jobs:
        need(job["job_id"] != record["job_id"], "critic job equals a builder job")
        need(job["pid"] != record["pid"], "critic and builder share a pid")
        need(job["role"] != record["role"] and job["seat"] != record["seat"], "critic and builder share a role or seat")
        need(job["argv_sha256"] != record["argv_sha256"], "critic and builder share an argv")
    work = rel(run, record["workdir"])
    need(work == Path(os.path.realpath(bundle)), "critic workdir is not the bundle")
    for job in builder_jobs:
        other = rel(run, job["workdir"])
        need(work != other and other not in work.parents and work not in other.parents, "critic workdir overlaps the builder's")
    need(not (run / BUILDER_DIR in work.parents or run / "worktrees" in work.parents), "critic workdir lies in the builder's tree")
    prompt = read_json(run / CRITIC_DIR / record["job_id"] / "prompt.txt", "critic prompt")
    need(prompt == critic_prompt(bundle), "critic prompt is not built from the bundle and the record only")
    out = parse_object(stdout, "critic")
    need(set(out) == set(SCHEMAS["findings"]["fields"]), "critic output fields")
    need(out["findings"] == c["findings"] and out["approved"] == c["approved"]
         and out["claims_browser_validation"] == c["claims_browser_validation"], "critic record differs from its output")
    valid_ids = evidence_ids(inspection)
    shots = [n for n in bundle_files(bundle) if n.startswith("screenshots/")]
    bundle_sha = rnd["bundle"]["bundle_sha"]
    for f in c["findings"]:
        need(isinstance(f, dict) and set(f) == set(SCHEMAS["findings"]["fields"]["findings"]["item"]["fields"]),
             "finding fields")
        need(f["priority"] in ("blocking", "advisory"), "finding priority")
        need(f["bundle_sha"] == bundle_sha, "finding carries a different bundle_sha")
        need(f["evidence_id"] in valid_ids, "finding cites an evidence id that is not in the bundle")
    need(c["approved"] == (not any(f["priority"] == "blocking" for f in c["findings"])), "critic approval disagrees with its findings")
    need(not c["claims_browser_validation"] or shots, "critic claims browser validation but the bundle has no screenshot")
    need(c["findings_sha"] == sha(canon(c["findings"])), "findings sha mismatch")
    return c


def check_builder(run, rnd, prior_critic):
    job = rnd["build_job"]
    record, _ = job_record(run, BUILDER_DIR, job["job_id"])
    need(record == job and record["role"] == "design-builder", f"job record disagrees with dispatch files: {record['job_id']}")
    prompt = read_json(run / BUILDER_DIR / record["job_id"] / "prompt.txt", "builder prompt")
    if rnd["n"] > 0:
        need(prior_critic is not None, "revision without a critic round")
        need(set(prompt) == {"stage", "surface", "findings", "findings_sha"} and prompt["findings"] == prior_critic["findings"]
             and prompt["findings_sha"] == prior_critic["findings_sha"], "revision prompt is not the findings and their sha only")
    return job


def render_agrees(recorded, base, repo, commit, record, adapters, tmp):
    """Re-render and compare with the recorded round. Raises NotRun (exit 3) or Failed (exit 1)."""
    fresh = do_render(repo, commit, record, tmp, adapters)
    if fresh["state"] != "ran":
        raise NotRun(fresh["reason"])
    need(fresh["results"] == recorded["results"], "re-render disagrees: check results differ "
         f"({sorted(k for k in fresh['results'] if fresh['results'][k] != recorded['results'].get(k))})")
    again, was = read_json(tmp / "inspection.json", "inspection"), read_json(base / "inspection.json", "inspection")
    keep = lambda doc: sorted((c["check"], e["selector"], e["viewport"], e["result"]) for c in doc["checks"] for e in c["evidence"])
    need(keep(again) == keep(was), "re-render disagrees: evidence differs")
    steps = lambda path: [{k: json.loads(l)[k] for k in ("step", "input", "expect", "observed")}
                          for l in Path(path).read_text(encoding="utf-8").splitlines()]
    need(steps(tmp / "trace.jsonl") == steps(base / "trace.jsonl"), "re-render disagrees: trace differs")
    if PIPE["png_deterministic"]:
        need({s["path"]: s["sha256"] for s in fresh["shots"]} == {s["path"]: s["sha256"] for s in recorded["shots"]},
             "re-render disagrees: screenshots differ")


def verify(receipt_path, run_arg=None):
    """Raises Failed (1), Refused (2) or NotRun (3). Returns normally only for PASS."""
    receipt, run = load_receipt(receipt_path, run_arg)
    need(receipt["version"] == 1 and receipt["kind"] == "design-receipt", "invalid receipt: kind")
    need(receipt["lane"] in PIPE["lanes"], "invalid receipt: lane")
    verdict, rendered, rounds = receipt["verdict"], receipt["rendered"], receipt["rounds"]
    need(isinstance(rounds, list) and 1 <= len(rounds) <= 1 + PIPE["max_revisions"], "round budget exceeded")
    if verdict.get("status") == "NOT RUN" or rendered.get("state") != "ran":
        print(f"rendered: NOT RUN ({rendered.get('reason', 'no browser')})")
        raise NotRun(rendered.get("reason", "no browser"))
    for name in ("record.json",):
        need((run / name).is_file(), f"{name} missing")
    record_doc = read_json(run / "record.json", "record")
    need(sha((run / "record.json").read_bytes()) == receipt["record_sha"], "record sha mismatch")
    product = read_json(run / "product.json", "product record") if receipt["product_sha"] else None
    need(not receipt["product_sha"] or sha((run / "product.json").read_bytes()) == receipt["product_sha"], "product sha mismatch")
    try:
        record = route(receipt["lane"], record_doc, product)
    except Refused as error:
        raise Failed(f"record invalid: {error}")
    names = [a["name"] for a in receipt["adapters"]]
    for a in receipt["adapters"]:
        need(a["upstream"]["state"] in ADAPT["upstream_states"], f"adapter upstream state {a['upstream']['state']!r} claims more than evidence")
    try:
        need(names == select_adapters(record, names), "adapter selection disagrees with the record")
    except Refused as error:
        raise Failed(str(error))
    repo = rel(run, receipt["repo"])
    need(COMMIT.fullmatch(receipt["base_commit"]), "base commit malformed")
    prior, previous, critic_prev, builders, loaded = receipt["base_commit"], None, None, [], []
    for index, rnd in enumerate(rounds):
        need(rnd.get("n") == index and COMMIT.fullmatch(str(rnd.get("commit", ""))), "round malformed")
        try:
            parents = render.commit_info(repo, rnd["commit"])[1]
        except Refused as error:
            raise Failed(f"round commit cannot be read from the repo: {error}")
        need(parents[:1] == [prior], "revision is not a child of the previous commit")
        if index > 0:
            need(tree_sha(repo, rnd["commit"]) != tree_sha(repo, previous), "findings unresolved: the tree did not change")
        prior = previous = rnd["commit"]
        builders.append(check_builder(run, rnd, critic_prev))
        need(rnd["render"]["state"] == "ran", "round did not render")
        base, inspection = check_round_files(run, rnd, record)
        bundle, _ = check_bundle(run, repo, rnd, base, record)
        need(rnd.get("critic") is not None, "critic missing")
        critic_prev = check_critic(run, rnd, bundle, base, inspection, builders)
        rnd["critic"]["findings"] = critic_prev["findings"]
        loaded.append((rnd, base))
    def again(item):
        rnd, base = item
        with tempfile.TemporaryDirectory(prefix="design-rerender-") as tmp:
            render_agrees(rnd["render"], base, repo, rnd["commit"], record, names, Path(tmp))

    try:  # last: every structural check passed, now the browser has the final say (rounds render side by side)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(loaded)) as pool:
            list(pool.map(again, loaded))
    except NotRun as error:
        print(f"rendered: NOT RUN ({str(error)[:160]})")
        raise
    final = dict(rounds[-1])
    status, reasons = decide(final)
    need(verdict == {"status": status, "reasons": reasons}, "verdict disagrees with the re-derived one")
    print("rendered: ran")
    need(status == "PASS", "; ".join(reasons))


def diagnose(run_arg):
    run = Path(os.path.realpath(run_arg))
    valid((run / "receipt.json").is_file(), "no receipt in the run")
    receipt = read_json(run / "receipt.json", "receipt")
    lines = []
    for rnd in receipt["rounds"][:1]:
        trace = Path(run, f"renders/r{rnd['n']}", "trace.jsonl")
        valid(trace.is_file(), "no trace for the first round")
        for text in trace.read_text(encoding="utf-8").splitlines():
            step = json.loads(text)
            if step["step"].startswith("interaction:") and step["expect"] is not None and "blocked" not in (step["observed"] or {}):
                if not render.expect_met(step["expect"], step["observed"] or {}):
                    lines.append(f"{step['step']}: key {step['input']['key']} on {step['input']['target']}: expected "
                                 f"{json.dumps(step['expect'], sort_keys=True)} observed {json.dumps(step['observed'], sort_keys=True)} "
                                 f"screenshot {step['screenshot_sha']}")
            elif step["step"].startswith("interaction:") and "blocked" in (step["observed"] or {}):
                lines.append(f"{step['step']}: key {step['input']['key']} on {step['input']['target']}: blocked "
                             f"(keyboard-reach) screenshot none")
    print("\n".join(lines) if lines else "no failing interaction step in the first round")
    return 0


# --- cli ------------------------------------------------------------------------------------------------------------

def cmd_inspect(args):
    record_doc = read_json(args.record, "record")
    valid(Path(args.tree).is_dir(), "--tree is not a directory")
    validate(record_doc, "design-record")
    record = normalise(record_doc)
    requested = [a for a in args.adapters.split(",") if a] if args.adapters else None
    names = select_adapters(record, requested)
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="design-inspect-"))
    out.mkdir(parents=True, exist_ok=True)
    report = {"adapters": names, "out": str(out)}
    if args.allow_playwright:  # a contract run beside the render; its outcome is reported and never feeds the verdict
        report["playwright"] = _load("aiarch_design_playwright", "playwright.py").run(args.tree, out / "playwright", allow=True)
    try:
        insp = render.render_tree(args.tree, record, out, names)
    except NotRun as error:
        report["rendered"] = "NOT RUN"
        print(json.dumps(report, sort_keys=True))
        print(f"rendered: NOT RUN ({str(error)[:160]})")
        return 3
    results = checks.run_checks(insp, record, names)
    (out / "inspection.json").write_text(dump(dict(insp, checks=results, adapters=names)), encoding="utf-8")
    summary = {c["check"]: c["result"] for c in results}
    browser = [c["check"] for c in results if c["cause"] == "browser"]
    problems = shot_problems(out, insp)
    report.update(rendered="ran", checks=summary, problems=problems)
    if browser:
        report["rendered"] = "NOT RUN"
        print(json.dumps(report, sort_keys=True))
        print(f"rendered: NOT RUN (check error: {browser[0]})")
        return 3
    print(json.dumps(report, sort_keys=True))
    print("rendered: ran")
    return 0 if all(r == "pass" for r in summary.values()) and not problems else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("route")
    p.add_argument("--lane", required=True)
    p.add_argument("--record", required=True)
    p.add_argument("--product")
    p.add_argument("--run")
    p = sub.add_parser("inspect")
    p.add_argument("--tree", required=True)
    p.add_argument("--record", required=True)
    p.add_argument("--adapters")
    p.add_argument("--out")
    p.add_argument("--allow-playwright", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("receipt")
    p.add_argument("--run")
    p = sub.add_parser("diagnose")
    p.add_argument("run")
    p = sub.add_parser("deps")
    p.add_argument("--skills-dir")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "route":
            route(args.lane, read_json(args.record, "record"), read_json(args.product, "product record") if args.product else None,
                  args.run)
            print(json.dumps({"status": "ok"}))
            return 0
        if args.command == "inspect":
            return cmd_inspect(args)
        if args.command == "verify":
            verify(args.receipt, args.run)
            print(json.dumps({"status": "pass", "reason": None}))
            return 0
        if args.command == "diagnose":
            return diagnose(args.run)
        report = {"slots": deps(args.skills_dir)}
        if args.json:
            print(dump(report))
        else:
            for d in report["slots"]:
                print(f"{d['id']} [{d['license']}] {d['state']} - {d['reason']} ({d['effect']})")
        return 0
    except Failed as error:
        print(json.dumps({"status": "fail", "reason": str(error)}))
        return 1
    except Refused as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except NotRun as error:
        print(json.dumps({"status": "not-run", "reason": str(error)}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
