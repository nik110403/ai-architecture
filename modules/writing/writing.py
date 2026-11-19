#!/usr/bin/env python3
"""Contract layer for prose work: register routing, fidelity checks, profiles, writer observation.

Subcommands: registers, validate-registers, route, check, profile (export|import|rollback),
writer (status|run). It generates no prose and makes no model call; an optional external writer
runs only behind --allow-writer, with a declared argv, no shell and a minimal environment.
Exit codes: 0 ok, 1 check failed or table invalid, 2 usage or refused input, 3 nothing to restore
or writer failed.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTERS_FILE = HERE / "registers.json"
ASSISTANT = "assistant-draft"
ENV_ALLOW = ("PATH", "LANG", "HOME")
MAX_PROFILE = 64 * 1024
MAX_DESCRIBE = 1024 * 1024
TABLE_KEYS = {"purpose", "rule_sets", "may_inherit_from", "default"}
PROFILE_KEYS = {"version", "registers", "samples"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PRIVATE = re.compile("/" + "home/|C:\\\\+Users", re.I)
INTENT = re.compile(r"^\s*Intent:\s*(.*)$")


class Exit(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code, self.message = code, message


# duplicate-key rejection, same shape as skills/maintenance/scripts/loop_check.py:28-33
def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text, what):
    try:
        return json.loads(text, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError) as error:
        raise Exit(2, f"invalid {what}: {error}")


def read_text(path, what):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Exit(2, f"cannot read {what}: {type(error).__name__}")


def load_json(path, what):
    return parse_json(read_text(path, what), what)


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def canonical(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def emit(obj, fmt):
    if fmt == "json":
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        for key, value in obj.items():
            print(f"{key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}")


# ---- registers -------------------------------------------------------------------------------

def load_table(path=REGISTERS_FILE):
    return load_json(path, "register table")


def validate_table(table):
    """Return the violated rule name and detail, or None."""
    if not isinstance(table, dict) or not isinstance(table.get("registers"), dict):
        return "schema", "table needs a registers object"
    regs = table["registers"]
    nonfiction = table.get("nonfiction_rule_sets")
    if not isinstance(nonfiction, list) or not all(isinstance(x, str) for x in nonfiction):
        return "schema", "nonfiction_rule_sets must be a list of strings"
    for name, reg in regs.items():
        if not isinstance(reg, dict) or not {"purpose", "rule_sets", "may_inherit_from"} <= set(reg) <= TABLE_KEYS:
            return "schema", f"{name}: bad fields"
        if not isinstance(reg["purpose"], str):
            return "schema", f"{name}: purpose must be a string"
        for key in ("rule_sets", "may_inherit_from"):
            if not isinstance(reg[key], list) or not all(isinstance(x, str) for x in reg[key]):
                return "schema", f"{name}: {key} must be a list of strings"
        for parent in reg["may_inherit_from"]:
            if parent not in regs or parent == name:
                return "schema", f"{name}: unknown or self parent {parent}"
        if reg.get("default") == "essay":
            return "no-essay-default", f"{name} defaults to essay"
    corr = regs.get("correspondence")
    if corr and {"essay", "academic"} & set(corr["may_inherit_from"]):
        return "correspondence-no-essay-academic", "correspondence inherits from essay or academic"
    for name in ("personal", "fiction"):
        reg = regs.get(name)
        if reg and (reg["may_inherit_from"] or set(reg["rule_sets"]) & set(nonfiction)):
            return "personal-fiction-isolated", f"{name} inherits or applies a nonfiction rule set"
    return None


def known_registers():
    return set(load_table()["registers"])


def require_register(name):
    if name not in known_registers():
        raise Exit(2, f"unknown register: {name}")


def cmd_registers(a):
    emit(load_table(), a.format)
    return 0


def cmd_validate(a):
    violation = validate_table(load_table(a.file))
    if violation:
        raise Exit(1, f"validate-registers: violated rule: {violation[0]} ({violation[1]})")
    print("validate-registers: OK")
    return 0


# ---- writers ---------------------------------------------------------------------------------

def load_writers(path):
    data = load_json(path, "writers file")
    writers = data.get("writers") if isinstance(data, dict) else None
    if not isinstance(writers, dict):
        raise Exit(2, "writers file needs a writers object")
    base = Path(path).resolve().parent
    result = {}
    for name, spec in writers.items():
        ok = isinstance(spec, dict) and isinstance(spec.get("argv"), list) and spec["argv"] \
            and all(isinstance(x, str) for x in spec["argv"])
        if not ok:
            raise Exit(2, f"writer {name}: argv must be a non-empty list of strings")
        result[name] = [str(base / x[2:]) if x.startswith("./") else x for x in spec["argv"]], base
    return result


def child_env():
    return {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}


def observe(entry):
    """Run `argv --describe` and report what the writer itself says, never what the file claims."""
    argv, base = entry
    try:
        timeout = float(os.environ.get("WRITING_DESCRIBE_TIMEOUT", "10"))
    except ValueError:
        timeout = 10.0
    try:
        done = subprocess.run(argv + ["--describe"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, env=child_env(), cwd=base, shell=False)
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": f"timeout after {timeout:g}s"}
    except OSError as error:
        return {"status": "unavailable", "reason": f"cannot start: {type(error).__name__}"}
    if done.returncode != 0:
        return {"status": "unavailable", "reason": f"describe exited {done.returncode}"}
    try:
        info = json.loads(done.stdout[:MAX_DESCRIBE].decode("utf-8"), object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        return {"status": "unavailable", "reason": "describe output is not JSON"}
    lists = isinstance(info, dict) and all(
        isinstance(info.get(k), list) and all(isinstance(x, str) for x in info[k])
        for k in ("registers", "destinations"))
    if not lists:
        return {"status": "unsupported", "reason": "describe lacks registers and destinations lists"}
    return {"status": "available", "registers": info["registers"], "destinations": info["destinations"]}


def supports(status, register, destination):
    return status["status"] == "available" and register in status["registers"] \
        and destination in status["destinations"]


def limitation(names, register, destination, reasons):
    return {"writer": ASSISTANT, "destination_updated": False, "reason": "; ".join(reasons) or "no writers declared",
            "limitation": f"{','.join(names) or 'none'} does not support {register} for {destination}; "
                          "delivered as assistant draft"}


def cmd_route(a):
    require_register(a.register)
    writers = load_writers(a.writers)
    reasons = []
    for name in sorted(writers):
        status = observe(writers[name])
        if supports(status, a.register, a.destination):
            emit({"writer": name, "limitation": None, "destination_updated": False,
                  "register": a.register, "destination": a.destination}, a.format)
            return 0
        reasons.append(f"{name}: {status['status']}" + (f" ({status['reason']})" if "reason" in status else
                                                         " (register or destination not offered)"))
    emit(limitation(sorted(writers), a.register, a.destination, reasons), a.format)
    return 0


def cmd_status(a):
    writers = load_writers(a.writers)
    if a.name not in writers:
        raise Exit(2, f"unknown writer: {a.name}")
    emit(dict(name=a.name, **observe(writers[a.name])), a.format)
    return 0


# ---- fidelity --------------------------------------------------------------------------------

URL = re.compile(r"https?://[^\s<>\"'\]\)]+")
DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+")
SPAN = re.compile(r"\[\[(.+?)\]\]")
QUOTE = re.compile(r"\"([^\"\n]{1,200})\"|\u201c([^\u201d\n]{1,200})\u201d")
CITEKEY = re.compile(r"(?<![\w@])@[A-Za-z][\w:-]*\w")
UNITS = r"(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|ms|km|kg|MB|GB|KB|USD|EUR|GBP|percent|euros?|dollars?)"
NUMBER = re.compile(rf"(?<![\w.@/-])\d+(?:[.,]\d+)*(?:%|\s{UNITS}\b)?")


def extract_facts(text):
    body = "\n".join(l for l in text.splitlines() if not INTENT.match(l))
    facts = []

    def take(pattern, kind, group=0, strip=""):
        nonlocal body
        for m in pattern.finditer(body):
            value = (m.group(group) if not group else next(g for g in m.groups() if g)).strip().rstrip(strip)
            if value and (kind, value) not in facts:
                facts.append((kind, value))
        body = pattern.sub(lambda m: " " * len(m.group(0)), body)

    take(URL, "url", strip=".,;:")
    take(DOI, "doi", strip=".,;:)]")
    take(SPAN, "text", 1)
    take(QUOTE, "text", 1)
    take(CITEKEY, "text")
    take(NUMBER, "number")
    return facts


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


def fact_present(kind, value, draft):
    value = squash(value)
    core = re.escape(value).replace(r"\ ", r"\s+")
    if kind == "number":
        pattern = r"(?<![\w.])" + core + r"(?!\w|[.,]\d)"
    elif kind in ("url", "doi"):
        pattern = r"(?<![\w/])" + core + r"(?![\w/%#?=&~+-]|\.\w)"
    else:
        pattern = (r"(?<!\w)" if value[0].isalnum() or value[0] == "_" else "") + core + \
                  (r"(?!\w)" if value[-1].isalnum() or value[-1] == "_" else "")
    return re.search(pattern, squash(draft)) is not None


def edit_spans(previous, edited):
    """Return (added lines that must survive, deleted lines that must stay out)."""
    p, e = previous.splitlines(), edited.splitlines()
    added, deleted = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=p, b=e, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            added += [l.strip() for l in e[j1:j2] if l.strip()]
        if tag in ("replace", "delete"):
            deleted += [l.strip() for l in p[i1:i2] if l.strip()]
    deleted = [d for d in deleted if not any(d in a for a in added)]
    return added, deleted


def fidelity(source, draft, register, writer="none", previous=None, edited=None):
    table = load_table()["registers"]
    facts = extract_facts(source)
    missing = [v for k, v in facts if not fact_present(k, v, draft)]
    intent = next((m.group(1).strip() for m in map(INTENT.match, source.splitlines()) if m and m.group(1).strip()), None)
    intent_kept = int(intent is not None and squash(intent) in squash(draft))
    added, deleted = edit_spans(previous, edited) if previous is not None else ([], [])
    lost = [l for l in added if squash(l) not in squash(draft)]
    resurrected = [l for l in deleted if squash(l) in squash(draft)]
    ok = not missing and (intent is None or intent_kept) and not lost and not resurrected
    return {
        "register": register,
        "facts": {"expected": len(facts), "kept": len(facts) - len(missing), "missing": missing},
        "intent": {"expected": int(intent is not None), "kept": intent_kept},
        "user_edits": {"expected": len(added), "kept": len(added) - len(lost), "lost": lost,
                       "deleted_expected": len(deleted), "resurrected": resurrected},
        "review": {"register": register, "rule_sets": table[register]["rule_sets"], "writer": writer,
                   "verdict": "pass" if ok else "fail"},
        "verdict": "pass" if ok else "fail",
    }


def run_check(source_path, draft_path, register, out, writer, previous, edited):
    require_register(register)
    if (previous is None) != (edited is None):
        raise Exit(2, "--previous and --edited go together")
    receipt = fidelity(read_text(source_path, "source"), read_text(draft_path, "draft"), register, writer,
                       read_text(previous, "previous") if previous else None,
                       read_text(edited, "edited") if edited else None)
    atomic_write(Path(out) / "fidelity.json", canonical(receipt), 0o644)
    return receipt


def finish_check(receipt, fmt):
    emit({"verdict": receipt["verdict"], "facts_missing": receipt["facts"]["missing"],
          "intent_kept": receipt["intent"]["kept"], "edits_lost": receipt["user_edits"]["lost"]}, fmt)
    return 0 if receipt["verdict"] == "pass" else 1


def cmd_check(a):
    return finish_check(run_check(a.source, a.draft, a.register, a.out, a.writer_name, a.previous, a.edited),
                        a.format)


def cmd_run(a):
    if not a.allow_writer:
        raise Exit(2, "refused: pass --allow-writer to run the declared writer command")
    require_register(a.register)
    writers = load_writers(a.writers)
    if a.name not in writers:
        raise Exit(2, f"unknown writer: {a.name}")
    status = observe(writers[a.name])
    if not supports(status, a.register, a.destination):
        info = limitation([a.name], a.register, a.destination, [status.get("reason", "register or destination not offered")])
        emit(info, a.format)
        raise Exit(2, info["limitation"])
    source = Path(a.source)
    try:
        data = source.read_bytes()
        timeout = float(os.environ.get("WRITING_RUN_TIMEOUT", "60"))
        argv, base = writers[a.name]
        done = subprocess.run(argv + ["--register", a.register, "--destination", a.destination], input=data,
                              capture_output=True, timeout=timeout, env=child_env(), cwd=base, shell=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Exit(3, f"writer failed: {type(error).__name__}")
    if done.returncode != 0:
        raise Exit(3, f"writer exited {done.returncode}")
    out = Path(a.out)
    atomic_write(out / "draft.md", done.stdout, 0o644)
    receipt = run_check(source, out / "draft.md", a.register, out, a.name, a.previous, a.edited)
    return finish_check(receipt, a.format)


# ---- profiles --------------------------------------------------------------------------------

def private_text(text):
    return bool(PRIVATE.search(text) or EMAIL.search(text))


def strings(node):
    """Every string in a parsed JSON value, keys and values, recursively."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from strings(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from strings(value)


