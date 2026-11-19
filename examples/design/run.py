#!/usr/bin/env python3
"""Design pipeline, offline, with authored stand-in workers.

Usage: run.py --offline [--out DIR] [--record FILE] [--product FILE] [--lane product|polish-only] [--revisions N]
Read README.md first. `--offline` is required: every seat is a `local-command` seat running a deterministic worker through
`modules/orchestration/dispatch.py run`, and the pages are rendered in real headless Chrome on 127.0.0.1. Nothing here starts a
model, a harness or an outside network connection. Exit codes: 0 PASS, 1 FAIL, 2 refused, 3 NOT RUN.
Failure cells: --leave-unfixed CHECK, --delete-element, --canary TEXT, --plant-symlink PATH.
"""
import argparse
import importlib.util
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
DISPATCH = ROOT / "modules" / "orchestration" / "dispatch.py"
WORKERS, FIXTURES = HERE / "workers", HERE / "fixtures"
spec = importlib.util.spec_from_file_location("aiarch_design_main", ROOT / "modules" / "design-pipeline" / "design.py")
design = importlib.util.module_from_spec(spec)
spec.loader.exec_module(design)
render = design.render
SURFACE = "notes"


def refuse(message):
    print(f"refused: {message}", file=sys.stderr)
    return 2


def default_out():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "ai-architecture" / "design"


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
    def __init__(self, run, args):
        self.run, self.args = run, args
        (run / "home").mkdir()
        self.env = {**render.git_env(), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(run / "home"),
                    "TMPDIR": tempfile.gettempdir()}


def seat(script, *args):
    return [sys.executable, "-B", "-I", str(WORKERS / script), *map(str, args)]


def write_config(ctx):
    a = ctx.args
    builder = ["--fixtures", FIXTURES]
    for flag, value in (("--leave-unfixed", a.leave_unfixed), ("--canary", a.canary), ("--plant-symlink", a.plant_symlink)):
        if value:
            builder += [flag, value]
    if a.delete_element:
        builder.append("--delete-element")
    seats = {"design-builder": seat("builder.py", *builder), "design-critic": seat("critic.py")}
    config = {"seats": {role: {"harness": "local-command", "command": command} for role, command in seats.items()},
              "roles": {role: [role] for role in seats}, "permit": sorted(seats), "allow_write": True,
              "worktree_root": str(ctx.run / "worktrees"), "timeout_seconds": 120}
    (ctx.run / "config").mkdir()
    (ctx.run / "config" / "dispatch.json").write_text(design.dump(config), encoding="utf-8")


def dispatch(ctx, folder, role, job_id, prompt, workdir):
    """One job through dispatch.py run; returns (job record, parsed worker stdout)."""
    prompts = ctx.run / "prompts"
    prompts.mkdir(exist_ok=True)
    prompt_file = prompts / f"{job_id}.json"
    prompt_file.write_text(json.dumps(prompt, sort_keys=True), encoding="utf-8")
    argv = [sys.executable, "-B", str(DISPATCH), "run", "--config", str(ctx.run / "config" / "dispatch.json"), "--role", role,
            "--prompt-file", str(prompt_file), "--run-dir", str(ctx.run / folder), "--job-id", job_id, "--workdir", str(workdir)]
    proc = subprocess.run(argv, env=ctx.env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"dispatch of {job_id} exited {proc.returncode}: {proc.stdout[-400:]}")
    record, stdout = design.job_record(ctx.run, folder, job_id)
    return record, design.parse_object(stdout, job_id)


def git(repo, *argv):
    return render.git(repo, *argv).strip()


