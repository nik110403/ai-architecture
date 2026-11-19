#!/usr/bin/env python3
"""Generic manifest-driven install / upgrade / rollback / recover primitive (stdlib only).

  installer.py plan|apply|status|rollback|recover --manifest M --target-root R [--state-dir S] [--allow-takeover]

The manifest lists files {src, dest, mode, sha256}; every source is hash-verified before anything is written (fail closed).
`--target-root` is required: there is no default root and `$HOME` is never implicit. `apply` journals first, backs up what it
replaces, writes atomically and records the installed hashes. A target whose bytes differ from both the recorded installed hash
and the new one is a user change: exit 1, preserved. A target that exists, was never recorded and differs in bytes or mode is
`unmanaged`: `apply` refuses (exit 1, `unmanaged-file`) before writing anything; `--allow-takeover` (plan and apply, never
stored) backs it up and replaces it. `rollback` restores the pre-install originals and removes what the
installer created. `recover` reverts or completes an interrupted transaction from the journal. Prints one JSON object with a
`reason`. Exit: 0 ok, 1 user change / interrupted / invalid state, 2 refused, 3 busy. Installs nothing by itself, ever.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

SCHEMA = "installer-manifest/1"
CRASH_ENV = "AIARCH_INSTALLER_CRASH_AFTER"  # test seam: os._exit(137) after journal step k (no handler runs)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAX_MANIFEST = 1 << 20
FAULT_ENV = "AIARCH_INSTALLER_FAULT"  # test seam: os._exit(137) at the named point (no handler runs)


def fault(point):
    if os.environ.get(FAULT_ENV) == point:
        os._exit(137)


class Refused(Exception):
    def __init__(self, reason, message="", code=2):
        super().__init__(message or reason)
        self.reason, self.message, self.code = reason, message or reason, code


def expected_uid():
    return os.geteuid()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k}")
        out[k] = v
    return out


def _no_const(name):
    raise ValueError(f"non-finite number {name}")


def strict(text):
    return json.loads(text, object_pairs_hook=_unique, parse_constant=_no_const)


class Out:
    redact = staticmethod(lambda t: t)

    @classmethod
    def arm(cls, pairs):
        pairs = sorted([(str(p), label) for label, p in pairs if p], key=lambda x: -len(x[0]))

        def redact(text):
            for needle, label in pairs:
                text = text.replace(needle, label)
                real = os.path.realpath(needle)
                text = text.replace(real, label)
            return text
        cls.redact = staticmethod(redact)

    @classmethod
    def emit(cls, code, reason, **kw):
        print(cls.redact(json.dumps(dict(kw, reason=reason), sort_keys=True, allow_nan=False)))
        if code:
            print(cls.redact(f"installer: {reason}: {kw.get('message', '')}"), file=sys.stderr)
        return code


# --- target and manifest ------------------------------------------------------------------------------------------------

def load_target(path):
    if not path:
        raise Refused("target-root-required", "--target-root is required; there is no default root")
    p = Path(path)
    if not p.is_dir():
        raise Refused("target-root-invalid", "--target-root must be an existing directory")
    real = Path(os.path.realpath(p))
    st = os.stat(real)
    if st.st_uid != expected_uid():
        raise Refused("foreign-owner", "the target root is owned by another user")
    return real


def safe_dest(root, dest):
    if not isinstance(dest, str) or not dest or dest.startswith("/") or "\\" in dest or "\0" in dest:
        raise Refused("dest-unsafe", f"dest {dest!r} must be a relative path")
    parts = dest.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise Refused("dest-unsafe", f"dest {dest!r} must not contain empty, '.' or '..' components")
    cur = root
    for i, part in enumerate(parts):
        cur = cur / part
        if os.path.islink(cur):
            raise Refused("symlink-component", f"dest {dest!r} passes through a symlink")
        if not os.path.lexists(cur):
            break
        if i < len(parts) - 1 and not os.path.isdir(cur):
            raise Refused("dest-unsafe", f"dest {dest!r}: a path component is not a directory")
    target = root.joinpath(*parts)
    if os.path.commonpath([str(root), os.path.realpath(target)]) != str(root):
        raise Refused("dest-unsafe", f"dest {dest!r} resolves outside the target root")
    if os.path.lexists(target) and not os.path.isfile(target):
        raise Refused("dest-unsafe", f"dest {dest!r} exists and is not a regular file")
    return target


def load_manifest(path):
    p = Path(path)
    try:
        if p.stat().st_size > MAX_MANIFEST:
            raise Refused("manifest-invalid", "manifest too large")
        doc = strict(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise Refused("manifest-invalid", f"manifest unreadable: {error.__class__.__name__}")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("files"), list):
        raise Refused("manifest-invalid", "not an installer manifest")
    if not isinstance(doc.get("name"), str) or not NAME.fullmatch(doc["name"]) or not isinstance(doc.get("version"), str) or not doc["version"]:
        raise Refused("manifest-invalid", "name or version invalid")
    seen = set()
    files = []
    for e in doc["files"]:
        if not isinstance(e, dict) or set(e) != {"src", "dest", "mode", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", str(e["sha256"])):
            raise Refused("manifest-invalid", "file entry malformed")
        if not re.fullmatch(r"0[0-7]{3}", str(e["mode"])):
            raise Refused("manifest-invalid", "mode must look like 0644")
        if e["dest"] in seen:
            raise Refused("manifest-invalid", "duplicate dest")
        seen.add(e["dest"])
        src = e["src"]
        if not isinstance(src, str) or src.startswith("/") or ".." in src.split("/"):
            raise Refused("manifest-invalid", "src must be relative to the manifest")
        sp = p.parent / src
        if os.path.islink(sp) or not sp.is_file():
            raise Refused("manifest-invalid", f"source {src!r} is missing or a symlink")
        data = sp.read_bytes()
        if sha(data) != e["sha256"]:
            raise Refused("hash-mismatch", f"source {src!r} does not match its pinned sha256")
        files.append({"dest": e["dest"], "mode": int(e["mode"], 8), "sha256": e["sha256"], "data": data})
    return {"name": doc["name"], "version": doc["version"], "files": files}


# --- state ------------------------------------------------------------------------------------------------------------------

class Store:
    def __init__(self, root, name, state_dir=None):
        self.dir = Path(state_dir) if state_dir else root / ".installer" / name
        self.installed = self.dir / "installed.json"
        self.journal = self.dir / "journal.jsonl"
        self.txn = self.dir / "txn"
        self.orig = self.dir / "originals"
        self.orig_index = self.dir / "originals.json"
        self.n = 0

    def ensure(self):
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.path.islink(self.dir) or os.stat(self.dir).st_uid != expected_uid():
            raise Refused("foreign-owner", "the state dir is a symlink or owned by someone else")

    def lock(self):
        self.ensure()
        fd = os.open(self.dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Refused("busy", "another installer run holds the lock", 3)
        return fd

    def read_json(self, path, default):
        try:
            return strict(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, ValueError):
            raise Refused("state-invalid", f"{path.name} is unreadable", 1)

    def write_json(self, path, doc):
        atomic_write(path, json.dumps(doc, indent=1, sort_keys=True).encode(), 0o600)

    def entries(self):
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(strict(line))
            except ValueError:
                break  # a torn last line: ignored
        return out

    def append(self, entry):
        self.n += 1
        fd = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        crash = os.environ.get(CRASH_ENV)
        if crash and int(crash) == self.n and entry.get("step") != "commit":  # after commit nothing is left to interrupt
            os._exit(137)

    def open_txn(self):
        es = self.entries()
        if not es:
            return None
        if es[-1].get("step") in ("commit", "reverted", "rolled-back", "recovered"):
            return None
        starts = [i for i, e in enumerate(es) if e.get("step") == "begin"]
        return es[starts[-1]:] if starts else es  # only the open transaction, never an earlier one's entries


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, mode, point=None):
    path = Path(path)
    tmp = path.parent / f".{path.name}.aiarch-tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        if point:
            fault(f"{point}:pre-chmod")
        os.fchmod(fh.fileno(), mode)
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def current(path):
    try:
        data = path.read_bytes()
        return data, stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None, None


# --- plan / apply -----------------------------------------------------------------------------------------------------------

def classify(root, man, installed, allow_takeover=False):
    recorded = (installed or {}).get("files", {})
    acts = []
    for f in man["files"]:
        target = safe_dest(root, f["dest"])
        data, mode = current(target)
        rec = recorded.get(f["dest"])
        if data is None:
            acts.append(dict(f, action="create", target=target, backup=False, was_installed=bool(rec)))
        elif sha(data) == f["sha256"] and mode == f["mode"]:
            acts.append(dict(f, action="unchanged", target=target))
        elif rec is None:  # on disk, never recorded by this installer, differs in bytes or mode
            if allow_takeover:
                acts.append(dict(f, action="takeover", target=target, backup=True, was_installed=False))
            else:
                acts.append(dict(f, action="unmanaged", target=target))
        elif sha(data) == f["sha256"]:
            acts.append(dict(f, action="replace", target=target, backup=True, was_installed=True))
        elif sha(data) == rec["sha256"]:
            acts.append(dict(f, action="replace", target=target, backup=True, was_installed=True))
        else:
            acts.append(dict(f, action="conflict", target=target))
    return acts


def cmd_plan(root, man, store, allow_takeover=False):
    installed = store.read_json(store.installed, None)
    acts = classify(root, man, installed, allow_takeover)
    return Out.emit(0, "plan", actions=[{"dest": a["dest"], "action": a["action"]} for a in acts], version=man["version"])


def refuse_blocked(acts):
    conflicts = [a["dest"] for a in acts if a["action"] == "conflict"]
    if conflicts:
        raise Refused("user-change", f"changed by you since the last install, left untouched: {', '.join(conflicts)}", 1)
    unmanaged = [a["dest"] for a in acts if a["action"] == "unmanaged"]
    if unmanaged:
        raise Refused("unmanaged-file", f"exists and was not installed by this installer, left untouched (--allow-takeover to replace with a backup): {', '.join(unmanaged)}", 1)


def cmd_apply(root, man, store, allow_takeover=False):
    if not store.dir.exists():  # nothing recorded yet: refuse read-only, before the state dir (and its lock) is created
        refuse_blocked(classify(root, man, None, allow_takeover))
    fd = store.lock()
    try:
        if store.open_txn():
            raise Refused("interrupted", "an earlier apply was interrupted: run recover", 1)
        installed = store.read_json(store.installed, None)
        acts = classify(root, man, installed, allow_takeover)
        refuse_blocked(acts)
        todo = [a for a in acts if a["action"] != "unchanged"]
        same_version = installed is not None and installed.get("version") == man["version"]
        if not todo and same_version:
            return Out.emit(0, "no-change", version=man["version"])
        originals = store.read_json(store.orig_index, {})
        shutil.rmtree(store.txn, ignore_errors=True)
        store.txn.mkdir(parents=True, mode=0o700)
        new_dirs = []
        for a in todo:
            d = a["target"].parent
            chain = []
            while not d.exists() and d != root:
                chain.append(d)
                d = d.parent
            new_dirs += [str(c.relative_to(root)) for c in reversed(chain)]
        new_dirs = sorted(set(new_dirs), key=lambda x: x.count("/"))
        begin = {"step": "begin", "version": man["version"], "dests": [{"dest": a["dest"], "create": a["action"] == "create", "sha256": a["sha256"],
                                                                         "backup": bool(a.get("backup")),
                                                                         "orig": a["dest"] not in originals and bool(a.get("backup"))} for a in todo],
                 "new_dirs": new_dirs, "had_installed": installed is not None, "had_originals": bool(originals)}
        store.ensure()
        if installed is not None:
            shutil.copy2(store.installed, store.txn / "installed.prev")
        if store.orig_index.exists():
            shutil.copy2(store.orig_index, store.txn / "originals.prev")
        store.append(begin)
        recorded = (installed or {}).get("files", {})
        for a in acts:  # identical but never recorded: rollback must leave it as it was, not remove it
            if a["action"] == "unchanged" and a["dest"] not in recorded and a["dest"] not in originals:
                data, mode = current(a["target"])
                store.orig.mkdir(exist_ok=True, mode=0o700)
                atomic_write(store.orig / f"o{len(originals)}", data, mode)
                originals[a["dest"]] = {"file": f"o{len(originals)}", "mode": mode}
                store.write_json(store.orig_index, originals)
        files = dict((installed or {}).get("files", {}))
        created_dirs = list((installed or {}).get("created_dirs", []))
        for i, a in enumerate(todo):
            target = a["target"]
            if a.get("backup"):
                data, mode = current(target)
                atomic_write(store.txn / f"b{i}", data, mode, "backup")  # temp, fsync, chmod, rename, fsync dir; journaled only after
                fault("backup:unjournaled")
                if a["dest"] not in originals and not a.get("was_installed"):
                    store.orig.mkdir(exist_ok=True, mode=0o700)
                    atomic_write(store.orig / f"o{len(originals)}", data, mode)
                    originals[a["dest"]] = {"file": f"o{len(originals)}", "mode": mode}
                    store.write_json(store.orig_index, originals)
                store.append({"step": "backup", "dest": a["dest"], "i": i})
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, a["data"], a["mode"])
            store.append({"step": "write", "dest": a["dest"], "i": i})
            files[a["dest"]] = {"sha256": a["sha256"], "mode": a["mode"], "created": a["action"] == "create" or files.get(a["dest"], {}).get("created", False)}
        for f in man["files"]:
            files.setdefault(f["dest"], {"sha256": f["sha256"], "mode": f["mode"], "created": False})
        store.write_json(store.installed, {"name": man["name"], "version": man["version"], "files": files,
                                           "created_dirs": sorted(set(created_dirs) | set(new_dirs))})
        store.append({"step": "record"})
        store.append({"step": "commit"})
        shutil.rmtree(store.txn, ignore_errors=True)
        return Out.emit(0, "applied", version=man["version"], changed=[a["dest"] for a in todo])
    finally:
        os.close(fd)


# --- recover / rollback / status ---------------------------------------------------------------------------------------------

def restore_file(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, data, mode)


def cmd_recover(root, man, store):
    fd = store.lock()
    try:
        es = store.open_txn()
        if not es:
            return Out.emit(0, "recovered", outcome="nothing", message="no interrupted transaction")
        begin = next((e for e in es if e.get("step") == "begin"), None)
        if begin is None:
            raise Refused("state-invalid", "journal has no begin entry", 1)
        if any(e.get("step") == "record" for e in es):  # installed.json was replaced: the new state is complete
            store.append({"step": "commit"})
            shutil.rmtree(store.txn, ignore_errors=True)
            return Out.emit(0, "recovered", outcome="completed")
        journaled = {e.get("i") for e in es if e.get("step") == "backup"}
        for i, d in enumerate(begin["dests"]):
            target = safe_dest(root, d["dest"])
            backup = store.txn / f"b{i}"
            if i in journaled and backup.exists():  # an unjournaled backup may be incomplete; its target was not yet touched
                restore_file(target, backup.read_bytes(), stat.S_IMODE(backup.stat().st_mode))
            elif d["create"]:
                data, _ = current(target)
                if data is not None and sha(data) == d["sha256"]:
                    target.unlink()
            tmp = target.parent / f".{target.name}.aiarch-tmp"
            if os.path.lexists(tmp):
                tmp.unlink()
        for rel in sorted(begin["new_dirs"], key=lambda x: -x.count("/")):
            try:
                (root / rel).rmdir()
            except OSError:
                pass
        prev = store.txn / "installed.prev"
        if prev.exists():
            shutil.copy2(prev, store.installed)
        elif store.installed.exists():
            store.installed.unlink()
        oprev = store.txn / "originals.prev"
        if oprev.exists():
            shutil.copy2(oprev, store.orig_index)
        elif store.orig_index.exists():
            store.orig_index.unlink()
        if store.orig.exists():
            keep = {v["file"] for v in store.read_json(store.orig_index, {}).values()}
            for f in store.orig.iterdir():
                if f.name not in keep:
                    f.unlink()
        store.append({"step": "reverted"})
        shutil.rmtree(store.txn, ignore_errors=True)
        return Out.emit(0, "recovered", outcome="reverted")
    finally:
        os.close(fd)


def cmd_rollback(root, man, store):
    fd = store.lock()
    try:
        if store.open_txn():
            raise Refused("interrupted", "an earlier apply was interrupted: run recover first", 1)
        installed = store.read_json(store.installed, None)
        if installed is None:
            return Out.emit(0, "rolled-back", message="nothing installed")
        originals = store.read_json(store.orig_index, {})
        plan = []
        for dest, rec in installed["files"].items():
            target = safe_dest(root, dest)
            data, mode = current(target)
            orig = originals.get(dest)
            orig_data = (store.orig / orig["file"]).read_bytes() if orig else None
            if data is None:
                plan.append((target, "skip", None, None))
            elif sha(data) == rec["sha256"]:
                plan.append((target, "restore" if orig else "remove", orig_data, orig["mode"] if orig else None))
            elif orig_data is not None and data == orig_data:
                plan.append((target, "skip", None, None))
            else:
                raise Refused("user-change", f"{dest} was changed by you since the install; rollback leaves everything as it is", 1)
        for target, what, data, mode in plan:
            if what == "restore":
                restore_file(target, data, mode)
            elif what == "remove":
                target.unlink()
        for rel in sorted(installed.get("created_dirs", []), key=lambda x: -x.count("/")):
            try:
                (root / rel).rmdir()
            except OSError:
                pass
        store.installed.unlink()
        if store.orig_index.exists():
            store.orig_index.unlink()
        shutil.rmtree(store.orig, ignore_errors=True)
        store.append({"step": "rolled-back"})
        return Out.emit(0, "rolled-back", version=installed["version"])
    finally:
        os.close(fd)


def cmd_status(root, man, store):
    if not store.dir.is_dir():
        return Out.emit(0, "status", state="absent")
    if store.open_txn():
        return Out.emit(1, "interrupted", state="interrupted", message="an apply was interrupted: run recover")
    installed = store.read_json(store.installed, None)
    if installed is None:
        return Out.emit(0, "status", state="absent")
    files = {}
    for dest, rec in installed["files"].items():
        data, mode = current(safe_dest(root, dest))
        files[dest] = "missing" if data is None else ("ok" if sha(data) == rec["sha256"] and mode == rec["mode"] else "modified")
    state = "installed" if installed["version"] == man["version"] else "installed-previous"
    return Out.emit(0, "status", state=state, installed_version=installed["version"], files=files)


COMMANDS = {"plan": cmd_plan, "apply": cmd_apply, "status": cmd_status, "rollback": cmd_rollback, "recover": cmd_recover}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("usage", message)


def main(argv=None):
    p = Parser(prog="installer.py", description=__doc__.splitlines()[0])
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--manifest", required=True)
    p.add_argument("--target-root")
    p.add_argument("--state-dir")
    p.add_argument("--allow-takeover", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit as stop:
        return 2 if stop.code else 0
    except Refused as e:
        return Out.emit(2, "usage", message=e.message)
    try:
        root = load_target(a.target_root)
        Out.arm([("<root>", root), ("<state>", a.state_dir), ("<manifest>", os.path.dirname(os.path.abspath(a.manifest)))])
        man = load_manifest(a.manifest)
        store = Store(root, man["name"], a.state_dir)
        if a.command in ("plan", "apply"):
            return COMMANDS[a.command](root, man, store, a.allow_takeover)
        if a.allow_takeover:
            raise Refused("usage", "--allow-takeover applies to plan and apply only")
        return COMMANDS[a.command](root, man, store)
    except Refused as e:
        return Out.emit(e.code, e.reason, message=e.message)
    except OSError as e:
        return Out.emit(1, "io-error", message=f"{e.__class__.__name__}")


if __name__ == "__main__":
    sys.exit(main())