def validate_profile(raw):
    if len(raw.encode("utf-8")) > MAX_PROFILE:
        raise Exit(2, "profile larger than 64 KB")
    profile = parse_json(raw, "profile")
    if private_text(raw) or any(private_text(t) for t in strings(profile)):
        raise Exit(2, "profile refused: contains a private path or an email address")
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
        raise Exit(2, "profile needs exactly: version, registers, samples")
    if profile["version"] != 1 or isinstance(profile["version"], bool):
        raise Exit(2, "profile version must be 1")
    known = known_registers()
    if not isinstance(profile["registers"], dict):
        raise Exit(2, "profile registers must be an object")
    for name, entry in profile["registers"].items():
        if name not in known or not isinstance(entry, dict) or not set(entry) <= {"prefer", "avoid"}:
            raise Exit(2, f"profile register {name}: bad name or keys")
        for key, items in entry.items():
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                raise Exit(2, f"profile {name}.{key} must be a list of strings")
    samples = profile["samples"]
    if not isinstance(samples, list):
        raise Exit(2, "profile samples must be a list")
    for s in samples:
        if not isinstance(s, dict) or set(s) != {"register", "sha256"} or s["register"] not in known \
                or not isinstance(s["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", s["sha256"]):
            raise Exit(2, "profile samples hold only register and sha256 (no sample text)")
    return profile


def current_profile(directory):
    path = Path(directory) / "profile.json"
    if not path.is_file():
        raise Exit(3, "no profile in --profile-dir")
    return path


def backups(directory):
    return sorted((Path(directory) / "backups").glob("[0-9]*.json"))


def cmd_export(a):
    atomic_write(a.out, current_profile(a.profile_dir).read_bytes())
    print("profile export: OK")
    return 0


def cmd_import(a):
    profile = validate_profile(read_text(a.file, "profile"))
    directory = Path(a.profile_dir)
    path = directory / "profile.json"
    if path.is_file():
        existing = backups(directory)
        number = int(existing[-1].stem) + 1 if existing else 1
        atomic_write(directory / "backups" / f"{number:06d}.json", path.read_bytes())
    atomic_write(path, canonical(profile))
    print("profile import: OK")
    return 0


def cmd_rollback(a):
    existing = backups(a.profile_dir)
    if not existing:
        raise Exit(3, "no backup to restore")
    atomic_write(Path(a.profile_dir) / "profile.json", existing[-1].read_bytes())
    existing[-1].unlink()
    print("profile rollback: OK")
    return 0


# ---- cli -------------------------------------------------------------------------------------

def build():
    top = argparse.ArgumentParser(prog="writing", description="Prose contracts: registers, fidelity, profiles.")
    sub = top.add_subparsers(dest="cmd", required=True)

    def add(parent, name, func, help_, *flags):
        p = parent.add_parser(name, help=help_, description=help_)
        p.set_defaults(func=func)
        p.add_argument("--format", choices=("text", "json"), default="text")
        for flag, kw in flags:
            p.add_argument(flag, **kw)
        return p

    req = {"required": True}
    add(sub, "registers", cmd_registers, "Print the register table.")
    p = add(sub, "validate-registers", cmd_validate, "Check a register table against the fixed rules.")
    p.add_argument("file")
    add(sub, "route", cmd_route, "Pick a writer that reports support for a register and destination.",
        ("--register", req), ("--destination", req), ("--writers", req))
    add(sub, "check", cmd_check, "Check a draft against its source and write fidelity.json.",
        ("--source", req), ("--draft", req), ("--register", req), ("--out", req),
        ("--previous", {}), ("--edited", {}), ("--writer-name", {"default": "none"}))
    prof = sub.add_parser("profile", help="Style profile export, import, rollback.")
    ps = prof.add_subparsers(dest="sub", required=True)
    add(ps, "export", cmd_export, "Write the current profile to a file.", ("--profile-dir", req), ("--out", req))
    p = add(ps, "import", cmd_import, "Validate and install a profile, keeping a backup.", ("--profile-dir", req))
    p.add_argument("file")
    add(ps, "rollback", cmd_rollback, "Restore the last backed-up profile.", ("--profile-dir", req))
    wr = sub.add_parser("writer", help="Observe or run a declared external writer.")
    ws = wr.add_subparsers(dest="sub", required=True)
    add(ws, "status", cmd_status, "Ask a writer what it supports (describe only).", ("--writers", req), ("--name", req))
    add(ws, "run", cmd_run, "Run a declared writer on a source and check its draft.",
        ("--writers", req), ("--name", req), ("--register", req), ("--destination", req), ("--source", req),
        ("--out", req), ("--previous", {}), ("--edited", {}),
        ("--allow-writer", {"action": "store_true", "help": "allow running the declared writer command"}))
    return top


def main(argv=None):
    args = build().parse_args(argv)
    try:
        return args.func(args)
    except Exit as error:
        if error.message:
            print(f"writing: {error.message}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
