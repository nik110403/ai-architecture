#!/usr/bin/env python3
"""Development/security/domain journey contracts and checker (stdlib only; no model, no test framework).

Subcommands:
  check --kind K ARTIFACT --run R   re-execute the artifact's evidence; exit 0 pass, 1 fail (named reason), 2 refused
  deps [--skills-dir D] [--json]     per journey: upstream dependency, source, license, state, effect
  verify-receipt RECEIPT [--run R]   load every artifact by sha, re-run `check` for each in a fresh process

Artifact strings are data: argv is built here from journeys.json and validated hex commits, never from the
artifact. The workers are authored stand-ins, so a pass proves the contract held, not that a model judged well.
"""

import argparse
import ast
import concurrent.futures
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTRACT = json.loads((HERE / "journeys.json").read_text(encoding="utf-8"))
JOURNEYS = CONTRACT["journeys"]
KINDS = CONTRACT["kinds"]
FIXTURES = ROOT / "examples" / "development" / "fixtures"
SOURCES = ROOT / "modules" / "contracts" / "sources.json"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
JOB_KEYS = {"role", "job_id", "stdout_sha256", "started_at", "finished_at", "pid", "worktree"}
ART_KEYS = {"version", "kind", "journey_id", "jobs", "evidence", "upstream"}
EVIDENCE_KEYS = {
    "debug": {"repo", "base_commit", "red_commit", "green_commit", "test_id", "recorded", "reproduce", "scope", "reuse",
              "new_dependencies", "dependency_decisions", "inputs"},
    "domain": {"repo", "commit", "finding", "verdict", "improvement", "maker_job", "refuter_job", "inputs"},
    "secure-build": {"repo", "base_commit", "build_commit", "threats", "benign", "review", "inputs"},
    "security-audit": {"repo", "target_commit", "map", "findings", "inputs"},
    "incident": {"repo", "base_commit", "metric", "facts", "candidates", "ranking", "measurements", "inputs"},
}
GIT_TIMEOUT, RUN_TIMEOUT = 60, 30
GIT_HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "gc.auto=0"]
FIXED_DATE = "2026-01-01T00:00:00Z"


class Failed(Exception):
    """Exit 1: the evidence did not hold."""


class Refused(Exception):
    """Exit 2: malformed input, unknown kind, or a path escaping the run."""


def need(condition, reason):
    if not condition:
        raise Failed(reason)


def valid(condition, reason):
    if not condition:
        raise Refused(reason)


# --- hashing ------------------------------------------------------------------------------------------------------

