#!/usr/bin/env python3
"""Registry-based commit, follow and approved push for private Git repositories. Stdlib only.

Single committer: one host commits and pushes (`run --apply --host H`); replicas only fast-forward
(`follow --apply`). Status and dry-run are the default; `--apply` is the only write path for `run` and
`follow`. A `public` repository is never pushed by `run`; only `push NAME --approve-head <HEAD>` with a
typed confirmation on a terminal does. Exit codes: 0 ok, 1 stop (diverged, secrets, ...), 2 invalid input,
3 refused (host, remote, approval), 4 forbidden git operation. No network beyond what the registered
remote URL needs; git may use only the file, ssh and https transports (narrowable per repo).
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODES = {"commit", "follow-only", "off"}
VISIBILITIES = {"private", "public"}
HEX40 = re.compile(r"[0-9a-f]{40}")
KEY_BLOCK = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", ".netrc", "credentials.json", ".credentials.json", "auth.json"}
ENV_OK = {".env.example", ".env.sample", ".env.template"}
FORBIDDEN_FLAGS = {"--force", "-f", "--force-with-lease", "--mirror", "--all", "--delete", "--tags", "--hard"}
PROTOCOLS = ("file", "ssh", "https")
IN_PROGRESS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


_allow = [":".join(PROTOCOLS)]


def use_repo(repo):
    """Select the transport allowlist for the repo about to be handled (registry `allow_protocols`)."""
    _allow[0] = repo.get("allow_protocols") or ":".join(PROTOCOLS)


class Invalid(Exception):
    """Exit 2."""


class Refused(Exception):
    """Exit 3: host, remote or approval refusal; nothing happened."""


class Forbidden(SystemExit):
    """Exit 4: a git operation this tool never performs. Raised before anything is spawned."""

    def __init__(self, message):
        super().__init__(4)
        self.message = message


def _forbidden(argv):
    args = list(argv)
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    sub = args[i] if i < len(args) else ""
    rest = args[i + 1:]
    before_paths = args[:args.index("--")] if "--" in args else args
    for arg in before_paths:
        if (arg in FORBIDDEN_FLAGS and not (arg == "--all" and sub == "remote")) or arg.startswith(("--force-with-lease=", "--force=")):
            raise Forbidden(f"forbidden git operation: {arg}")
        if arg.startswith("+"):
            raise Forbidden("forbidden git operation: forced refspec")
    if sub in ("clean", "restore", "rebase", "pull") \
            or (sub == "checkout" and "--" in rest) \
            or (sub == "stash" and "drop" in rest) \
            or (sub == "branch" and "-D" in rest):
        raise Forbidden(f"forbidden git operation: {sub}")


def _git(argv, cwd=None):
    """The only place git is spawned. Resolves git on PATH; refuses destructive operations first."""
    _forbidden(argv)
    binary = shutil.which("git")
    if binary is None:
        raise Invalid("git not found on PATH")
    # Every caller-supplied GIT_* key is dropped (GIT_CONFIG_*, GIT_DIR, GIT_SSH*, GIT_EXEC_PATH, ...).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": _allow[0],
                "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    cmd = [binary] + (["-C", str(cwd)] if cwd is not None else []) + list(argv)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)


def out(path, *argv):
    result = _git(list(argv), cwd=path)
    return result.stdout.strip() if result.returncode == 0 else None


# ---- registry -------------------------------------------------------------------------------
def dup_hook(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key: {key}")
        data[key] = value
    return data


def load_registry(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=dup_hook)
    except (OSError, ValueError) as error:
        raise Invalid(f"registry unreadable: {error}")
    if not isinstance(data, dict) or set(data) != {"schema_version", "committer_host", "repos"} \
            or data["schema_version"] != 1 or not isinstance(data["repos"], list) \
            or not isinstance(data["committer_host"], str) or not data["committer_host"]:
        raise Invalid("registry needs schema_version 1, committer_host and repos")
    names = set()
    for repo in data["repos"]:
        allowed = {"name", "path", "mode", "visibility", "remote", "remote_name", "allow_protocols"}
        if not isinstance(repo, dict) or set(repo) - allowed or not {"path", "mode", "visibility", "remote"} <= set(repo):
            raise Invalid("repo needs path, mode, visibility (required) and remote")
        if not isinstance(repo["path"], str) or not repo["path"] or not isinstance(repo["remote"], str):
            raise Invalid("repo path and remote must be strings")
        if repo["mode"] not in MODES:
            raise Invalid(f"unknown mode: {repo['mode']!r}")
        if repo["visibility"] not in VISIBILITIES:
            raise Invalid(f"visibility must be private or public: {repo['visibility']!r}")
        if repo["mode"] != "off" and not repo["remote"]:
            raise Invalid("an active repo needs a remote")
        if "allow_protocols" in repo:
            given = repo["allow_protocols"]
            parts = given.split(":") if isinstance(given, str) else None
            if not parts or any(x not in PROTOCOLS for x in parts) or len(set(parts)) != len(parts):
                raise Invalid(f"allow_protocols must be colon-joined values from {':'.join(PROTOCOLS)}")
        repo.setdefault("name", Path(repo["path"]).name)
        repo.setdefault("remote_name", "origin")
        if repo["name"] in names:
            raise Invalid(f"duplicate repo name: {repo['name']}")
        names.add(repo["name"])
    return data


def pick(data, name):
    repos = [r for r in data["repos"] if name is None or r["name"] == name]
    if name is not None and not repos:
        raise Invalid(f"unknown repo: {name}")
    return repos


# ---- inspection -----------------------------------------------------------------------------
def changed_paths(path):
    raw = _git(["status", "--porcelain=v1", "-z", "-uall"], cwd=path).stdout
    tokens, paths, i = raw.split("\0"), [], 0
    while i < len(tokens) and tokens[i]:
        entry = tokens[i]
        paths.append(entry[3:])
        i += 2 if entry[0] in "RC" else 1
    return paths


def secret_in(path, rel):
    name = Path(rel).name
    if name in SECRET_NAMES or (name.startswith(".env.") and name not in ENV_OK) \
            or name.endswith((".pem", ".key")):
        return f"{rel}: secret-looking file name"
    target = Path(path) / rel
    if target.is_file() and not target.is_symlink():
        data = target.read_bytes()[: 1 << 20]
        if KEY_BLOCK.search(data):
            return f"{rel}: private key block"
    return None


def has_markers(path, rel):
    target = Path(path) / rel
    if not target.is_file() or target.is_symlink():
        return False
    text = target.read_bytes()[: 1 << 20].decode("utf-8", "replace")
    return bool(re.search(r"^<<<<<<< ", text, re.M) and re.search(r"^>>>>>>> ", text, re.M))


def in_progress(path):
    for marker in IN_PROGRESS:
        found = out(path, "rev-parse", "--git-path", marker)
        if found and (Path(path) / found).exists():
            return marker
    return None


def ancestor(path, a, b):
    return _git(["merge-base", "--is-ancestor", a, b], cwd=path).returncode == 0


def relation(path, remote, branch, head, apply):
    """local-vs-remote: in-sync|ahead|new-branch|behind|diverged|rewritten|unreachable. ls-remote only
    unless `apply` (then a non-forced fetch into refs/remotes)."""
    listed = _git(["ls-remote", remote, f"refs/heads/{branch}"], cwd=path)
    if listed.returncode != 0:
        return "unreachable", None
    remote_head = listed.stdout.split()[0] if listed.stdout.strip() else None
    if remote_head is None:
        return "new-branch", None
    if remote_head == head:
        return "in-sync", remote_head
    known = _git(["cat-file", "-e", f"{remote_head}^{{commit}}"], cwd=path).returncode == 0
    if not known:
        if not apply:
            return "behind", remote_head
        fetched = _git(["fetch", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}"], cwd=path)
        if fetched.returncode != 0:
            return "rewritten", remote_head
    if ancestor(path, remote_head, head):
        return "ahead", remote_head
    if ancestor(path, head, remote_head):
        return "behind", remote_head
    return "diverged", remote_head


def check_remote(path, repo, label=""):
    """The registry URL must be the one and only fetch AND push URL of the remote."""
    for flags in ([], ["--push"]):
        listed = _git(["remote", "get-url", *flags, "--all", repo["remote_name"]], cwd=path)
        if listed.returncode != 0 or listed.stdout.splitlines() != [repo["remote"]]:
            raise Refused(f"{label}remote mismatch (git remote get-url differs from the registry)")


def inspect(repo, apply):
    """Gather state; `stops` is a list of reasons to do nothing."""
    path = Path(repo["path"])
    info = {"name": repo["name"], "stops": [], "changes": [], "relation": None, "branch": None, "head": None}
    if not (path / ".git").exists() or out(path, "rev-parse", "--git-dir") is None:
        info["stops"].append("not a git repository")
        return info
    use_repo(repo)
    check_remote(path, repo, f"{repo['name']}: ")
    marker = in_progress(path)
    if marker:
        info["stops"].append(f"{marker}: merge/rebase/cherry-pick in progress")
    branch = out(path, "symbolic-ref", "-q", "--short", "HEAD")
    if branch is None:
        info["stops"].append("detached HEAD")
    info["branch"] = branch
    head = out(path, "rev-parse", "HEAD")
    if head is None:
        info["stops"].append("no commits yet")
    info["head"] = head
    if out(path, "ls-files", "-u"):
        info["stops"].append("unmerged paths (conflict)")
    info["changes"] = changed_paths(path)
    for rel in info["changes"]:
        if has_markers(path, rel):
            info["stops"].append(f"conflict markers in {rel}")
        problem = secret_in(path, rel)
        if problem:
            info["stops"].append(f"secret-looking content, not committed: {problem}")
    if branch and head:
        info["relation"], info["remote_head"] = relation(path, repo["remote_name"], branch, head, apply)
        rel = info["relation"]
        if rel == "behind" and info["changes"]:
            rel = info["relation"] = "diverged"
        if rel == "unreachable":
            info["stops"].append("remote unreachable")
        elif rel in ("behind", "diverged", "rewritten"):
            info["stops"].append(f"{rel}: remote has commits this clone has not integrated; "
                                 "integrate by hand, nothing was changed")
    return info


# ---- commands -------------------------------------------------------------------------------
def cmd_status(args):
    code = 0
    for repo in pick(load_registry(args.registry), args.name):
        if repo["mode"] == "off":
            print(f"{repo['name']}: off")
            continue
        try:
            info = inspect(repo, False)
        except Refused as error:
            print(error)
            code = max(code, 3)
            continue
        print(f"{repo['name']}: mode={repo['mode']} visibility={repo['visibility']} branch={info['branch']} "
              f"dirty={len(info['changes'])} remote={info['relation']}")
        for stop in info["stops"]:
            print(f"  stop: {stop}")
        code = max(code, 1 if info["stops"] else 0)
    return code


def cmd_run(args):
    data = load_registry(args.registry)
    if args.host != data["committer_host"]:
        raise Refused(f"host mismatch: committer_host is {data['committer_host']!r}, got {args.host!r}")
    code = 0
    for repo in pick(data, args.name):
        if repo["mode"] != "commit":
            print(f"{repo['name']}: skipped (mode {repo['mode']})")
            continue
        try:
            code = max(code, run_repo(repo, args.apply))
        except Refused as error:
            print(error)
            code = max(code, 3)
    return code


def run_repo(repo, apply):
    path, name = Path(repo["path"]), repo["name"]
    use_repo(repo)
    info = inspect(repo, apply)
    if info["stops"]:
        for stop in info["stops"]:
            print(f"{name}: stop: {stop}")
        return 1
    changes = info["changes"]
    needs_push = info["relation"] in ("ahead", "new-branch")
    if not changes and not needs_push:
        print(f"{name}: clean, in sync")
        return 0
    if not apply:
        if changes:
            print(f"{name}: dry-run: would commit {len(changes)} files")
        if repo["visibility"] == "public":
            print(f"{name}: public: push requires --approve-head")
        elif changes or needs_push:
            print(f"{name}: dry-run: would push {info['branch']}")
        return 0
    if changes:
        for i in range(0, len(changes), 100):
            if _git(["add", "--"] + changes[i:i + 100], cwd=path).returncode != 0:
                print(f"{name}: stop: git add failed")
                return 1
        message = f"autopilot: {len(changes)} files"
        done = _git(["commit", "-q", "-m", message], cwd=path)
        if done.returncode != 0:
            print(f"{name}: stop: git commit failed: {done.stderr.strip()}")
            return 1
        print(f"{name}: committed {len(changes)} files")
    if repo["visibility"] == "public":
        print(f"{name}: public: push requires --approve-head")
        return 0
    pushed = _git(["push", repo["remote_name"], info["branch"]], cwd=path)
    if pushed.returncode != 0:
        print(f"{name}: stop: push rejected: {pushed.stderr.strip()}")
        return 1
    print(f"{name}: pushed {info['branch']}")
    return 0


def cmd_follow(args):
    data = load_registry(args.registry)
    code = 0
    for repo in pick(data, args.name):
        eligible = repo["mode"] == "follow-only" or (
            repo["mode"] == "commit" and args.host and args.host != data["committer_host"])
        if not eligible:
            print(f"{repo['name']}: skipped (not a follower here)")
            continue
        try:
            code = max(code, follow_repo(repo, args.apply))
        except Refused as error:
            print(error)
            code = max(code, 3)
    return code


def follow_repo(repo, apply):
    path, name, remote = Path(repo["path"]), repo["name"], repo["remote_name"]
    info = inspect_follow(repo, apply)
    if info["stops"]:
        for stop in info["stops"]:
            print(f"{name}: stop: {stop}")
        return 1
    rel = info["relation"]
    if rel == "in-sync":
        print(f"{name}: up to date")
        return 0
    if rel in ("ahead", "new-branch"):
        print(f"{name}: stop: unpushed local commits; nothing changed")
        return 1
    if not apply:
        print(f"{name}: dry-run: would fetch and fast-forward {info['branch']}")
        return 0
    tracking = f"refs/remotes/{remote}/{info['branch']}"
    incoming = set((out(path, "diff", "--name-only", info["head"], tracking) or "").splitlines())
    overlap = sorted(incoming & set(info["changes"]))
    if overlap:
        print(f"{name}: stop: local changes overlap incoming files: {', '.join(overlap)}")
        return 1
    merged = _git(["merge", "--ff-only", tracking], cwd=path)
    if merged.returncode != 0:
        print(f"{name}: stop: fast-forward failed: {merged.stderr.strip()}")
        return 1
    print(f"{name}: fast-forwarded {info['branch']} to {out(path, 'rev-parse', '--short', 'HEAD')}")
    return 0


def inspect_follow(repo, apply):
    """Like inspect, but dirty files are not stops by themselves and `behind` is the goal state."""
    info = inspect(repo, apply)
    keep = []
    for stop in info["stops"]:
        if stop.startswith(("diverged", "behind")):
            continue
        if stop.startswith(("secret-looking", )):
            continue
        keep.append(stop)
    info["stops"] = keep
    if info["relation"] == "diverged" and info["changes"]:
        # inspect() promotes behind+dirty to diverged; a follower sees it as behind.
        path = Path(repo["path"])
        remote_head = info.get("remote_head")
        if remote_head and _git(["cat-file", "-e", f"{remote_head}^{{commit}}"], cwd=path).returncode == 0:
            info["relation"] = "behind" if ancestor(path, info["head"], remote_head) else "diverged"
        else:
            info["relation"] = "behind"
    if info["relation"] in ("diverged", "rewritten"):
        info["stops"].append(f"{info['relation']}: local and remote histories differ; nothing changed")
    return info


def cmd_push(args):
    data = load_registry(args.registry)
    repo = pick(data, args.name)[0]
    path = Path(repo["path"])
    use_repo(repo)
    check_remote(path, repo)
    head = out(path, "rev-parse", "HEAD")
    branch = out(path, "symbolic-ref", "-q", "--short", "HEAD")
    if not args.approve_head or not HEX40.fullmatch(args.approve_head) or args.approve_head != head:
        raise Refused("push requires --approve-head equal to the current HEAD (40 hex)")
    if branch is None:
        raise Refused("detached HEAD")
    try:
        reader, writer = open("/dev/tty", "r"), open("/dev/tty", "w")
    except OSError:
        raise Refused("approval requires an interactive terminal")
    with reader, writer:
        writer.write(f"Type the repo name ({repo['name']}) to push {branch} at {head[:12]}: ")
        writer.flush()
        typed = reader.readline().strip()
    if typed != repo["name"]:
        raise Refused("typed confirmation does not match the repo name")
    if out(path, "rev-parse", "HEAD") != head:
        raise Refused("HEAD moved while waiting for approval; nothing pushed")
    pushed = _git(["push", repo["remote_name"], f"{head}:refs/heads/{branch}"], cwd=path)
    if pushed.returncode != 0:
        print(f"stop: push rejected: {pushed.stderr.strip()}")
        return 1
    listed = _git(["ls-remote", repo["remote_name"], f"refs/heads/{branch}"], cwd=path).stdout.split()
    print(f"{repo['name']}: pushed {branch} at {listed[0] if listed else 'unknown'}")
    return 0


def cmd_import(args):
    spec = importlib.util.spec_from_file_location(
        "steward_u14", Path(__file__).resolve().parents[1] / "stewardship" / "steward.py")
    if not Path(spec.origin).is_file():
        raise Invalid("modules/stewardship/steward.py not found next to this module")
    steward = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(steward)
    try:
        registry = steward.load_registry(args.from_steward, must_exist=True)
    except steward.Refused as error:
        raise Invalid(str(error))
    repos = [{"name": Path(p["root"]).name, "path": p["root"], "mode": "off", "visibility": "public",
              "remote": ""} for p in registry["projects"]]
    text = json.dumps({"schema_version": 1, "committer_host": args.host, "repos": repos}, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


def cmd_unit(args):
    data = load_registry(args.registry)
    if args.host != data["committer_host"]:
        raise Refused("host mismatch")
    exe = f"{sys.executable} {Path(__file__).resolve()} run --apply --registry {Path(args.registry).resolve()} --host {args.host}"
    print(f"# git-automation.service\n[Service]\nType=oneshot\nExecStart={exe}\n")
    print("# git-automation.timer\n[Timer]\nOnCalendar=*:0/10\nPersistent=true\n\n[Install]\nWantedBy=timers.target")
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"git_auto: {message}", file=sys.stderr)
        raise SystemExit(2)


def build_parser():
    p = Parser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    s = sub.add_parser("validate")
    s.add_argument("--registry", required=True)
    for cmd in ("status", "run", "follow", "unit"):
        s = sub.add_parser(cmd)
        s.add_argument("name", nargs="?")
        s.add_argument("--registry", required=True)
        if cmd in ("run", "unit"):
            s.add_argument("--host", required=True)
        if cmd == "follow":
            s.add_argument("--host")
        if cmd in ("run", "follow"):
            s.add_argument("--apply", action="store_true")
    s = sub.add_parser("push")
    s.add_argument("name")
    s.add_argument("--registry", required=True)
    s.add_argument("--approve-head")
    s = sub.add_parser("import")
    s.add_argument("--from-steward", required=True)
    s.add_argument("--host", required=True)
    s.add_argument("--out")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "validate":
            load_registry(args.registry)
            print("ok")
            return 0
        return {"status": cmd_status, "run": cmd_run, "follow": cmd_follow, "push": cmd_push,
                "import": cmd_import, "unit": cmd_unit}[args.cmd](args)
    except Invalid as error:
        print(f"git_auto: invalid: {error}", file=sys.stderr)
        return 2
    except Refused as error:
        print(f"git_auto: refused: {error}", file=sys.stderr)
        print(f"refused: {error}")
        return 3
    except Forbidden as error:
        print(f"git_auto: {error.message}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
