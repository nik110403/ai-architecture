#!/usr/bin/env python3
"""Five development journeys through the existing roles, offline, with authored stand-in workers.

Usage: run.py --offline [--out DIR] [--skills-dir DIR]. Read README.md first. `--offline` is required: every seat is
a `local-command` seat running a deterministic fixture worker through `modules/orchestration/dispatch.py run`.
Nothing here starts a model, a harness or a network connection. Each journey ends in a checked artifact; the
module's `check` re-executes the evidence, `verify-receipt` re-checks all five.
"""

import argparse
import concurrent.futures
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.development import development as dev  # noqa: E402

DISPATCH = ROOT / "modules" / "orchestration" / "dispatch.py"
CONTRACT = ROOT / "modules" / "development" / "journeys.json"
WORKERS, FIXTURES = HERE / "workers", HERE / "fixtures"
FINDINGS = [
    ("F1", "E1", "An unauthenticated caller sends a crafted name to search() and reads the hidden row, including the "
                 "secret canary value."),
    ("F2", "E2", "A non-admin caller calls read_hidden() hoping the missing audit-log control lets the hidden row out; "
                 "the role check blocks it."),
]


def refuse(message):
    print(f"refused: {message}", file=sys.stderr)
    return 2


def default_out():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "ai-architecture" / "development"


def earlier_run(out):
    try:
        doc = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get("run"), str) and doc["run"].startswith("runs/")


def prepare_out(out):
    """The output dir, 0700, never a symlink or someone else's; its realpath."""
    out = Path(out).expanduser()
    if out.is_symlink() or os.path.abspath(out) != os.path.realpath(out):
        raise SystemExit(refuse("--out is, or lies below, a symlink"))
    if out.exists():
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SystemExit(refuse("--out is not a directory owned by you"))
        if any(out.iterdir()) and not earlier_run(out):
            raise SystemExit(refuse("--out is not empty and holds no earlier run (no latest.json)"))
        if info.st_mode & 0o077:
            out.chmod(0o700)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(mode=0o700)
    return Path(os.path.realpath(out))


class Ctx:
    def __init__(self, run, skills_dir):
        self.run, self.skills_dir = run, skills_dir
        (run / "home").mkdir()  # the dispatcher's own git calls see this empty HOME, not the user's
        self.env = {**dev.git_env(), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(run / "home"),
                    "TMPDIR": tempfile.gettempdir()}


def seat(script, *args):
    return [sys.executable, "-B", "-I", str(WORKERS / script), *map(str, args)]


def write_config(ctx, journey, seats):
    config = {"seats": {role: {"harness": "local-command", "command": command} for role, command in seats.items()},
              "roles": {role: [role] for role in seats}, "permit": sorted(seats), "allow_write": True,
              "worktree_root": str(ctx.run / "worktrees"), "timeout_seconds": 120}
    (ctx.run / "config").mkdir(exist_ok=True)
    (ctx.run / "config" / f"{journey}.json").write_text(dev.dump(config), encoding="utf-8")


def dispatch(ctx, journey, role, job_id, prompt, workdir):
    """One job through dispatch.py run; returns (job record from the dispatch files, parsed worker stdout)."""
    prompts = ctx.run / "prompts"
    prompts.mkdir(exist_ok=True)
    prompt_file = prompts / f"{job_id}.json"
    prompt_file.write_text(json.dumps(prompt, sort_keys=True), encoding="utf-8")
    argv = [sys.executable, "-B", str(DISPATCH), "run", "--config", str(ctx.run / "config" / f"{journey}.json"),
            "--role", role, "--prompt-file", str(prompt_file), "--run-dir", str(ctx.run / "dispatch"),
            "--job-id", job_id, "--workdir", str(workdir)]
    proc = subprocess.run(argv, env=ctx.env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"dispatch of {job_id} exited {proc.returncode}: {proc.stdout[-400:]}")
    record, stdout = dev.job_record(ctx.run, job_id)
    return record, dev.Jobs.parse(stdout, job_id)


def init_repo(ctx, journey):
    contract = dev.JOURNEYS[journey]
    repo = ctx.run / contract["repo"]
    repo.parent.mkdir(exist_ok=True)
    shutil.copytree(FIXTURES / contract["fixtures"] / "base", repo)
    dev.git(repo, "init", "-q", "-b", "main")
    dev.git(repo, "add", "-A")
    dev.git(repo, "commit", "-q", "-m", "base")
    return repo


def head(repo):
    return dev.git(repo, "rev-parse", "HEAD").strip()


def inputs(ctx, journey):
    contract = dev.JOURNEYS[journey]
    return [{"path": f"{contract['repo']}/{name}",
             "sha256": dev.sha((ctx.run / contract["repo"] / name).read_bytes())} for name in contract["inputs"]]