def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def dump(obj):
    return json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _strip(obj, drop):
    if isinstance(obj, dict):
        return {k: _strip(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [_strip(v, drop) for v in obj]
    return obj


def stable_sha256(artifact):
    """Hash of the artifact without the closed set of per-run fields listed in journeys.json."""
    return sha(canon(_strip(artifact, set(CONTRACT["stable_exclude"]))))


# --- safe paths, git, scripts -------------------------------------------------------------------------------------

def run_path(text):
    path = Path(str(text))
    valid(path.is_dir(), "run dir is not a directory")
    return Path(os.path.realpath(path))


def rel(run, text):
    """A run-relative path that stays inside the run after symlinks resolve (Refused otherwise)."""
    valid(isinstance(text, str) and text and "\0" not in text, "malformed path")
    valid(not os.path.isabs(text) and ".." not in Path(text).parts, "path escapes the run")
    real = Path(os.path.realpath(run / text))
    valid(real == run or run in real.parents, "path escapes the run")
    return real


def safe_path():
    kept = [d for d in os.environ.get("PATH", os.defpath).split(os.pathsep) if os.path.isabs(d)]
    return os.pathsep.join(kept) or "/usr/bin:/bin"


def git_env():
    return {
        "PATH": safe_path(), "LANG": "C", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE,
    }


def git(repo, *argv, binary=False, ok=False):
    """Run git hardened against the host (no global/system config, no hooks, no signing). Failed unless ok=True."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *GIT_HARDEN, *argv], capture_output=True, env=git_env(),
                              timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failed(f"git unavailable: {error.__class__.__name__}")
    if ok:
        return proc
    need(proc.returncode == 0, f"git {argv[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:120]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def commit_of(repo, value, what):
    need(isinstance(value, str) and COMMIT.fullmatch(value), f"{what} is not a full commit id")
    need(git(repo, "cat-file", "-t", value, ok=True).stdout.strip() == b"commit", f"{what} is not a commit in the repo")
    return value


def blob(repo, commit, name):
    proc = git(repo, "show", f"{commit}:{name}", ok=True)
    return proc.stdout if proc.returncode == 0 else None


@contextmanager
def extracted(repo, commit):
    """The commit's tree in a fresh temp dir (git archive; tar data filter refuses links that escape)."""
    data = git(repo, "archive", "--format=tar", commit, binary=True)
    with tempfile.TemporaryDirectory(prefix="dev-check-") as tmp:
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(tmp, filter="data")
        except (tarfile.TarError, OSError) as error:
            raise Failed(f"cannot extract {commit[:12]}: {error.__class__.__name__}")
        yield Path(tmp)


def run_script(script, args, cwd):
    """`<this python> -B -I script args`, fresh process, scrubbed env. The script and args are built by us."""
    env = {"PATH": safe_path(), "LANG": "C", "HOME": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run([sys.executable, "-B", "-I", str(script), *map(str, args)], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failed(f"script {Path(script).name} did not run: {error.__class__.__name__}")


def script_json(script, args, cwd):
    proc = run_script(script, args, cwd)
    try:
        value = json.loads(proc.stdout)
    except ValueError:
        raise Failed(f"script {Path(script).name} printed no JSON (rc {proc.returncode})")
    need(isinstance(value, dict), f"script {Path(script).name} printed no JSON object")
    return value


# --- dispatch job records (the files are the source of truth) ----------------------------------------------------

def job_record(run, job_id):
    """The job record derived from <run>/dispatch/<job_id>/{result,start}.json and stdout.txt."""
    valid(isinstance(job_id, str) and NAME.fullmatch(job_id), "malformed job id")
    folder = rel(run, f"dispatch/{job_id}")
    try:
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        start = json.loads((folder / "start.json").read_text(encoding="utf-8"))
        stdout = (folder / "stdout.txt").read_bytes()
        dispatch, outcome = result["dispatch"], result["result"]
        worktree = start.get("worktree")
        if worktree:
            relative = os.path.relpath(worktree["path"], run)
            need(not relative.startswith(".."), f"job {job_id}: worktree is outside the run")
            worktree = {"path": relative, "branch": worktree["branch"]}
        record = {
            "role": dispatch["role"], "job_id": job_id, "stdout_sha256": sha(stdout),
            "started_at": dispatch["started_at"], "finished_at": outcome["finished_at"], "pid": dispatch["pid"],
            "worktree": worktree or None,
        }
        succeeded = outcome["outcome"] == "succeeded" and dispatch["exit_code"] == 0
        digest = dispatch["stdout"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Failed(f"job record disagrees with dispatch files: {job_id} unreadable")
    need(succeeded, f"job {job_id} did not succeed")
    need(digest == record["stdout_sha256"], f"job record disagrees with dispatch files: {job_id} stdout")
    return record, stdout


class Jobs:
    """The artifact's job list, each record checked against the dispatch files."""

    def __init__(self, run, records):
        valid(isinstance(records, list), "jobs must be a list")
        self.run, self.items = run, []
        for record in records:
            valid(isinstance(record, dict) and set(record) == JOB_KEYS, "malformed job record")
            expected, stdout = job_record(run, record["job_id"])
            need(record == expected, f"job record disagrees with dispatch files: {record['job_id']}")
            self.items.append((expected, stdout))

    def by_id(self, job_id, role, what):
        for record, stdout in self.items:
            if record["job_id"] == job_id:
                need(record["role"] == role, f"{what}: expected role {role}, got {record['role']}")
                return record, self.parse(stdout, job_id)
        raise Failed(f"{what} job missing")

    def by_role(self, role):
        found = [(r, self.parse(s, r["job_id"])) for r, s in self.items if r["role"] == role]
        need(len(found) >= 1, f"{role} job missing")
        return found

    @staticmethod
    def parse(stdout, job_id):
        try:
            value = json.loads(stdout)
        except ValueError:
            raise Failed(f"job {job_id}: stdout is not JSON")
        need(isinstance(value, dict), f"job {job_id}: stdout is not a JSON object")
        return value


# --- shared checks ------------------------------------------------------------------------------------------------

def upstream_for(journey, skills_dir=None):
    """Upstream state for a journey: never anything stronger than present-untested."""
    states = [d["state"] for d in deps_for(journey, skills_dir)]
    state = "present-untested" if "present-untested" in states else "unavailable"
    ids = [d["id"] for d in JOURNEYS[journey]["dependencies"]]
    return {"state": state, "ids": ids, "claim": CONTRACT["upstream_claim"]}


def check_common(run, artifact, kind):
    valid(isinstance(artifact, dict) and set(artifact) == ART_KEYS, "malformed artifact: fields")
    need(artifact["kind"] == kind and artifact["journey_id"] == kind, f"kind mismatch: artifact is {artifact['kind']!r}")
    evidence = artifact["evidence"]
    valid(isinstance(evidence, dict), "malformed artifact: evidence")
    missing = EVIDENCE_KEYS[kind] - set(evidence)
    need(not missing, "no-artifact-evidence: required evidence missing" if not (EVIDENCE_KEYS[kind] & set(evidence))
         else f"malformed evidence: missing {sorted(missing)}")
    upstream = artifact["upstream"]
    need(isinstance(upstream, dict) and set(upstream) == {"state", "ids", "claim"}, "upstream record malformed")
    need(upstream["state"] in CONTRACT["upstream_states"], f"upstream state {upstream['state']!r} claims more than evidence")
    need(upstream["claim"] == CONTRACT["upstream_claim"], "upstream claim must be 'not exercised'")
    need(upstream["ids"] == [d["id"] for d in JOURNEYS[kind]["dependencies"]], "upstream ids differ from the journey")
    contract = JOURNEYS[kind]
    need(evidence["repo"] == contract["repo"], "repo differs from the journey contract")
    check_inputs(run, evidence["inputs"], contract)
    repo = rel(run, contract["repo"])
    valid(repo.is_dir(), "repo missing from the run")
    return evidence, contract, repo


def check_inputs(run, inputs, contract):
    valid(isinstance(inputs, list), "inputs must be a list")
    seen = set()
    for item in inputs:
        valid(isinstance(item, dict) and set(item) == {"path", "sha256"}, "malformed input record")
        path = rel(run, item["path"])
        need(path.is_file() and sha(path.read_bytes()) == item["sha256"], f"input sha mismatch: {item['path']}")
        seen.add(item["path"])
    required = {f"{contract['repo']}/{name}" for name in contract["inputs"]}
    need(required <= seen, f"inputs lack {sorted(required - seen)}")


def text_lines(repo, commit, name):
    data = blob(repo, commit, name)
    need(data is not None, f"{name} missing at {commit[:12]}")
    return data.decode("utf-8", "replace").splitlines()


# --- debug --------------------------------------------------------------------------------------------------------

def import_roots(source, name):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise Failed(f"{name} does not parse")
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {n.name.split(".")[0] for n in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def defined_names(source):
    try:
        return {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    except SyntaxError:
        return set()


def check_debug(run, artifact):
    e, c, repo = check_common(run, artifact, "debug")
    base, red, green = (commit_of(repo, e[k], k) for k in ("base_commit", "red_commit", "green_commit"))
    test_file, test_id = c["test_file"], e["test_id"]
    need(isinstance(test_id, str) and re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", test_id), "test id malformed")
    need(isinstance(e["recorded"], dict), "recorded exits malformed")
    reproduce = e["reproduce"]
    need(isinstance(reproduce, dict) and reproduce.get("script") == c["repro_script"]
         and isinstance(reproduce.get("symptom"), str) and reproduce["symptom"], "reproduce record malformed")
    with extracted(repo, base) as tree:
        proc = run_script(tree / c["repro_script"], [], tree)
        need(proc.returncode == 0 and reproduce["symptom"] in proc.stdout, "symptom not reproduced at the base commit")
    with extracted(repo, red) as tree:
        need((tree / test_file).is_file(), "test file missing at the red commit")
        proc = run_script(tree / test_file, [], tree)
        need(proc.returncode != 0, "red commit passes: the new test did not fail")
        method = test_id.split(".")[1]
        failing = re.search(rf"FAIL: {re.escape(method)} \(__main__\.{re.escape(test_id)}\)", proc.stderr)
        need(failing and "AssertionError" in proc.stderr, "red for wrong reason: no AssertionError from the recorded test id")
    red_test, green_test = blob(repo, red, test_file), blob(repo, green, test_file)
    need(red_test is not None and red_test == green_test, "test changed between the red and green commits")
    with extracted(repo, green) as tree:
        proc = run_script(tree / test_file, [], tree)
        need(proc.returncode == 0, "green commit fails the test")
    need(git(repo, "rev-parse", f"{red}^").strip() == base, "red commit is not a child of the base commit")
    need(git(repo, "merge-base", "--is-ancestor", red, green, ok=True).returncode == 0, "green does not descend from red")
    check_scope_imports_reuse(repo, e, base, green)
    check_decisions(e)
    jobs = Jobs(run, artifact["jobs"])
    need(len(jobs.items) == 2, "debug needs exactly the builder and verifier jobs")
    builder, verifier = jobs.items[0][0], jobs.items[1][0]
    need(builder["role"] == "builder", f"expected role builder at position 0, got {builder['role']}")
    need(verifier["role"] == "verifier", f"expected role verifier at position 1, got {verifier['role']}")
    need(builder["job_id"] != verifier["job_id"] and builder["pid"] != verifier["pid"], "verifier is not a fresh job")
    need(builder["worktree"] and verifier["worktree"] and builder["worktree"]["path"] != verifier["worktree"]["path"],
         "verifier did not get its own worktree")
    worktree = rel(run, verifier["worktree"]["path"])
    head = git(worktree, "rev-parse", "HEAD").strip()
    need(head == green, f"verifier worktree HEAD {head[:12]} is not the green commit")
    need(git(worktree, "status", "--porcelain") == "", "verifier worktree dirty")
    said = Jobs.parse(jobs.items[1][1], verifier["job_id"])
    need(said.get("head") == green and said.get("rc") == 0, "verifier output does not report the green commit passing")
    built = Jobs.parse(jobs.items[0][1], builder["job_id"])
    need((built.get("base_commit"), built.get("red_commit"), built.get("green_commit")) == (base, red, green),
         "builder output does not match the artifact commits")


def check_scope_imports_reuse(repo, e, base, green):
    scope, reuse, new_deps = e["scope"], e["reuse"], e["new_dependencies"]
    for name, items in (("scope", scope), ("reuse", reuse), ("new_dependencies", new_deps)):
        need(isinstance(items, list) and all(isinstance(i, str) for i in items), f"{name} must be a list of strings")
    changed = [n for n in git(repo, "diff", "--name-only", base, green).splitlines() if n]
    outside = sorted(set(changed) - set(scope))
    need(not outside, f"touched file outside declared scope: {outside}")
    for name in changed:
        if not name.endswith(".py"):
            continue
        old = blob(repo, base, name)
        added = import_roots(blob(repo, green, name).decode("utf-8", "replace"), name) - (
            import_roots(old.decode("utf-8", "replace"), name) if old else set())
        for root in sorted(added):
            local = blob(repo, green, f"{root}.py") is not None
            need(root in sys.stdlib_module_names or local or root in new_deps, f"undeclared import {root} in {name}")
    diff = git(repo, "diff", base, green)
    added_text = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    for item in reuse:
        match = re.fullmatch(r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)", item)
        need(match, f"reuse name malformed: {item}")
        source = blob(repo, base, f"{match[1]}.py")
        need(source is not None and match[2] in defined_names(source.decode("utf-8", "replace")),
             f"reuse name absent at base: {item}")
        need(re.search(rf"\b{re.escape(match[2])}\b", added_text), f"reuse name unused in diff: {item}")


def check_decisions(e):
    decisions = e["dependency_decisions"]
    need(isinstance(decisions, list) and decisions, "dependency decision missing")
    for d in decisions:
        need(isinstance(d, dict) and d.get("decision") in ("adopted", "rejected") and isinstance(d.get("candidate"), str)
             and isinstance(d.get("reason"), str) and d["reason"].strip(), "dependency decision malformed")
        need(d["decision"] != "adopted" or d["candidate"] in e["new_dependencies"], "adopted dependency not in new_dependencies")


# --- domain -------------------------------------------------------------------------------------------------------

def check_domain(run, artifact):
    e, c, repo = check_common(run, artifact, "domain")
    commit = commit_of(repo, e["commit"], "commit")
    f = e["finding"]
    need(isinstance(f, dict) and {"term", "glossary_ref", "code_ref", "scenario_id", "expected", "observed"} <= set(f),
         "finding malformed")
    need(f["scenario_id"] in c["scenarios"], "unknown scenario id")
    with extracted(repo, commit) as tree:
        replay = script_json(FIXTURES / c["fixtures"] / c["script"], [tree, f["scenario_id"]], tree)
    need(replay.get("contradiction") is True, "no contradiction reproduced")
    need((replay.get("expected"), replay.get("observed")) == (f["expected"], f["observed"]),
         "claimed expected/observed differ from the replay")
    for ref, needle in (("glossary_ref", f["term"]), ("code_ref", f["code_ref"].get("symbol"))):
        item = f[ref]
        need(isinstance(item, dict) and item.get("file") in c["ref_files"] and isinstance(item.get("line"), int)
             and isinstance(needle, str) and needle, f"{ref} malformed")
        lines = text_lines(repo, commit, item["file"])
        ok = 1 <= item["line"] <= len(lines) and needle.lower() in lines[item["line"] - 1].lower()
        need(ok, f"{ref} line {item['line']} does not contain {needle!r}")
    imp = e["improvement"]
    need(isinstance(imp, dict) and isinstance(imp.get("file"), str), "improvement must name exactly one file")
    need(blob(repo, commit, imp["file"]) is not None, "improvement file not in the repo")
    need(imp.get("acceptance_check") == f"{c['script']}:{f['scenario_id']}", "improvement acceptance check missing or unnamed")
    need(isinstance(imp.get("change"), str) and imp["change"].strip(), "improvement change missing")
    maker, refuter = e["maker_job"], e["refuter_job"]
    need(maker != refuter, "refuter is the maker job")
    jobs = Jobs(run, artifact["jobs"])
    _, made = jobs.by_id(maker, "surveyor", "maker")
    need(made.get("finding") == f, "finding differs from the maker job output")
    _, said = jobs.by_id(refuter, "refuter", "refuter")
    need(said.get("finding_sha256") == sha(canon(f)), "refuter verdict bound to a different finding")
    need(said.get("verdict") == "STANDS" and e["verdict"] == "STANDS", "refuter verdict is not STANDS")


# --- secure-build -------------------------------------------------------------------------------------------------

def attack(tree, c, item):
    return script_json(FIXTURES / c["fixtures"] / c["script"], [tree, item["id"], item["input"]], tree).get("outcome")


def check_secure(run, artifact):
    e, c, repo = check_common(run, artifact, "secure-build")
    base, build = commit_of(repo, e["base_commit"], "base_commit"), commit_of(repo, e["build_commit"], "build_commit")
    need(e["threats"] == [{**t, "expected": "refused"} for t in c["threats"]], "threat list differs from the contract")
    need(e["benign"] == {**c["benign"], "expected": "served"}, "benign request differs from the contract")
    with extracted(repo, build) as tree:
        for threat in c["threats"]:
            outcome = attack(tree, c, threat)
            need(outcome == "refused", f"threat {threat['id']} not refused (outcome {outcome})")
        outcome = attack(tree, c, c["benign"])
        need(outcome == "served", f"benign request not served (outcome {outcome})")
    with extracted(repo, base) as tree:
        for threat in c["threats"]:
            need(attack(tree, c, threat) != "refused", f"base already refuses {threat['id']}: nothing was constructed")
    jobs = Jobs(run, artifact["jobs"])
    (_, said), = jobs.by_role("security-reviewer")[:1]
    need(e["review"] == {"commit": build, "verdict": "APPROVE"} and said.get("commit") == build
         and said.get("verdict") == "APPROVE", "review not bound to the build commit")
    (_, built), = jobs.by_role("builder")[:1]
    need((built.get("base_commit"), built.get("build_commit")) == (base, build), "builder output does not match the artifact commits")


# --- security-audit -----------------------------------------------------------------------------------------------

def finding_sha(finding):
    return sha(canon({k: v for k, v in finding.items() if k != "refuter_job"}))


def check_audit(run, artifact):
    e, c, repo = check_common(run, artifact, "security-audit")
    target = commit_of(repo, e["target_commit"], "target_commit")
    audit_map = e["map"]
    need(isinstance(audit_map, dict) and set(audit_map) == {"assets", "trust_boundaries", "inputs"}
         and all(isinstance(v, list) and v for v in audit_map.values()), "map malformed")
    findings = e["findings"]
    need(isinstance(findings, list) and findings, "findings missing")
    with extracted(repo, target) as tree:
        for f in findings:
            need(isinstance(f, dict) and isinstance(f.get("id"), str), "finding malformed")
            fid = f["id"]
            need(isinstance(f.get("attack_scenario"), str) and f["attack_scenario"].strip(),
                 f"finding {fid}: empty attack scenario (no attack scenario, no finding)")
            need(f.get("status") in c["statuses"], f"finding {fid}: unknown status")
            evidence_id = f.get("evidence_id")
            need(evidence_id is not None or f["status"] != "confirmed", f"finding {fid}: confirmed without evidence")
            if evidence_id is None:
                continue
            need(evidence_id in c["evidence"], f"finding {fid}: unknown evidence id")
            replay = script_json(FIXTURES / c["fixtures"] / c["script"], [tree, c["evidence"][evidence_id]], tree)
            seen = replay.get("canary_observed")
            need(isinstance(seen, bool), f"finding {fid}: replay printed no observation")
            if f["status"] == "confirmed":
                need(seen, f"confirmed finding {fid} canary not observed in this replay")
            else:
                need(not seen, f"finding {fid}: status {f['status']} understates a replayed exploit")
            need(f.get("observed") == {"canary_observed": seen}, f"finding {fid}: claimed observation differs from replay")
    jobs = Jobs(run, artifact["jobs"])
    (_, mapped), = jobs.by_role("surveyor")[:1]
    need(mapped.get("map") == audit_map, "map differs from the mapper job output")
    for f in findings:
        need(isinstance(f.get("refuter_job"), str), f"finding {f['id']}: no refuter verdict")
        _, said = jobs.by_id(f["refuter_job"], "refuter", f"finding {f['id']} refuter")
        need(said.get("finding_id") == f["id"] and said.get("finding_sha256") == finding_sha(f),
             f"finding {f['id']}: refuter verdict bound to a different finding")
        need(said.get("verdict") == "STANDS", f"finding {f['id']}: refuter verdict is not STANDS")


# --- incident -----------------------------------------------------------------------------------------------------

def measure(c, state_bytes):
    with tempfile.TemporaryDirectory(prefix="dev-measure-") as tmp:
        path = Path(tmp) / "state.json"
        path.write_bytes(state_bytes)
        value = script_json(FIXTURES / c["fixtures"] / c["script"], [path], Path(tmp)).get(c["metric"]["name"])
    need(isinstance(value, int) and not isinstance(value, bool), "measurement printed no metric")
    return value


def check_incident(run, artifact):
    e, c, repo = check_common(run, artifact, "incident")
    base = commit_of(repo, e["base_commit"], "base_commit")
    metric = c["metric"]
    need(e["metric"] == metric, "metric differs from the contract")
    facts = e["facts"]
    need(isinstance(facts, list) and facts, "fact snapshot missing")
    lines = text_lines(repo, base, c["facts_file"])
    for fact in facts:
        ok = (isinstance(fact, dict) and fact.get("file") == c["facts_file"] and isinstance(fact.get("line"), int)
              and 1 <= fact["line"] <= len(lines) and fact.get("sha256") == sha(lines[fact["line"] - 1]))
        need(ok, f"fact line sha mismatch: {fact}")
    candidates = e["candidates"]
    need(isinstance(candidates, list) and len(candidates) >= c["min_candidates"], f"fewer than {c['min_candidates']} candidates")
    ids = [x.get("id") if isinstance(x, dict) else None for x in candidates]
    need(len(set(ids)) == len(ids) and set(ids) <= set(c["changes"]), "candidate ids malformed")
    for x in candidates:
        need(all(isinstance(x.get(k), str) and x[k].strip() for k in ("prerequisite", "risk", "rollback", "metric")),
             f"candidate {x['id']} lacks prerequisite, risk, rollback or metric")
    need(sorted(e["ranking"]) == sorted(ids), "ranking does not cover the candidates")
    measurements = e["measurements"]
    need(isinstance(measurements, list) and measurements, "no measurements")
    base_state = blob(repo, base, "state.json")
    need(base_state is not None, "state.json missing at base")
    failures = 0
    for m in measurements:
        need(failures < c["max_failures"], "change after two failures: stop and return to fact gathering")
        need(isinstance(m, dict) and m.get("change_id") in ids, "measurement names no candidate")
        failures += check_measurement(repo, c, base, base_state, m)
    need(measurements[0]["change_id"] == e["ranking"][0], "first experiment is not the top-ranked candidate")
    jobs = Jobs(run, artifact["jobs"])
    (_, surveyed), = jobs.by_role("surveyor")[:1]
    need(surveyed.get("facts") == facts, "facts differ from the surveyor output")
    _, ranked = jobs.by_role("tribune")[0]
    need(ranked.get("ranking") == e["ranking"], "ranking differs from the tribune output")
    for m in measurements:
        _, applied = jobs.by_id(m.get("apply_job"), "builder", f"apply job for {m['change_id']}")
        need((applied.get("apply_commit"), applied.get("rollback_commit")) == (m["apply_commit"], m["rollback_commit"]),
             f"apply job output does not match measurement {m['change_id']}")


def check_measurement(repo, c, base, base_state, m):
    """Replay one measurement; return 1 when the change failed to reach the threshold."""
    cid, change = m["change_id"], c["changes"][m["change_id"]]
    apply = commit_of(repo, m.get("apply_commit"), "apply_commit")
    need(git(repo, "rev-parse", f"{apply}^").strip() == base, f"apply commit of {cid} is not a child of base")
    need([n for n in git(repo, "diff", "--name-only", base, apply).splitlines() if n] == ["state.json"],
         f"apply commit of {cid} touches more than state.json")
    before_state, after_state = json.loads(base_state), json.loads(blob(repo, apply, "state.json"))
    changed = {k for k in before_state.keys() | after_state.keys() if before_state.get(k) != after_state.get(k)}
    need(len(changed) <= 1, f"more than one change in measurement {cid}: {sorted(changed)}")
    need(changed == set(change["set"]) and all(after_state.get(k) == v for k, v in change["set"].items()),
         f"apply commit does not make exactly change {cid}")
    recheck = m.get("prerequisite_recheck")
    need(isinstance(recheck, dict) and recheck.get("satisfied") is True and recheck.get("state_sha256") == sha(base_state)
         and all(before_state.get(k) == v for k, v in change["requires"].items()),
         f"prerequisite recheck missing, stale or unmet for {cid}")
    need(m.get("before") == measure(c, base_state), f"before metric differs from the re-measurement for {cid}")
    after = measure(c, blob(repo, apply, "state.json"))
    need(m.get("after") == after, f"after metric differs from the re-measurement for {cid}")
    outcome = "succeeded" if after <= c["metric"]["threshold"] else "failed"
    need(m.get("outcome") == outcome, f"outcome of {cid} is {outcome}")
    if outcome == "succeeded":
        need(m.get("rollback_commit") is None, f"{cid} succeeded but records a rollback")
        return 0
    rollback = commit_of(repo, m.get("rollback_commit"), "rollback_commit")
    need(git(repo, "rev-parse", f"{rollback}^").strip() == apply, f"rollback of {cid} is not a child of its apply commit")
    need(git(repo, "rev-parse", f"{rollback}^{{tree}}").strip() == git(repo, "rev-parse", f"{base}^{{tree}}").strip(),
         f"rollback of {cid} does not restore the base state byte-for-byte")
    return 1


CHECKS = {"debug": check_debug, "domain": check_domain, "secure-build": check_secure,
          "security-audit": check_audit, "incident": check_incident}


def run_check(kind, artifact_path, run):
    valid(kind in CHECKS, f"unknown kind {kind!r}")
    run = run_path(run)
    try:
        artifact_file = Path(os.path.realpath(artifact_path))
        valid(run in artifact_file.parents, "artifact path escapes the run")
        artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise Refused("artifact unreadable or not JSON")
    try:
        CHECKS[kind](run, artifact)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as error:
        raise Refused(f"malformed artifact: {error.__class__.__name__}")


# --- deps ---------------------------------------------------------------------------------------------------------

def licensed_sources():
    return {s["identifier"]: s for s in json.loads(SOURCES.read_text(encoding="utf-8"))["sources"]}


def has_skill_file(folder):
    """A SKILL.md below folder (depth <= 4, no symlinks followed). The file is never read."""
    if not folder.is_dir() or folder.is_symlink():
        return False
    base = len(folder.parts)
    for current, dirs, files in os.walk(folder, followlinks=False):
        if len(Path(current).parts) - base >= 4:
            dirs[:] = []
        if "SKILL.md" in files and not (Path(current) / "SKILL.md").is_symlink():
            return True
    return False


def deps_for(journey, skills_dir=None):
    sources, out = licensed_sources(), []
    for dep in JOURNEYS[journey]["dependencies"]:
        source = sources.get(dep["source"]) if dep["source"] else None
        license_name = source["license"] if source else "unresolved"
        if source is None:
            state, reason = "unavailable", "no licensed upstream source for this slot"
        elif skills_dir is None:
            state, reason = "unavailable", "no skills dir given"
        elif has_skill_file(Path(skills_dir) / dep["id"]):
            state, reason = "present-untested", "SKILL.md present; activation and behaviour untested"
        else:
            state, reason = "unavailable", "not found in the skills dir"
        out.append({"id": dep["id"], "source": dep["source"], "license": license_name, "state": state,
                    "reason": reason, "effect": CONTRACT["dependency_effect"]})
    return out


def cmd_deps(args):
    if args.skills_dir is not None and not Path(args.skills_dir).is_dir():
        raise Refused("skills dir is not a directory")
    report = {"journeys": [{"journey": j, "dependencies": deps_for(j, args.skills_dir)} for j in KINDS]}
    if args.json:
        print(dump(report))
    else:
        for item in report["journeys"]:
            for d in item["dependencies"]:
                print(f"{item['journey']}: {d['id']} [{d['license']}] {d['state']} - {d['reason']} ({d['effect']})")
    return 0


# --- receipt ------------------------------------------------------------------------------------------------------

def verify_receipt(receipt_path, run_arg):
    receipt_file = Path(os.path.realpath(receipt_path))
    run = run_path(run_arg or receipt_file.parent)
    valid(run in receipt_file.parents, "receipt path escapes the run")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise Refused("receipt unreadable or not JSON")
    valid(isinstance(receipt, dict) and set(receipt) == {"version", "kind", "artifacts", "upstream", "self_sha256"}
          and isinstance(receipt["artifacts"], list), "malformed receipt")
    body = {k: v for k, v in receipt.items() if k != "self_sha256"}
    need(receipt["self_sha256"] == sha(canon(body)), "receipt self hash mismatch")
    need(receipt["upstream"] == CONTRACT["upstream_claim"], "receipt upstream must be 'not exercised'")
    entries, loaded = receipt["artifacts"], {}
    for entry in entries:
        valid(isinstance(entry, dict) and set(entry) == {"kind", "journey_id", "path", "sha256", "stable_sha256"},
              "malformed receipt entry")
        path = rel(run, entry["path"])
        valid(path.is_file(), "receipt names a missing artifact")
        data = path.read_bytes()
        need(sha(data) == entry["sha256"], f"artifact sha mismatch: {entry['kind']}")
        try:
            loaded[entry["kind"]] = json.loads(data)
        except ValueError:
            raise Refused(f"artifact is not JSON: {entry['kind']}")
        need(isinstance(loaded[entry["kind"]], dict) and loaded[entry["kind"]].get("kind") == entry["kind"]
             and entry["journey_id"] == entry["kind"], f"kind mismatch: {entry['kind']}")
        need(stable_sha256(loaded[entry["kind"]]) == entry["stable_sha256"], f"stable hash mismatch: {entry['kind']}")
    need(sorted(e["kind"] for e in entries) == sorted(KINDS), "receipt must hold exactly one artifact per journey")
    need(len({e["sha256"] for e in entries}) == len(entries), "two journeys point at one artifact")
    check_disjoint(loaded)
    def one(entry):
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "check", "--kind", entry["kind"],
             str(rel(run, entry["path"])), "--run", str(run)], capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            print(f"{entry['kind']}: {proc.stdout.strip()}", file=sys.stderr)
        return proc.returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(entries) or 1) as pool:
        codes = list(pool.map(one, entries))
    need(1 not in codes, "an artifact check failed")
    valid(not any(codes), "an artifact check was refused")


def check_disjoint(loaded):
    separate = ("secure-build", "security-audit", "incident")
    for index, one in enumerate(separate):
        for other in separate[index + 1:]:
            ids = [{j["job_id"] for j in loaded[k]["jobs"]} for k in (one, other)]
            paths = [{i["path"] for i in loaded[k]["evidence"]["inputs"]} for k in (one, other)]
            shared = (ids[0] & ids[1]) | (paths[0] & paths[1])
            need(not shared, f"shared evidence between {one} and {other}: {sorted(shared)}")


# --- cli ----------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--kind", required=True)
    check.add_argument("artifact")
    check.add_argument("--run", required=True)
    deps = sub.add_parser("deps")
    deps.add_argument("--skills-dir")
    deps.add_argument("--json", action="store_true")
    receipt = sub.add_parser("verify-receipt")
    receipt.add_argument("receipt")
    receipt.add_argument("--run")
    args = parser.parse_args(argv)
    try:
        if args.command == "deps":
            return cmd_deps(args)
        if args.command == "check":
            run_check(args.kind, args.artifact, args.run)
        else:
            verify_receipt(args.receipt, args.run)
    except Failed as error:
        print(json.dumps({"status": "fail", "reason": str(error)}))
        return 1
    except Refused as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    print(json.dumps({"status": "pass", "reason": None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
