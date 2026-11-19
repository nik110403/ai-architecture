#!/usr/bin/env python3
"""Versioned MCP / skill / agent manifest: drift, backup-first apply, rollback, reconciliation. Stdlib only.

Managed targets are JSON key paths in harness config files (MCP servers) and file trees (skills, agents),
always under one explicit `--home`. Status and dry-run are the default; `apply --apply` and
`rollback --apply` are the only writers of targets. Environment entries carry variable NAMES only; a literal
secret value is refused. Exit codes: 0 ok, 1 drift or conflict, 2 invalid input, 3 refused (broken target,
missing tool), 4 write failure (backup kept, targets restored). No network, no model.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SECRET_ARG = re.compile(r"--[^=\s]*(key|token)[^=\s]*=", re.I)
CREDENTIAL_FILES = {".credentials.json", "auth.json", ".env", "id_rsa", "id_ed25519", ".netrc"}
HARNESS = {"claude": (".mcp.json", "mcpServers"), "opencode": ("opencode.json", "mcp")}


class Invalid(Exception):
    """Exit 2: nothing written."""


class Refused(Exception):
    """Exit 3."""


def dup_hook(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key: {key}")
        data[key] = value
    return data


def parse_json(text):
    return json.loads(text, object_pairs_hook=dup_hook)


def vhash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bhash(data):
    return hashlib.sha256(data).hexdigest()


def short(value):
    return value[:12] if value else "-"


# ---- manifest -------------------------------------------------------------------------------
def rel_ok(rel):
    parts = Path(rel).parts
    return isinstance(rel, str) and rel and not Path(rel).is_absolute() and ".." not in parts


def check_entry(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
        raise Invalid("every entry needs a string id")
    kind = entry.get("kind")
    if kind == "tree":
        if set(entry) != {"id", "kind", "dest", "files"} or not rel_ok(entry["dest"]) \
                or not isinstance(entry["files"], dict) or not entry["files"]:
            raise Invalid(f"{entry['id']}: tree entry needs id, kind, dest, files")
        for rel, text in entry["files"].items():
            if not rel_ok(rel) or not isinstance(text, str):
                raise Invalid(f"{entry['id']}: bad file path or content: {rel!r}")
            if Path(rel).name in CREDENTIAL_FILES:
                raise Invalid(f"{entry['id']}: credential-style file refused: {Path(rel).name}")
        return
    if kind != "mcp":
        raise Invalid(f"{entry['id']}: unknown kind {kind!r}")
    allowed = {"id", "kind", "harness", "name", "command", "args", "env", "url", "headers"}
    if set(entry) - allowed or entry.get("harness") not in HARNESS:
        raise Invalid(f"{entry['id']}: mcp entry has unknown fields or harness")
    if not isinstance(entry.get("name"), str) or not SERVER.fullmatch(entry["name"]):
        raise Invalid(f"{entry['id']}: invalid server name")
    if ("command" in entry) == ("url" in entry):
        raise Invalid(f"{entry['id']}: give exactly one of command or url")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Invalid(f"{entry['id']}: args must be a list of strings")
    for arg in args:
        if SECRET_ARG.match(arg):
            raise Invalid(f"{entry['id']}: a literal secret-style argument is refused; pass a variable name in env")
    env = entry.get("env", [])
    if not isinstance(env, list) or not all(isinstance(n, str) and NAME.fullmatch(n) for n in env):
        raise Invalid(f"{entry['id']}: env must be a list of variable NAMES; values are refused")
    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and NAME.fullmatch(v) for k, v in headers.items()):
        raise Invalid(f"{entry['id']}: headers must map a header to an env variable NAME; literal values are refused")
    if "command" in entry and not (isinstance(entry["command"], str) and entry["command"]):
        raise Invalid(f"{entry['id']}: command must be a string")
    if "url" in entry:
        parts = urlsplit(entry["url"]) if isinstance(entry["url"], str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.hostname \
                or parts.username or parts.password or parts.query:
            raise Invalid(f"{entry['id']}: url must be http(s) without userinfo or query")


def validate_manifest(data):
    if not isinstance(data, dict) or not {"schema_version", "manifest_version", "entries"} <= set(data) \
            or set(data) - {"schema_version", "manifest_version", "entries", "reconcile"} \
            or data["schema_version"] != 1 or not isinstance(data["manifest_version"], int) \
            or isinstance(data["manifest_version"], bool) or data["manifest_version"] < 1 \
            or not isinstance(data["entries"], list):
        raise Invalid("manifest needs schema_version 1, manifest_version >= 1 and entries")
    seen = set()
    for entry in data["entries"]:
        check_entry(entry)
        if entry["id"] in seen:
            raise Invalid(f"duplicate entry id: {entry['id']}")
        seen.add(entry["id"])
    return data


def load_manifest(path):
    try:
        return validate_manifest(parse_json(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise Invalid(f"manifest unreadable: {error}")


def render_mcp(entry):
    file, root = HARNESS[entry["harness"]]
    env = {n: ("${%s}" if entry["harness"] == "claude" else "{env:%s}") % n for n in entry.get("env", [])}
    headers = {k: ("${%s}" if entry["harness"] == "claude" else "{env:%s}") % v
               for k, v in entry.get("headers", {}).items()}
    if entry["harness"] == "claude":
        value = {"type": "http", "url": entry["url"]} if "url" in entry else \
            {"command": entry["command"], "args": entry.get("args", [])}
        if headers:
            value["headers"] = headers
        if env:
            value["env"] = env
    else:
        value = {"type": "remote", "url": entry["url"]} if "url" in entry else \
            {"type": "local", "command": [entry["command"], *entry.get("args", [])]}
        if headers:
            value["headers"] = headers
        if env:
            value["environment"] = env
    return file, [root, entry["name"]], value


def build_targets(manifest):
    targets = []
    for entry in manifest["entries"]:
        if entry["kind"] == "mcp":
            file, path, value = render_mcp(entry)
            targets.append({"id": entry["id"], "type": "json", "file": file, "path": path, "desired": value,
                            "akey": f"{file}::{'.'.join(path)}", "label": f"{file}:{'.'.join(path)}"})
        else:
            for rel, text in sorted(entry["files"].items()):
                file = (Path(entry["dest"]) / rel).as_posix()
                targets.append({"id": entry["id"], "type": "file", "file": file, "desired": text.encode(),
                                "akey": f"tree::{file}", "label": file})
    return targets


# ---- filesystem helpers ---------------------------------------------------------------------
def check_home(path):
    home = Path(path)
    if not home.is_dir():
        raise Invalid(f"--home is not a directory: {path}")
    return Path(os.path.realpath(home))


def contain(home, targets):
    for target in targets:
        real = Path(os.path.realpath(home / target["file"]))
        if real != home and home not in real.parents:
            raise Invalid(f"target resolves outside --home: {target['file']}")
        target["real"] = real


def _write_target(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cs-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def state_dir(home):
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "aiarch" / "config-sync" / hashlib.sha256(str(home).encode()).hexdigest()


def private_dir(path):
    missing = []
    for p in [path, *path.parents]:
        if p.exists():
            break
        missing.append(p)
    for p in reversed(missing):
        os.mkdir(p, 0o700)
        os.chmod(p, 0o700)
    return path


def write_state(path, data):
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_state_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# ---- classification -------------------------------------------------------------------------
def get_path(doc, path):
    node = doc
    for key in path:
        if not isinstance(node, dict):
            return "blocked", None
        if key not in node:
            return "absent", None
        node = node[key]
    return "found", node


def set_path(doc, path, value):
    node = doc
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def classify(home, targets, applied):
    """Returns (results, docs). docs: file -> parsed JSON dict or None (absent) for json targets."""
    docs, broken = {}, {}
    for target in targets:
        if target["type"] != "json" or target["file"] in docs:
            continue
        real = target["real"]
        if not real.exists():
            docs[target["file"]] = None
            continue
        try:
            doc = parse_json(real.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("top level is not an object")
            docs[target["file"]] = doc
        except (OSError, ValueError, UnicodeDecodeError):
            docs[target["file"]] = None
            broken[target["file"]] = True
    results = []
    for target in targets:
        record = applied["keys"].get(target["akey"])
        if target["type"] == "json":
            desired = vhash(target["desired"])
            if broken.get(target["file"]):
                status, current = "broken", None
            else:
                where, value = get_path(docs[target["file"]] or {}, target["path"])
                current = vhash(value) if where == "found" else None
                status = decide(where, current, desired, record)
        else:
            desired = bhash(target["desired"])
            real = target["real"]
            if real.is_symlink() or (real.exists() and not real.is_file()):
                status, current = "broken", None
            elif real.exists():
                current = bhash(real.read_bytes())
                status = decide("found", current, desired, record)
            else:
                current, status = None, "missing"
        results.append({**target, "status": status, "current": current, "applied": record,
                        "desired_hash": desired})
    return results, docs


def decide(where, current, desired, record):
    if where == "blocked":
        return "conflict"
    if where == "absent":
        return "missing"
    if current == desired:
        return "ok"
    if record is not None and current == record:
        return "outdated"
    return "conflict"


def show(results):
    for r in results:
        print(f"{r['status']} {r['id']} {r['label']} current={short(r['current'])} "
              f"desired={short(r['desired_hash'])} applied={short(r['applied'])}")


def exit_for(results):
    statuses = {r["status"] for r in results}
    if "broken" in statuses:
        return "broken"
    if "conflict" in statuses:
        return "conflict"
    return "ok"


# ---- opencode agents ------------------------------------------------------------------------
def strip_models(agents):
    return {k: {f: v for f, v in entry.items() if f != "model"} if isinstance(entry, dict) else entry
            for k, entry in agents.items()}


def check_agents(home, agents_dir, sync):
    sync = Path(sync)
    if not sync.is_file():
        raise Refused(f"opencode-agents-sync not found: {sync}")
    result = subprocess.run([sys.executable, "-B", str(sync), "--agents-dir", str(agents_dir)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Refused(f"opencode-agents-sync failed: {result.stderr.strip()}")
    generated = json.loads(result.stdout)["agent"]
    target = Path(os.path.realpath(home / "opencode.json"))
    if home not in target.parents:
        raise Invalid("opencode.json resolves outside --home")
    try:
        current = parse_json(target.read_text(encoding="utf-8")).get("agent")
    except (OSError, ValueError, AttributeError):
        print("broken: opencode.json")
        return False
    if not isinstance(current, dict) or strip_models(current) != strip_models(generated):
        print("drift: opencode agents")
        return False
    print("ok: opencode agents")
    return True


# ---- commands -------------------------------------------------------------------------------
def prepare(args):
    manifest = load_manifest(args.manifest)
    home = check_home(args.home)
    targets = build_targets(manifest)
    contain(home, targets)
    applied = read_state_json(state_dir(home) / "applied.json", {"schema_version": 1, "keys": {}})
    results, docs = classify(home, targets, applied)
    return manifest, home, results, docs, applied


def cmd_status(args):
    manifest, home, results, docs, applied = prepare(args)
    show(results)
    code = 0 if exit_for(results) == "ok" and all(r["status"] == "ok" for r in results) else 1
    if args.agents_dir:
        if not check_agents(home, args.agents_dir, args.opencode_sync):
            code = 1
    return code


def cmd_apply(args):
    manifest, home, results, docs, applied = prepare(args)
    show(results)
    verdict = exit_for(results)
    if verdict == "broken":
        print("refused: a target is broken; fix it by hand, nothing was written")
        return 3
    if verdict == "conflict":
        print("conflict: a managed value differs from the last applied state; nothing was written")
        return 1
    actions = [r for r in results if r["status"] in ("missing", "outdated")]
    new_keys = {r["akey"]: r["desired_hash"] for r in results}
    if not actions:
        print("up to date")
        if args.apply and new_keys != {k: applied["keys"].get(k) for k in new_keys}:
            write_state(state_dir(home) / "applied.json",
                        json.dumps({"schema_version": 1, "keys": {**applied["keys"], **new_keys}}).encode())
        return 0
    if not args.apply:
        print(f"dry-run: {len(actions)} change(s) would be applied; pass --apply")
        return 0
    return do_apply(home, results, docs, actions, applied, new_keys)


def do_apply(home, results, docs, actions, applied, new_keys):
    # new content per touched file
    changes = {}
    for r in actions:
        if r["type"] == "file":
            changes[r["file"]] = (r["real"], r["desired"])
        else:
            doc = changes.get(r["file"])
            doc = doc[1] if doc else copy.deepcopy(docs[r["file"]] or {})
            set_path(doc, r["path"], r["desired"])
            changes[r["file"]] = (r["real"], doc)
    payload = {}
    for rel, (real, content) in changes.items():
        payload[rel] = (real, content if isinstance(content, bytes)
                        else (json.dumps(content, indent=2) + "\n").encode())
    sdir = state_dir(home)
    history = read_state_json(sdir / "history.json", {"snapshots": []})
    existing = [int(p.name) for p in (sdir / "backups").glob("[0-9]*")] if (sdir / "backups").exists() else []
    snap_id = "%04d" % (max(existing, default=0) + 1)
    sdir_snap = sdir / "backups" / snap_id
    files = []
    for i, (rel, (real, data)) in enumerate(sorted(payload.items())):
        entry = {"file": rel, "existed": real.exists(), "backup": None, "after_sha": bhash(data)}
        if real.exists():
            entry["backup"] = f"f{i}"
            write_state(sdir_snap / entry["backup"], real.read_bytes())
        files.append(entry)
    snapshot = {"id": snap_id, "committed": False, "files": files, "prev_applied": applied["keys"]}
    write_state(sdir_snap / "snapshot.json", json.dumps(snapshot).encode())  # backup first, then any write
    written = []
    try:
        for rel, (real, data) in sorted(payload.items()):
            _write_target(real, data)
            written.append(rel)
    except Exception as error:
        for entry in files:
            if entry["file"] in written:
                real = payload[entry["file"]][0]
                if entry["existed"]:
                    _write_target(real, (sdir_snap / entry["backup"]).read_bytes())
                elif real.exists():
                    real.unlink()
        print(f"error: write failed ({type(error).__name__}); targets restored, backup kept in state")
        return 4
    snapshot["committed"] = True
    write_state(sdir_snap / "snapshot.json", json.dumps(snapshot).encode())
    history["snapshots"].append(snap_id)
    write_state(sdir / "history.json", json.dumps(history).encode())
    write_state(sdir / "applied.json",
                json.dumps({"schema_version": 1, "keys": {**applied["keys"], **new_keys}}).encode())
    print(f"applied {len(actions)} change(s); snapshot {snap_id}")
    return 0


def cmd_rollback(args):
    home = check_home(args.home)
    sdir = state_dir(home)
    history = read_state_json(sdir / "history.json", {"snapshots": []})
    if not history["snapshots"]:
        print("nothing to roll back")
        return 0
    snap_id = history["snapshots"][-1]
    snapshot = json.loads((sdir / "backups" / snap_id / "snapshot.json").read_text())
    plan = []
    for entry in snapshot["files"]:
        real = Path(os.path.realpath(home / entry["file"]))
        if real != home and home not in real.parents:
            raise Invalid(f"target resolves outside --home: {entry['file']}")
        current = bhash(real.read_bytes()) if real.exists() else None
        if current != entry["after_sha"]:
            print(f"conflict: {entry['file']} changed since the snapshot; nothing was restored")
            return 1
        plan.append((entry, real))
    for entry, real in plan:
        print(f"{'restore' if entry['existed'] else 'remove'} {entry['file']}")
    if not args.apply:
        print("dry-run: pass --apply to roll back")
        return 0
    for entry, real in plan:
        if entry["existed"]:
            _write_target(real, (sdir / "backups" / snap_id / entry["backup"]).read_bytes())
        else:
            real.unlink()
    history["snapshots"].pop()
    write_state(sdir / "history.json", json.dumps(history).encode())
    write_state(sdir / "applied.json", json.dumps({"schema_version": 1, "keys": snapshot["prev_applied"]}).encode())
    print(f"rolled back snapshot {snap_id}")
    return 0


def cmd_reconcile(args):
    a, b = load_manifest(args.a), load_manifest(args.b)
    ea, eb = {e["id"]: e for e in a["entries"]}, {e["id"]: e for e in b["entries"]}
    differing = sorted(i for i in ea.keys() & eb.keys() if ea[i] != eb[i])
    same_version = a["manifest_version"] == b["manifest_version"]
    if a["manifest_version"] >= b["manifest_version"]:
        high, low = ea, eb
    else:
        high, low = eb, ea
    merged = {**low, **high}
    if same_version:
        merged = {**ea, **eb}  # proposal only: b wins; the diff below is the review surface
    proposed = {"schema_version": 1, "manifest_version": max(a["manifest_version"], b["manifest_version"]),
                "entries": [merged[i] for i in sorted(merged)],
                "reconcile": {"diff": [{"id": i, "a": ea[i], "b": eb[i]} for i in differing]}}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proposed, indent=2) + "\n", encoding="utf-8")
    if same_version and differing:
        print(f"version-conflict: same manifest_version {a['manifest_version']}, entries differ: {', '.join(differing)}")
        print(f"proposal written to {out}; review it, then run apply --manifest on it yourself")
        return 1
    print(f"reconciled; proposal written to {out}; nothing was applied")
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"config_sync: {message}", file=sys.stderr)
        raise SystemExit(2)


def build_parser():
    default_sync = Path(__file__).resolve().parents[2] / "opencode" / "agents" / "opencode-agents-sync"
    p = Parser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    s = sub.add_parser("validate")
    s.add_argument("--manifest", required=True)
    s = sub.add_parser("status")
    s.add_argument("--manifest", required=True)
    s.add_argument("--home", required=True)
    s.add_argument("--agents-dir")
    s.add_argument("--opencode-sync", default=str(default_sync))
    s = sub.add_parser("apply")
    s.add_argument("--manifest", required=True)
    s.add_argument("--home", required=True)
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("rollback")
    s.add_argument("--home", required=True)
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("reconcile")
    s.add_argument("--a", required=True)
    s.add_argument("--b", required=True)
    s.add_argument("--out", required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "validate":
            load_manifest(args.manifest)
            print("ok")
            return 0
        return {"status": cmd_status, "apply": cmd_apply, "rollback": cmd_rollback,
                "reconcile": cmd_reconcile}[args.cmd](args)
    except Invalid as error:
        print(f"config_sync: invalid: {error}", file=sys.stderr)
        return 2
    except Refused as error:
        print(f"config_sync: refused: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