def artifact(ctx, kind, jobs, evidence):
    evidence = {"repo": dev.JOURNEYS[kind]["repo"], **evidence, "inputs": inputs(ctx, kind)}
    return {"version": 1, "kind": kind, "journey_id": kind, "jobs": jobs, "evidence": evidence,
            "upstream": dev.upstream_for(kind, ctx.skills_dir)}


def build_debug(ctx):
    fixtures, repo = FIXTURES / "bug", init_repo(ctx, "debug")
    write_config(ctx, "debug", {"builder": seat("builder.py", "--journey", "debug", "--fixtures", fixtures),
                                "verifier": seat("verifier.py")})
    built, out = dispatch(ctx, "debug", "builder", "debug-builder",
                          {"task": "reproduce the whitespace slug bug, add a failing test, fix it reusing helpers"}, repo)
    verified, _ = dispatch(ctx, "debug", "verifier", "debug-verifier",
                           {"task": "verify the green commit", "green_commit": out["green_commit"]},
                           ctx.run / built["worktree"]["path"])
    keys = ("base_commit", "red_commit", "green_commit", "test_id", "reproduce", "scope", "reuse", "new_dependencies",
            "dependency_decisions")
    evidence = {k: out[k] for k in keys}
    evidence["recorded"] = {"red_exit": out["red_exit"], "green_exit": out["green_exit"]}
    return artifact(ctx, "debug", [built, verified], evidence)


def build_domain(ctx):
    fixtures, repo = FIXTURES / "domain", init_repo(ctx, "domain")
    write_config(ctx, "domain", {"surveyor": seat("surveyor.py", "--journey", "domain", "--fixtures", fixtures),
                                 "refuter": seat("refuter.py", "--journey", "domain", "--fixtures", fixtures)})
    maker, made = dispatch(ctx, "domain", "surveyor", "domain-surveyor",
                           {"task": "compare the glossary term with the code on one scenario"}, repo)
    finding = made["finding"]
    refuter, said = dispatch(ctx, "domain", "refuter", "domain-refuter",
                             {"task": "try to refute this finding", "finding": finding,
                              "finding_sha256": dev.sha(dev.canon(finding))}, repo)
    evidence = {
        "commit": head(repo), "finding": finding, "verdict": said["verdict"],
        "improvement": {"file": "metrics.py", "change": "set ACTIVE_WINDOW_DAYS to 30 to match the glossary",
                        "acceptance_check": f"check_scenario.py:{finding['scenario_id']}"},
        "maker_job": maker["job_id"], "refuter_job": refuter["job_id"],
    }
    return artifact(ctx, "domain", [maker, refuter], evidence)


def build_secure(ctx):
    contract, fixtures, repo = dev.JOURNEYS["secure-build"], FIXTURES / "secure", init_repo(ctx, "secure-build")
    write_config(ctx, "secure-build", {"builder": seat("builder.py", "--journey", "secure", "--fixtures", fixtures),
                                       "security-reviewer": seat("reviewer.py", "--fixtures", fixtures,
                                                                 "--contract", CONTRACT)})
    built, out = dispatch(ctx, "secure-build", "builder", "secure-builder",
                          {"task": "serve files under a base directory, refusing traversal, absolute paths and symlinks"},
                          repo)
    reviewed, said = dispatch(ctx, "secure-build", "security-reviewer", "secure-reviewer",
                              {"task": "review the guards", "commit": out["build_commit"]}, repo)
    evidence = {
        "base_commit": out["base_commit"], "build_commit": out["build_commit"],
        "threats": [{**t, "expected": "refused"} for t in contract["threats"]],
        "benign": {**contract["benign"], "expected": "served"},
        "review": {"commit": said["commit"], "verdict": said["verdict"]},
    }
    return artifact(ctx, "secure-build", [built, reviewed], evidence)


def build_audit(ctx):
    contract, fixtures, repo = dev.JOURNEYS["security-audit"], FIXTURES / "audit", init_repo(ctx, "security-audit")
    write_config(ctx, "security-audit", {"surveyor": seat("surveyor.py", "--journey", "audit", "--fixtures", fixtures),
                                         "refuter": seat("refuter.py", "--journey", "audit", "--fixtures", fixtures)})
    mapper, mapped = dispatch(ctx, "security-audit", "surveyor", "audit-surveyor",
                              {"task": "map assets, trust boundaries and inputs"}, repo)
    target, jobs, findings = head(repo), [mapper], []
    with dev.extracted(repo, target) as tree:  # the reporter: status comes from a replay, not from a label
        for fid, evidence_id, scenario in FINDINGS:
            case = contract["evidence"][evidence_id]
            seen = dev.script_json(fixtures / contract["script"], [tree, case], tree)["canary_observed"]
            findings.append({"id": fid, "attack_scenario": scenario, "evidence_id": evidence_id,
                             "status": "confirmed" if seen else "hardening", "observed": {"canary_observed": seen}})
    for finding in findings:
        record, said = dispatch(ctx, "security-audit", "refuter", f"audit-refuter-{finding['id']}",
                                {"task": "try to disprove this finding", "finding": finding,
                                 "finding_sha256": dev.finding_sha(finding),
                                 "replay_case": contract["evidence"][finding["evidence_id"]]}, repo)
        finding["refuter_job"] = record["job_id"]
        jobs.append(record)
    return artifact(ctx, "security-audit", jobs,
                    {"target_commit": target, "map": mapped["map"], "findings": findings})


