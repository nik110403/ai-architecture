#!/usr/bin/env python3
"""Project bootstrap, registration, verification, resurvey and lint. Stdlib only.

Every command takes explicit roots and an explicit registry file. Nothing here
discovers projects, touches the network or starts a model. `lint` is read-only.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLANCHECK = HERE.parents[1] / "tools" / "plancheck" / "plancheck"
SCAFFOLD = HERE / "scaffold"
FILES = ("TRACKER.md", "SPEC.md", "STRATEGY.md")
INSTRUCTIONS = ("AGENTS.md", "CLAUDE.md")
TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FIELD = re.compile(r"([a-z][a-z-]*): (\S.*)")


class Refused(Exception):
    """Controlled refusal: exit 2, nothing written."""


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_registry(path, must_exist):
    path = Path(path)
    if not path.exists():
        if must_exist:
            raise Refused(f"registry not found: {path}")
        return {"schema_version": 1, "projects": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=duplicate_keys)
    except (OSError, ValueError) as error:
        raise Refused(f"registry unreadable: {error}")
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "projects"}
        or data["schema_version"] != 1
        or not isinstance(data["projects"], list)
        or not all(isinstance(p, dict) and isinstance(p.get("root"), str) for p in data["projects"])
    ):
        raise Refused("registry has an unknown shape or schema_version")
    return data


def save_registry(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".registry-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def instructions_file(root):
    for name in INSTRUCTIONS:
        if (root / name).is_file():
            return root / name
    return None


def check_project(project):
    raw = Path(project)
    if raw.is_symlink():
        raise Refused(f"project is a symlink: {raw}")
    if not raw.is_dir():
        raise Refused(f"project is not a directory: {raw}")
    root = raw.resolve()
    inst = instructions_file(root)
    if inst is None:
        raise Refused("project has no AGENTS.md or CLAUDE.md")
    return root, inst


def check_topic(topic):
    if not TOPIC.fullmatch(topic) or ".." in topic:
        raise Refused(f"invalid topic: {topic}")
    return topic


def scaffold_paths(topic):
    return [f"plans/{topic}/{name}" for name in FILES]


def entry_for(root, inst, topic):
    present = [rel for rel in scaffold_paths(topic) if (root / rel).is_file()]
    return {
        "id": root.name,
        "root": str(root),
        "instructions_sha256": sha256(inst),
        "scaffold": present,
    }


def upsert(registry_path, entry):
    data = load_registry(registry_path, must_exist=False)
    data["projects"] = [p for p in data["projects"] if p["root"] != entry["root"]] + [entry]
    save_registry(registry_path, data)


def cmd_register(args):
    root, inst = check_project(args.project)
    entry = entry_for(root, inst, check_topic(args.topic))
    upsert(args.registry, entry)
    print(f"registered {root}")
    return 0


def cmd_bootstrap(args):
    root, inst = check_project(args.project)
    topic = check_topic(args.topic)
    for directory in (root / "plans", root / "plans" / topic):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise Refused(f"refusing symlinked or non-directory path: {directory}")
    for rel in scaffold_paths(topic):
        if (root / rel).is_symlink():
            raise Refused(f"refusing symlinked target: {rel}")
    (root / "plans" / topic).mkdir(parents=True, exist_ok=True)
    for rel in scaffold_paths(topic):
        target = root / rel
        try:
            with open(target, "x", encoding="utf-8") as handle:
                handle.write((SCAFFOLD / target.name).read_text(encoding="utf-8"))
            print(f"created {rel}")
        except FileExistsError:
            print(f"kept {rel}")
    upsert(args.registry, entry_for(root, inst, topic))
    print(f"registered {root}")
    return 0


def run_plancheck(root, pack):
    """Return (blocking lines, error or None) for one pack directory."""
    env = {k: v for k, v in os.environ.items() if k != "PLANCHECK_DEFAULT_ROOT"}
    try:
        done = subprocess.run(
            ["bash", str(PLANCHECK), "--dir", str(pack)],
            cwd=root, capture_output=True, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"plancheck did not run: {error}"
    if "1 packs swept" not in done.stdout:
        return [], "plancheck output unrecognised"
    return [l[2:].strip() for l in done.stdout.splitlines() if l.startswith("- ⛔ ")], None


def plan_findings(root):
    out = []
    plans = root / "plans"
    if not plans.is_dir():
        return out
    for pack in sorted(p for p in plans.iterdir() if p.is_dir() and (p / "TRACKER.md").is_file()):
        lines, error = run_plancheck(root, pack)
        if error:
            out.append(("plan", pack, error))
        out.extend(("plan", pack, line) for line in lines)
    return out


def verify_reasons(root, entry):
    reasons = []
    inst = instructions_file(root)
    if inst is None:
        reasons.append("instructions file missing")
    packs = set()
    for rel in entry["scaffold"]:
        if not (root / rel).is_file():
            reasons.append(f"missing {rel}")
        packs.add(Path(rel).parent)
    for pack in sorted(packs):
        if (root / pack / "TRACKER.md").is_file():
            lines, error = run_plancheck(root, root / pack)
            reasons += [f"plancheck {pack}: {l}" for l in lines]
            if error:
                reasons.append(f"plancheck {pack}: {error}")
    return reasons


def cmd_verify(args):
    root = Path(args.project).resolve()
    data = load_registry(args.registry, must_exist=False)
    entry = next((p for p in data["projects"] if p["root"] == str(root)), None)
    if entry is None:
        print(f"not registered: {root}")
        return 1
    reasons = verify_reasons(root, entry)
    for reason in reasons:
        print(reason)
    if not reasons:
        print(f"ok {root}")
    return 1 if reasons else 0


def survey(entry):
    root = Path(entry["root"])
    inst = instructions_file(root) if root.is_dir() else None
    if inst is None:
        return "missing"
    if sha256(inst) != entry["instructions_sha256"]:
        return "changed"
    if any(not (root / rel).is_file() for rel in entry.get("scaffold", [])):
        return "scaffold-missing"
    return "ok"


def cmd_resurvey(args):
    data = load_registry(args.registry, must_exist=True)
    entries = [
        {"id": p.get("id", ""), "root": p["root"], "status": survey(p)} for p in data["projects"]
    ]
    if args.format == "json":
        print(json.dumps({"schema_version": 1, "entries": entries}, indent=2))
    else:
        for e in entries:
            print(f"{e['status']} {e['root']}")
    return 0 if all(e["status"] == "ok" for e in entries) else 1


def agent_problem(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return "missing or unterminated frontmatter"
    values = {}
    for line in text[4:].split("\n---\n", 1)[0].splitlines():
        match = FIELD.fullmatch(line)
        if not match:
            return "unsupported or empty frontmatter line"
        key, value = match.groups()
        if key in values:
            return f"duplicate frontmatter field {key}"
        if not value.strip():
            return f"empty frontmatter field {key}"
        values[key] = value
    if not {"name", "description"} <= values.keys() or values.keys() - {"name", "description", "tools"}:
        return "frontmatter fields mismatch"
    if values["name"] != path.stem:
        return "name differs from file name"
    return None


def static_findings(root):
    out = []
    for inst in (root / n for n in INSTRUCTIONS):
        if not inst.is_file():
            continue
        fenced = False
        for line in inst.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
            if fenced:
                continue
            for target in LINK.findall(line):
                local = target.split("#", 1)[0]
                if not local or "://" in local or local.startswith(("/", "mailto:")):
                    continue
                if not (inst.parent / local).exists():
                    out.append(("instructions-dead-link", inst, f"dead link {target}"))
    if instructions_file(root) is None:
        out.append(("instructions-missing", root, "no AGENTS.md or CLAUDE.md"))
    for sub in ("agents", ".claude/agents"):
        for agent in sorted((root / sub).glob("*.md")):
            problem = agent_problem(agent) if agent.is_file() else None
            if problem:
                out.append(("agent-frontmatter", agent, problem))
    for flow in sorted(p for pat in ("*.yml", "*.yaml") for p in (root / ".github/workflows").glob(pat)):
        text = flow.read_text(encoding="utf-8", errors="replace")
        for key, pattern in (("on", r"^(on|\"on\"|'on')\s*:"), ("jobs", r"^jobs\s*:")):
            if not re.search(pattern, text, re.M):
                out.append(("workflow-shape", flow, f"missing top-level {key}:"))
    return out


def cmd_lint(args):
    findings = []
    for raw in args.root:
        root = Path(raw)
        if not root.is_dir():
            raise Refused(f"root is not a directory: {raw}")
        root = root.resolve()
        for code, path, message in static_findings(root) + plan_findings(root):
            findings.append({"code": code, "path": str(path), "root": str(root), "message": message})
    if args.format == "json":
        print(json.dumps({"schema_version": 1, "findings": findings}, indent=2))
    else:
        for f in findings:
            print(f"{f['code']} {f['path']}: {f['message']}")
        if not findings:
            print("lint: 0 findings")
    return 1 if findings else 0


def parser():
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    for name, func in (("bootstrap", cmd_bootstrap), ("register", cmd_register), ("verify", cmd_verify)):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--registry", required=True)
        if name != "verify":
            p.add_argument("--topic", default="main")
        p.set_defaults(func=func)
    p = sub.add_parser("resurvey")
    p.add_argument("--registry", required=True)
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(func=cmd_resurvey)
    p = sub.add_parser("lint")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(func=cmd_lint)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except Refused as error:
        print(f"steward: refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
