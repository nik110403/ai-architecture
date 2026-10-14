#!/usr/bin/env python3
"""Manual project-local continuity journal; Python 3.11+, stdlib only."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

LOCK_WAIT = 2.0


class JournalError(Exception):
    pass


def no_redirects(path):
    for current in reversed((path, *path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise JournalError("redirected target or managed ancestor")


def journal_path(raw):
    if ".." in Path(raw).parts:
        raise JournalError("target must not contain '..' components")
    target = Path(os.path.abspath(raw))
    no_redirects(target)
    if not target.is_dir():
        raise JournalError("target must be an existing directory")
    path = target / ".ai-continuity" / "journal.json"
    no_redirects(path)
    if path.exists() and not path.is_file():
        raise JournalError("journal must be a regular file")
    return path


def text(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value):
    return type(value) is int and value >= 0


def require(condition):
    if not condition:
        raise JournalError("malformed or unsupported journal schema")


def validate(data):
    require(
        isinstance(data, dict)
        and set(data) == {"schema", "revision", "jobs", "checkpoint"}
    )
    require(type(data["schema"]) is int and data["schema"] == 1)
    revision = data["revision"]
    require(integer(revision) and isinstance(data["jobs"], list))
    ids, events = set(), set()
    for job in data["jobs"]:
        require(
            isinstance(job, dict)
            and set(job)
            == {
                "id",
                "status",
                "result",
                "started_revision",
                "completed_revision",
            }
        )
        require(text(job["id"]) and job["id"] not in ids)
        ids.add(job["id"])
        started, completed = job["started_revision"], job["completed_revision"]
        require(integer(started) and 0 < started <= revision and started not in events)
        events.add(started)
        require(job["status"] in ("active", "completed"))
        if job["status"] == "active":
            require(completed is None and job["result"] is None)
        else:
            require(
                text(job["result"])
                and integer(completed)
                and started < completed <= revision
                and completed not in events
            )
            events.add(completed)
    cp = data["checkpoint"]
    if cp is not None:
        require(
            isinstance(cp, dict)
            and set(cp)
            == {
                "revision",
                "state",
                "evidence",
                "next",
                "covered_arrivals",
                "active_jobs",
            }
        )
        require(
            integer(cp["revision"])
            and 0 < cp["revision"] <= revision
            and cp["revision"] not in events
        )
        require(text(cp["state"]) and text(cp["next"]))
        require(
            isinstance(cp["evidence"], list)
            and bool(cp["evidence"])
            and all(text(ref) for ref in cp["evidence"])
        )
        covered, active = snapshot(data, cp["revision"])
        require(cp["covered_arrivals"] == covered and cp["active_jobs"] == active)
    return data


def snapshot(data, revision):
    covered, active = [], []
    for job in data["jobs"]:
        if job["started_revision"] < revision:
            completed = job["completed_revision"]
            if completed is not None and completed < revision:
                covered.append(job["id"])
            else:
                active.append(job["id"])
    return covered, active


def unique_keys(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out)
        out[key] = value
    return out


def load(path):
    no_redirects(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return validate(json.loads(raw, object_pairs_hook=unique_keys))
    except (ValueError, RecursionError) as error:
        raise JournalError("malformed or unsupported journal schema") from error


def resume(data):
    if data is None:
        return {"ready": False, "journal": None}
    validate(data)
    cp = data["checkpoint"]
    ready = bool(
        cp
        and cp["revision"] == data["revision"]
        and not any(job["status"] == "active" for job in data["jobs"])
        and cp["covered_arrivals"]
        == [job["id"] for job in data["jobs"] if job["status"] == "completed"]
    )
    return {"ready": ready, "journal": data}


def same_file(path, identity):
    try:
        no_redirects(path)
        current = path.stat()
        return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
    except (OSError, JournalError):
        return False


@contextmanager
def locked(path):
    no_redirects(path)
    path.parent.mkdir(exist_ok=True)
    lock = path.with_name("journal.lock")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        no_redirects(lock)
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise JournalError(
                    "journal lock exists; bounded wait expired; never displaced"
                )
            time.sleep(0.025)
    identity = os.fstat(fd)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(fd)
        if same_file(lock, identity):
            lock.unlink()


def atomic(path, data):
    no_redirects(path)
    encoded = (
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    temp = Path(name)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        no_redirects(path)
        os.replace(temp, path)
    finally:
        if same_file(temp, identity):
            temp.unlink()


def mutate(path, args):
    with locked(path):
        data = load(path)
        if data is None:
            data = {"schema": 1, "revision": 0, "jobs": [], "checkpoint": None}
        revision = data["revision"] + 1
        if args.command == "start":
            if not text(args.job) or any(job["id"] == args.job for job in data["jobs"]):
                raise JournalError("invalid or lifetime-duplicate job ID")
            data["jobs"].append(
                {
                    "id": args.job,
                    "status": "active",
                    "result": None,
                    "started_revision": revision,
                    "completed_revision": None,
                }
            )
        elif args.command == "finish":
            job = next((job for job in data["jobs"] if job["id"] == args.job), None)
            if job is None or job["status"] != "active" or not text(args.result):
                raise JournalError("unknown/completed job or empty result reference")
            job.update(
                status="completed", result=args.result, completed_revision=revision
            )
        else:
            if (
                not text(args.state)
                or not text(args.next)
                or not all(text(ref) for ref in args.evidence)
            ):
                raise JournalError("checkpoint needs nonempty state, evidence and Next")
            covered, active = snapshot(data, revision)
            data["checkpoint"] = {
                "revision": revision,
                "state": args.state,
                "evidence": args.evidence,
                "next": args.next,
                "covered_arrivals": covered,
                "active_jobs": active,
            }
        data["revision"] = revision
        validate(data)
        atomic(path, data)
        return resume(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "finish", "checkpoint", "resume"):
        sub = commands.add_parser(command)
        sub.add_argument("--target", required=True)
        if command in ("start", "finish"):
            sub.add_argument("--job", required=True)
        if command == "finish":
            sub.add_argument("--result", required=True)
        if command == "checkpoint":
            sub.add_argument("--state", required=True)
            sub.add_argument("--evidence", action="append", required=True)
            sub.add_argument("--next", required=True)
        if command == "resume":
            sub.add_argument(
                "--json", action="store_true", help="JSON output (also the default)"
            )
    args = parser.parse_args(argv)
    try:
        path = journal_path(args.target)
        result = resume(load(path)) if args.command == "resume" else mutate(path, args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (JournalError, OSError, UnicodeError, ValueError, RecursionError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