def build_incident(ctx):
    contract, fixtures, repo = dev.JOURNEYS["incident"], FIXTURES / "incident", init_repo(ctx, "incident")
    write_config(ctx, "incident", {
        "surveyor": seat("surveyor.py", "--journey", "incident", "--fixtures", fixtures),
        "tribune": seat("tribune.py"),
        "builder": seat("builder.py", "--journey", "incident", "--fixtures", fixtures, "--contract", CONTRACT)})
    surveyor, surveyed = dispatch(ctx, "incident", "surveyor", "incident-surveyor",
                                  {"task": "collect the latency facts from the log"}, repo)
    candidates = [{"id": cid, "prerequisite": " and ".join(f"{k} == {v!r}" for k, v in spec["requires"].items()),
                   "risk": spec["risk"], "rollback": "restore state.json from the base commit",
                   "metric": contract["metric"]["name"]} for cid, spec in contract["changes"].items()]
    tribune, ranked = dispatch(ctx, "incident", "tribune", "incident-tribune",
                               {"task": "rank the candidates for the first safe experiment", "candidates": candidates},
                               repo)
    base, jobs, measurements, failures = head(repo), [surveyor, tribune], [], 0
    for cid in ranked["ranking"]:
        if failures >= contract["max_failures"]:
            break
        record, out = dispatch(ctx, "incident", "builder", f"incident-apply-{cid}", {"change_id": cid}, repo)
        jobs.append(record)
        keys = ("change_id", "apply_commit", "rollback_commit", "prerequisite_recheck", "before", "after", "outcome")
        measurements.append({**{k: out[k] for k in keys}, "apply_job": record["job_id"]})
        if out["outcome"] == "succeeded":
            break
        failures += 1
    evidence = {"base_commit": base, "metric": contract["metric"], "facts": surveyed["facts"],
                "candidates": candidates, "ranking": ranked["ranking"], "measurements": measurements}
    return artifact(ctx, "incident", jobs, evidence)


BUILDERS = {"debug": build_debug, "domain": build_domain, "secure-build": build_secure,
            "security-audit": build_audit, "incident": build_incident}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--skills-dir")
    args = parser.parse_args(argv)
    if not args.offline:
        return refuse("--offline is required: this example only runs authored fixture workers")
    if shutil.which("git") is None or not os.path.isabs(shutil.which("git")):
        return refuse("git unavailable: dispatch write roles use git worktrees")
    if args.skills_dir is not None and not Path(args.skills_dir).is_dir():
        return refuse("--skills-dir is not a directory")
    out = prepare_out(args.out or default_out())
    run_id = uuid.uuid4().hex[:12]
    run = out / "runs" / run_id
    run.mkdir(parents=True, mode=0o700)
    ctx = Ctx(run, args.skills_dir)
    (run / "artifacts").mkdir()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(dev.KINDS)) as pool:  # journeys are independent
        built = [pool.submit(BUILDERS[kind], ctx) for kind in dev.KINDS]
        arts = [future.result() for future in built]
    entries = []
    for kind, art in zip(dev.KINDS, arts):
        path = run / "artifacts" / f"{kind}.json"
        path.write_text(dev.dump(art), encoding="utf-8")
        entries.append({"kind": kind, "journey_id": kind, "path": f"artifacts/{kind}.json",
                        "sha256": dev.sha(path.read_bytes()), "stable_sha256": dev.stable_sha256(art)})
        up = art["upstream"]
        if up["state"] == "unavailable":
            print(f"DEGRADED: {kind} upstream {up['ids'][0]} unavailable")
        else:
            print(f"UPSTREAM: {kind} {up['ids'][0]} {up['state']} (not exercised)")
    receipt = {"version": 1, "kind": "development-receipt", "artifacts": entries,
               "upstream": dev.CONTRACT["upstream_claim"]}
    receipt["self_sha256"] = dev.sha(dev.canon(receipt))
    (run / "receipt.json").write_text(dev.dump(receipt), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps({"run": f"runs/{run_id}"}) + "\n", encoding="utf-8")
    print(f"OK: 5 artifacts and a receipt in {run}")
    print(f"verify: python3 -B modules/development/development.py verify-receipt {run / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
