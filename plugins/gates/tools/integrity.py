#!/usr/bin/env python3
"""Guard-integrity check for the gates plugin, with backup and rollback. Stdlib only.

  integrity.py manifest --plugin P --out M              record (relpath, mode, sha256) of the guard files
  integrity.py check    --plugin P --manifest M         exit 1 naming each missing/changed/unreadable/non-regular/extra file
  integrity.py install  --from P --to T --manifest M    stage P beside T, check it there, back T up to T.bak-<utc>, swap; T is untouched on any failure
  integrity.py rollback --to T --backup B               restore T byte-identically from B

Scope: .claude-plugin/plugin.json, defaults.json and every file under hooks/ (hooks.json, run-hook.sh, the
guards, lib/*), __pycache__ included: any cached .pyc is an unexpected extra file (the hooks run with -B and
never load one). Make the manifest from a TRUSTED tree BEFORE you install: a manifest made from the copy you are
about to install proves nothing. Exit: 0 ok, 1 findings, 2 usage or refused input (symlinked target/backup, ...).
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone

ROOT_FILES = (".claude-plugin/plugin.json", "defaults.json")


class Refused(Exception):
    pass


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def scope(plugin):
    """Relative paths in scope that exist (files, symlinks or anything else), sorted."""
    found = {r for r in ROOT_FILES if os.path.lexists(os.path.join(plugin, r))}
    for base, dirs, files in os.walk(os.path.join(plugin, "hooks")):
        for d in list(dirs):
            if os.path.islink(os.path.join(base, d)):  # a symlinked directory is never descended into: report it
                found.add(os.path.relpath(os.path.join(base, d), plugin))
                dirs.remove(d)
        for name in files:
            found.add(os.path.relpath(os.path.join(base, name), plugin))
    return sorted(found)


def entry(plugin, rel):
    p = os.path.join(plugin, rel)
    st = os.lstat(p)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{rel}: not a regular file (symlink, directory or special file)")
    return {"path": rel, "mode": stat.S_IMODE(st.st_mode), "sha256": _sha(p)}


def make_manifest(plugin):
    if os.path.islink(plugin) or not os.path.isdir(plugin):
        raise Refused(f"plugin is not a plain directory: {plugin}")
    return {"schema_version": 1, "files": [entry(plugin, r) for r in scope(plugin)]}


def load_manifest(path):
    try:
        doc = json.load(open(path))
        files = doc["files"]
        assert doc["schema_version"] == 1 and isinstance(files, list) and files
        assert all(isinstance(f["path"], str) and isinstance(f["mode"], int) and isinstance(f["sha256"], str) for f in files)
        assert len({f["path"] for f in files}) == len(files)
        return doc
    except Exception:
        raise Refused(f"manifest unreadable or malformed: {path}")


def check_tree(plugin, manifest):
    """List of findings (strings); empty = identical to the manifest."""
    findings = []
    want = {f["path"]: f for f in manifest["files"]}
    for rel, f in sorted(want.items()):
        p = os.path.join(plugin, rel)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            findings.append(f"missing: {rel}")
            continue
        except OSError as exc:
            findings.append(f"unreadable: {rel} ({type(exc).__name__})")
            continue
        if stat.S_ISLNK(st.st_mode):
            findings.append(f"symlink instead of a regular file: {rel}")
        elif not stat.S_ISREG(st.st_mode):
            findings.append(f"not a regular file: {rel}")
        else:
            try:
                digest = _sha(p)
            except OSError as exc:
                findings.append(f"unreadable: {rel} ({type(exc).__name__})")
                continue
            if digest != f["sha256"]:
                findings.append(f"changed: {rel}")
            if stat.S_IMODE(st.st_mode) != f["mode"]:
                findings.append(f"mode changed: {rel} ({oct(stat.S_IMODE(st.st_mode))} != {oct(f['mode'])})")
    for rel in scope(plugin):
        if rel not in want:
            findings.append(f"unexpected extra file: {rel}")
    return findings


def snapshot(root):
    """Everything under root as (relpath, kind, mode, sha256-or-link-target): used to verify a restore."""
    out = []
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(base, name)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            if stat.S_ISLNK(st.st_mode):
                out.append((rel, "link", 0, os.readlink(p)))
            elif stat.S_ISDIR(st.st_mode):
                out.append((rel, "dir", stat.S_IMODE(st.st_mode), ""))
            else:
                out.append((rel, "file", stat.S_IMODE(st.st_mode), _sha(p)))
    return sorted(out)


def _plain_dir(path, what):
    if os.path.islink(path):
        raise Refused(f"{what} is a symlink: {path}")
    if os.path.lexists(path) and not os.path.isdir(path):
        raise Refused(f"{what} is not a directory: {path}")


def _replace_tree(src, dst):
    if os.path.lexists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, copy_function=shutil.copy2)


def install(src, dst, manifest):
    """Stage a copy next to dst, check it THERE, then swap by rename: a copy error or a failed check leaves dst
    untouched (nothing to restore), and a backup of the old tree is kept."""
    _plain_dir(src, "--from")
    _plain_dir(dst, "--to")
    if not os.path.isdir(src):
        raise Refused(f"--from is not a directory: {src}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = dst.rstrip("/")
    staging, backup, old = f"{base}.new-{stamp}", None, f"{base}.old-{stamp}"
    try:
        shutil.copytree(src, staging, symlinks=True, copy_function=shutil.copy2)
        findings = check_tree(staging, manifest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if findings:
        shutil.rmtree(staging, ignore_errors=True)
        print("install failed the integrity check; target left untouched", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        return 1
    if os.path.isdir(dst):
        backup = f"{base}.bak-{stamp}"
        try:
            shutil.copytree(dst, backup, symlinks=True, copy_function=shutil.copy2)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
            raise
        os.rename(dst, old)
    try:
        os.rename(staging, dst)
    except BaseException:
        if backup:
            os.rename(old, dst)  # put the original back
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if backup:
        shutil.rmtree(old, ignore_errors=True)
    print(f"installed {dst}" + (f"; backup {backup}" if backup else ""))
    return 0


def rollback(dst, backup):
    _plain_dir(dst, "--to")
    if os.path.islink(backup) or not os.path.isdir(backup):
        raise Refused(f"--backup is missing, not a directory or a symlink: {backup}")
    want = snapshot(backup)
    _replace_tree(backup, dst)
    if snapshot(dst) != want:
        print(f"ROLLBACK VERIFICATION FAILED: {dst} differs from {backup}", file=sys.stderr)
        return 1
    print(f"restored {dst} from {backup}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="integrity.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--plugin", required=True)
    m.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--plugin", required=True)
    c.add_argument("--manifest", required=True)
    i = sub.add_parser("install")
    i.add_argument("--from", dest="src", required=True)
    i.add_argument("--to", required=True)
    i.add_argument("--manifest", required=True)
    r = sub.add_parser("rollback")
    r.add_argument("--to", required=True)
    r.add_argument("--backup", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if a.cmd == "manifest":
            doc = make_manifest(a.plugin)
            tmp = a.out + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(doc, fh, indent=1)
                fh.write("\n")
            os.replace(tmp, a.out)
            print(f"manifest of {len(doc['files'])} files: {a.out}")
            return 0
        if a.cmd == "check":
            _plain_dir(a.plugin, "--plugin")
            findings = check_tree(a.plugin, load_manifest(a.manifest))
            for f in findings:
                print(f)
            print("integrity: FAIL" if findings else "integrity: ok")
            return 1 if findings else 0
        if a.cmd == "install":
            return install(a.src, a.to, load_manifest(a.manifest))
        return rollback(a.to, a.backup)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