def init_repo(run):
    repo = run / "repo"
    shutil.copytree(FIXTURES / "base", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def finish(run, receipt, out, run_id):
    receipt = design.seal(receipt)
    (run / "receipt.json").write_text(design.dump(receipt), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps({"run": f"runs/{run_id}"}) + "\n", encoding="utf-8")
    status = receipt["verdict"]["status"]
    final = receipt["rounds"][-1]["render"]
    design.print_rendered(final)
    print(f"verdict: {status}" + "".join(f"\n  {r}" for r in receipt["verdict"]["reasons"]))
    print(f"receipt: runs/{run_id}/receipt.json (under --out)")
    print("verify: python3 -B examples/design/verify_receipt.py <out>/runs/<id>/receipt.json")
    return design.PIPE["exit_codes"][status]


def build(args, ctx, out, run_id):
    run = ctx.run
    pipe = design.Pipeline()
    pipe.enter("route")
    record_doc = design.read_json(args.record, "record")
    product_doc = design.read_json(args.product, "product record") if args.product else None
    record = design.route(args.lane, record_doc, product_doc, run)
    pipe.enter("record")
    shutil.copyfile(args.record, run / "record.json")
    if args.product:
        shutil.copyfile(args.product, run / "product.json")
    names = design.select_adapters(record)
    repo, base = init_repo(run)
    write_config(ctx)
    rounds, critic_prev = [], None
    pipe.enter("build")
    build_prompt = {"stage": "build", "surface": SURFACE, "task": "build the first pass of the notes page for this record"}
    if args.canary:
        build_prompt["builder_notes"] = args.canary
    job, out_json = dispatch(ctx, design.BUILDER_DIR, "design-builder", "build-0", build_prompt, repo)
    commit = out_json["commit"]
    for k in range(args.revisions + 1):
        git(repo, "merge", "--ff-only", commit)
        pipe.enter("render")
        rnd = {"n": k, "commit": commit, "build_job": job}
        rnd["render"] = design.do_render(repo, commit, record, run / "renders" / f"r{k}", names)
        rounds.append(rnd)
        if rnd["render"]["state"] != "ran":
            return None, rounds, base, names
        bundle = run / "bundles" / f"r{k}"
        bundle_sha = design.write_bundle(bundle, run / "renders" / f"r{k}", repo, commit, record, k)
        rnd["bundle"] = {"dir": f"bundles/r{k}", "bundle_sha": bundle_sha}
        pipe.enter("critique")
        cjob, said = dispatch(ctx, design.CRITIC_DIR, "design-critic", f"critique-{k}", design.critic_prompt(bundle),
                              os.path.realpath(bundle))
        rnd["critic"] = {"job": cjob, "findings": said["findings"], "approved": said["approved"],
                         "claims_browser_validation": said["claims_browser_validation"],
                         "findings_sha": design.sha(design.canon(said["findings"]))}
        if k == args.revisions:
            break
        pipe.enter("revise")
        revise = {"stage": "revise", "surface": SURFACE, "findings": said["findings"],
                  "findings_sha": rnd["critic"]["findings_sha"]}
        job, out_json = dispatch(ctx, design.BUILDER_DIR, "design-builder", f"revise-{k + 1}", revise, repo)
        commit = out_json["commit"]
    pipe.enter("receipt")
    return True, rounds, base, names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--record", default=str(FIXTURES / "record.json"))
    parser.add_argument("--product", default=str(FIXTURES / "product.json"))
    parser.add_argument("--lane", default="product")
    parser.add_argument("--revisions", type=int, default=1)
    parser.add_argument("--leave-unfixed")
    parser.add_argument("--delete-element", action="store_true")
    parser.add_argument("--canary")
    parser.add_argument("--plant-symlink")
    args = parser.parse_args(argv)
    if not args.offline:
        return refuse("--offline is required: this example only runs authored fixture workers")
    if args.lane == "polish-only":
        args.product = None
    git_exe = os.environ.get("AIARCH_GIT") or shutil.which("git")
    if not git_exe or not os.path.isabs(git_exe):
        return refuse("git unavailable: dispatch write roles use git worktrees")
    out = prepare_out(args.out or default_out())
    run_id = uuid.uuid4().hex[:12]
    run = out / "runs" / run_id
    run.mkdir(parents=True, mode=0o700)
    ctx = Ctx(run, args)
    try:
        ok, rounds, base, names = build(args, ctx, out, run_id)
    except design.Refused as error:
        return refuse(str(error))
    except design.Failed as error:
        print(f"fail: {error}", file=sys.stderr)
        return 1
    finally:
        render.chrome.close_all()
    final = rounds[-1]
    status, reasons = design.decide(final) if final["render"]["state"] != "ran" or "critic" in final \
        else ("FAIL", ["critic missing"])
    receipt = {"version": 1, "kind": "design-receipt", "lane": args.lane, "surface": SURFACE,
               "record_sha": design.sha((run / "record.json").read_bytes()),
               "product_sha": design.sha((run / "product.json").read_bytes()) if args.product else None,
               "adapters": design.adapter_records(names), "repo": "repo", "base_commit": base, "rounds": rounds,
               "rendered": {"state": final["render"]["state"], "reason": final["render"].get("reason", "")},
               "verdict": {"status": status, "reasons": reasons}}
    return finish(run, receipt, out, run_id)


if __name__ == "__main__":
    sys.exit(main())
