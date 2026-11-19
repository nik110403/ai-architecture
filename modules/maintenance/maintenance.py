#!/usr/bin/env python3
"""Loop registry, health, scheduler adapters, changelog and retirement. Stdlib only.

Read-only by default. `scheduler apply`, `scheduler rollback`, `retire --apply` and `changelog --out`
are the only writers; each writes only a path you name. Exit codes: 0 ok, 1 violation or drift,
2 invalid input or usage, 3 unsupported or refused. It never starts a model, uses the network, or
touches a scheduler outside the files you pass.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KINDS = {"timer", "service", "hook", "file", "none"}
STATUSES = {"live", "dead", "retired"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:-]*\.(timer|service)")
MANIFEST = ".aiarch-manifest.json"
NOT_OBSERVABLE = "not-observable: no systemd user manager"
# systemctl verbs this tool may ever send; `enable` additionally needs an explicit flag.
SYSTEMCTL_VERBS = {"show-environment", "show", "list-timers", "daemon-reload", "enable"}
RUN_ARGV = []


class Invalid(Exception):
    """Controlled refusal: exit 2."""


class Unsupported(Exception):
    """Capability missing on this host: exit 3, nothing written."""


def dup_hook(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=dup_hook)
    except (OSError, ValueError) as error:
        raise Invalid(f"unreadable JSON {path}: {error}")


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dump(data):
    return json.dumps(data, indent=2) + "\n"


# ---- registry -------------------------------------------------------------------------------
def validate_registry(data):
    if not isinstance(data, dict) or set(data) != {"schema_version", "loops"} or data["schema_version"] != 1:
        raise Invalid("registry must be {schema_version: 1, loops: [...]}")
    if not isinstance(data["loops"], list):
        raise Invalid("loops must be a list")
    seen = set()
    for loop in data["loops"]:
        if not isinstance(loop, dict) or not {"id", "hosts", "checks", "status"} <= set(loop) \
                or set(loop) - {"id", "hosts", "checks", "status", "retired"}:
            raise Invalid("loop entry has missing or unknown fields")
        if not isinstance(loop["id"], str) or not ID.fullmatch(loop["id"]) or loop["id"] in seen:
            raise Invalid(f"invalid or duplicate loop id: {loop['id']!r}")
        seen.add(loop["id"])
        if loop["status"] not in STATUSES:
            raise Invalid(f"{loop['id']}: unknown status {loop['status']!r}")
        if not isinstance(loop["hosts"], list) or not loop["hosts"] \
                or not all(isinstance(h, str) and h for h in loop["hosts"]):
            raise Invalid(f"{loop['id']}: hosts must be a non-empty list of names")
        if not isinstance(loop["checks"], list) or not loop["checks"]:
            raise Invalid(f"{loop['id']}: checks must be a non-empty list")
        for check in loop["checks"]:
            if not isinstance(check, dict) or set(check) != {"kind", "target"} \
                    or not isinstance(check["target"], str):
                raise Invalid(f"{loop['id']}: malformed check")
            if check["kind"] not in KINDS:
                raise Invalid(f"{loop['id']}: unknown check kind {check['kind']!r}")
            if check["kind"] != "none" and not check["target"]:
                raise Invalid(f"{loop['id']}: empty check target")
            if check["kind"] in ("timer", "service") and not UNIT.fullmatch(check["target"]):
                raise Invalid(f"{loop['id']}: {check['kind']} target is not a unit name: {check['target']!r}")
        retired = loop.get("retired")
        if loop["status"] == "retired":
            if not isinstance(retired, dict) or set(retired) != {"date", "reason"} \
                    or not all(isinstance(v, str) and v for v in retired.values()):
                raise Invalid(f"{loop['id']}: retired loop needs retired.date and retired.reason")
        elif retired is not None:
            raise Invalid(f"{loop['id']}: retired block on a non-retired loop")
    return data


def load_registry(path):
    return validate_registry(load_json(path))


# ---- scheduler observation ------------------------------------------------------------------
def _run(argv):
    RUN_ARGV.append(list(argv))
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def systemctl(args, allow_enable=False):
    """The only place systemctl is spawned: verb allowlist, user manager only."""
    verb = args[0]
    if verb not in SYSTEMCTL_VERBS or (verb == "enable" and not allow_enable):
        raise RuntimeError(f"systemctl verb not allowed: {verb}")
    binary = shutil.which("systemctl")
    if binary is None:
        raise Unsupported("no systemctl on PATH")
    return _run(["systemctl", "--user", *args])


def systemd_usable():
    """Returns (ok, detail). Observed through PATH and a reachable user manager, never assumed."""
    if shutil.which("systemctl") is None:
        return False, "no systemctl on PATH"
    if systemctl(["show-environment"]).returncode != 0:
        return False, "systemctl --user show-environment failed"
    return True, "ok"


def show_unit(unit):
    result = systemctl(["show", "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", "--", unit])
    props = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    return props


def evaluate(check, registry_dir, host_ok, systemd, settings):
    kind, target = check["kind"], check["target"]
    if kind == "none":
        return "none", False
    if kind in ("timer", "service"):
        if not systemd:
            return NOT_OBSERVABLE, False
        props = show_unit(target)
        loaded, active = props.get("LoadState"), props.get("ActiveState")
        if kind == "timer" and loaded == "loaded" and active in ("active", "waiting"):
            return "ok", False
        if kind == "service" and loaded == "loaded" and active == "active":
            return "ok", False
        if loaded == "loaded":
            return "inactive", False
        return "missing", True
    if kind == "hook":
        if not settings:
            return "not-observable: no --settings file", False
        try:
            found = target.lower() in Path(settings).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            return "not-observable: settings file unreadable", False
        return ("ok", False) if found else ("missing", True)
    path = Path(target)
    if not path.is_absolute():
        path = registry_dir / path
    return ("ok", False) if path.exists() else ("missing", True)


def cmd_health(args):
    registry = Path(args.registry)
    data = load_registry(registry)
    systemd = systemd_usable()[0]
    loops, violations = [], 0
    for loop in data["loops"]:
        entry = {"id": loop["id"], "status": loop["status"], "checks": [], "violation": False}
        applies = args.host is None or "any" in loop["hosts"] or args.host in loop["hosts"]
        for check in loop["checks"]:
            if loop["status"] == "retired":
                result, bad = "retired", False
            elif not applies:
                result, bad = "skipped: host", False
            else:
                result, bad = evaluate(check, registry.resolve().parent, applies, systemd, args.settings)
            entry["checks"].append({"kind": check["kind"], "target": check["target"], "result": result})
            entry["violation"] = entry["violation"] or bad
        if loop["status"] == "dead" and applies:
            entry["violation"] = True
        violations += entry["violation"]
        loops.append(entry)
    report = {"loops": loops, "violations": violations}
    if args.format == "json":
        print(dump(report), end="")
    else:
        for loop in loops:
            print(f"{loop['id']} [{loop['status']}]{' VIOLATION' if loop['violation'] else ''}")
            for check in loop["checks"]:
                print(f"    {check['kind']}:{check['target']} - {check['result']}")
        print(f"violations: {violations}")
    return 1 if violations else 0


# ---- import from the Markdown loop-sync table -----------------------------------------------
def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def import_markdown(text):
    columns, loops = None, []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        if columns is None:
            names = cells(line)
            if "STATUS" in names:
                columns = {n: i for i, n in enumerate(names)}
            continue
        row = cells(line)
        if not row or not row[0].startswith("`"):
            continue
        status_cell = row[columns["STATUS"]] if columns["STATUS"] < len(row) else ""
        hosts_cell = row[columns["Hosts"]] if "Hosts" in columns and columns["Hosts"] < len(row) else "any"
        check_cell = row[columns["Check"]] if "Check" in columns and columns["Check"] < len(row) else ""
        checks = []
        for part in check_cell.split(";"):
            part = part.strip().replace("`", "")
            kind, _, target = part.partition(":")
            if kind in KINDS and kind != "none" and target.strip():
                checks.append({"kind": kind, "target": target.strip()})
        loops.append({
            "id": row[0].replace("`", ""),
            "hosts": [h.strip() for h in hosts_cell.split(",") if h.strip()] or ["any"],
            "checks": checks or [{"kind": "none", "target": ""}],
            "status": "dead" if status_cell.startswith("⛔") else "live",
        })
    if columns is None:
        raise Invalid("no table with a STATUS column found")
    return validate_registry({"schema_version": 1, "loops": loops})


def cmd_import(args):
    try:
        text = Path(args.source).read_text(encoding="utf-8")
    except OSError as error:
        raise Invalid(f"cannot read {args.source}: {error}")
    out = dump(import_markdown(text))
    if args.out:
        atomic_write(args.out, out)
        print(f"wrote {args.out}")
    else:
        print(out, end="")
    return 0


# ---- scheduler adapters ---------------------------------------------------------------------
def find_loop(data, loop_id):
    for loop in data["loops"]:
        if loop["id"] == loop_id:
            return loop
    raise Invalid(f"unknown loop: {loop_id}")


def render_units(loop, calendar, command):
    if not calendar or not command or "\n" in calendar + command:
        raise Invalid("--calendar and --exec must be non-empty single lines")
    timers = [c["target"] for c in loop["checks"] if c["kind"] == "timer" and UNIT.fullmatch(c["target"])]
    if not timers:
        raise Invalid(f"{loop['id']} has no timer check to derive a unit name from")
    name = timers[0][: -len(".timer")]
    desc = f"Description=aiarch loop {loop['id']}\n"
    return {
        f"{name}.timer": f"[Unit]\n{desc}\n[Timer]\nOnCalendar={calendar}\nPersistent=true\n\n"
                         f"[Install]\nWantedBy=timers.target\n",
        f"{name}.service": f"[Unit]\n{desc}\n[Service]\nType=oneshot\nExecStart={command}\n",
    }


def cron_text(loop, calendar, command):
    if not calendar or not command or "\n" in calendar + command:
        raise Invalid("--calendar and --exec must be non-empty single lines")
    return f"# aiarch loop: {loop['id']}\n{calendar} {command}\n"


def manifest_path(args):
    if args.adapter_target == "cron":
        return Path(str(args.crontab_file) + MANIFEST)
    return Path(args.unit_dir) / MANIFEST


def read_manifest(path, kind):
    if not path.exists():
        return None
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise Invalid(f"malformed manifest: {path}")
    for name in data["files"]:
        if not isinstance(name, str) or "/" in name or ".." in name or (
                kind == "systemd-user" and not UNIT.fullmatch(name)):
            raise Invalid(f"malformed manifest: bad file name {name!r}")
    return data


def target_kind(args):
    kind = "cron" if getattr(args, "adapter", None) == "cron" or (
        getattr(args, "crontab_file", None) and not getattr(args, "unit_dir", None)) else "systemd-user"
    args.adapter_target = kind
    if kind == "cron" and not args.crontab_file:
        raise Invalid("--crontab-file is required for the cron adapter")
    if kind == "systemd-user" and not args.unit_dir:
        raise Invalid("--unit-dir is required for the systemd-user adapter")
    return kind


def cmd_sched_plan_apply(args, apply):
    kind = target_kind(args)
    loop = find_loop(load_registry(args.registry), args.loop)
    if kind == "cron":
        files = {Path(args.crontab_file).name: cron_text(loop, args.calendar, args.exec)}
        base = Path(args.crontab_file).parent
    else:
        files = render_units(loop, args.calendar, args.exec)
        base = Path(args.unit_dir)
    if not apply:
        print(f"plan ({kind}): would write into {base}")
        for name, text in files.items():
            print(f"--- {name}\n{text}", end="")
        return 0
    if kind == "cron":
        if shutil.which("crontab") is None:
            raise Unsupported("no crontab on PATH")
        _run(["crontab", "-l"])  # observation only; the real crontab is never written
    else:
        ok, detail = systemd_usable()
        if not ok:
            raise Unsupported(detail)
    mpath = manifest_path(args)
    manifest = read_manifest(mpath, kind) or {"schema_version": 1, "adapter": kind, "files": {}}
    for name, text in files.items():
        existing = base / name
        if existing.exists() and manifest["files"].get(name) != sha(existing.read_bytes()):
            raise Unsupported(f"refusing to overwrite a file this tool did not write: {existing}")
    for name, text in files.items():
        atomic_write(base / name, text)
        manifest["files"][name] = sha(text)
    atomic_write(mpath, dump(manifest))
    print(f"applied ({kind}): wrote {', '.join(sorted(files))} and {mpath.name}")
    if kind == "cron":
        print(f"install it yourself with: crontab {args.crontab_file}")
    else:
        systemctl(["daemon-reload"])
        if args.enable:
            systemctl(["enable", "--", [n for n in files if n.endswith(".timer")][0]], allow_enable=True)
    return 0


def cmd_sched_report(args):
    kind = target_kind(args)
    manifest = read_manifest(manifest_path(args), kind)
    units = []
    if manifest:
        if kind == "cron":
            listed = ""
            if shutil.which("crontab"):
                listed = _run(["crontab", "-l"]).stdout
            for name in sorted(manifest["files"]):
                units.append({"unit": name, "observed": {"installed": all(
                    line in listed for line in (Path(args.crontab_file).read_text().splitlines()[1:]))}})
        else:
            usable = systemd_usable()[0]
            for name in sorted(manifest["files"]):
                units.append({"unit": name, "observed": show_unit(name) if usable else NOT_OBSERVABLE})
    report = {"adapter": kind, "units": units}
    if args.format == "json":
        print(dump(report), end="")
    else:
        for unit in units:
            print(f"{unit['unit']}: {unit['observed']}")
        if not units:
            print("no manifest: nothing recorded")
    return 0


def cmd_sched_rollback(args):
    kind = target_kind(args)
    mpath = manifest_path(args)
    manifest = read_manifest(mpath, kind)
    if not manifest:
        print("nothing to roll back")
        return 0
    base = Path(args.crontab_file).parent if kind == "cron" else Path(args.unit_dir)
    for name, digest in sorted(manifest["files"].items()):
        target = base / name
        if not target.exists():
            continue
        if sha(target.read_bytes()) != digest:
            print(f"kept (modified since apply): {name}")
            continue
        target.unlink()
        print(f"removed {name}")
    mpath.unlink()
    if kind == "systemd-user" and systemd_usable()[0]:
        systemctl(["daemon-reload"])
    return 0


def cmd_sched_observe(args):
    adapters, usable = {}, systemd_usable()
    adapters["systemd-user"] = {"state": "observed" if usable[0] else "unavailable", "detail": usable[1]}
    if shutil.which("crontab"):
        result = _run(["crontab", "-l"])
        adapters["cron"] = {"state": "observed", "detail": "crontab -l exit %d" % result.returncode}
    else:
        adapters["cron"] = {"state": "unavailable", "detail": "no crontab on PATH"}
    adapters["windows"] = {"state": "unsupported", "detail": "gap: no Task Scheduler adapter exists"}
    adapters["macos"] = {"state": "unsupported", "detail": "gap: no launchd adapter exists"}
    report = {"host_platform": platform.system().lower(), "adapters": adapters, "argv": RUN_ARGV}
    if args.format == "json":
        print(dump(report), end="")
    else:
        for name, info in adapters.items():
            print(f"{name}: {info['state']} ({info['detail']})")
    return 0


def cmd_scheduler(args):
    return {
        "plan": lambda: cmd_sched_plan_apply(args, False),
        "apply": lambda: cmd_sched_plan_apply(args, True),
        "report": lambda: cmd_sched_report(args),
        "rollback": lambda: cmd_sched_rollback(args),
        "observe": lambda: cmd_sched_observe(args),
    }[args.verb]()


# ---- changelog and retirement ---------------------------------------------------------------
def cmd_changelog(args):
    if not args.out:
        raise Invalid("--out is required")
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    if out == repo or repo in out.parents:
        raise Invalid("--out must be outside --repo")
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run(
        ["git", "-C", str(repo), "log", f"--since={args.since}", "--date=short", "--format=%ad%x09%s"],
        capture_output=True, text=True, env=env, timeout=60)
    if result.returncode != 0:
        raise Invalid(f"git log failed: {result.stderr.strip()}")
    weeks = {}
    for line in result.stdout.splitlines():
        day, _, subject = line.partition("\t")
        if subject.startswith("autopilot:"):
            continue
        date = datetime.date.fromisoformat(day)
        monday = date - datetime.timedelta(days=date.weekday())
        weeks.setdefault(monday.isoformat(), []).append(subject)
    lines = [f"# Changelog draft since {args.since}", ""]
    for monday in sorted(weeks, reverse=True):
        lines += [f"## Week of {monday}", ""] + [f"- {s}" for s in weeks[monday]] + [""]
    text = "\n".join(lines)
    if out.exists() and out.read_text(encoding="utf-8") == text:
        print(f"unchanged {out}")
        return 0
    atomic_write(out, text)
    print(f"wrote {out}")
    return 0


def cmd_retire(args):
    path = Path(args.registry)
    data = load_registry(path)
    loop = find_loop(data, args.loop)
    if loop["status"] == "retired":
        raise Invalid(f"{args.loop} is already retired")
    if not args.reason.strip():
        raise Invalid("--reason must not be empty")
    date = args.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    loop["status"] = "retired"
    loop["retired"] = {"date": date, "reason": args.reason.strip()}
    old = path.read_text(encoding="utf-8")
    new = dump(data)
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "registry", "registry.new"))
    if not args.apply:
        print(diff or "no change", end="")
        print("dry-run: pass --apply to write")
        return 0
    backup = Path(str(path) + ".bak")
    atomic_write(backup, old)
    atomic_write(path, new)
    print(f"retired {args.loop}; backup at {backup}")
    return 0


# ---- CLI ------------------------------------------------------------------------------------
class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"maintenance: {message}", file=sys.stderr)
        raise SystemExit(2)


def build_parser():
    p = Parser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    s = sub.add_parser("validate")
    s.add_argument("--registry", required=True)
    s = sub.add_parser("health")
    s.add_argument("--registry", required=True)
    s.add_argument("--host")
    s.add_argument("--settings")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s = sub.add_parser("import")
    s.add_argument("--from", dest="source", required=True)
    s.add_argument("--out")
    s = sub.add_parser("scheduler")
    s.add_argument("verb", choices=("plan", "apply", "report", "rollback", "observe"))
    s.add_argument("--registry")
    s.add_argument("--loop")
    s.add_argument("--adapter", choices=("systemd-user", "cron"), default="systemd-user")
    s.add_argument("--unit-dir")
    s.add_argument("--crontab-file")
    s.add_argument("--calendar")
    s.add_argument("--exec")
    s.add_argument("--enable", action="store_true")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s = sub.add_parser("changelog")
    s.add_argument("--repo", required=True)
    s.add_argument("--since", required=True)
    s.add_argument("--out")
    s = sub.add_parser("retire")
    s.add_argument("--registry", required=True)
    s.add_argument("--loop", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--date")
    s.add_argument("--apply", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "validate":
            load_registry(args.registry)
            print("ok")
            return 0
        if args.cmd == "scheduler":
            if args.verb in ("plan", "apply") and not (args.registry and args.loop):
                raise Invalid("--registry and --loop are required")
            if args.verb in ("report", "rollback"):
                args.adapter = "cron" if args.crontab_file else args.adapter
            if args.verb == "observe":
                return cmd_scheduler(args)
        return {"health": cmd_health, "import": cmd_import, "scheduler": cmd_scheduler,
                "changelog": cmd_changelog, "retire": cmd_retire}[args.cmd](args)
    except Invalid as error:
        print(f"maintenance: invalid: {error}", file=sys.stderr)
        return 2
    except Unsupported as error:
        print(f"maintenance: unsupported: {error}", file=sys.stderr)
        print("unsupported")
        return 3


if __name__ == "__main__":
    sys.exit(main())
