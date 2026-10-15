#!/usr/bin/env python3
"""Produce a manual, project-local routing manifest; never dispatch or write."""

import argparse
import json
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import stat
import sys


COLUMNS = ["Task", "Room", "Read first", "Do not load", "Capabilities", "Output"]
DIR_FD_FUNCTIONS = (os.open, os.stat)
NOFOLLOW_STAT = os.stat


def cells(line):
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|") or "\\|" in line:
        raise ValueError(
            "unsupported routing row: require six columns, no escaped pipes"
        )
    parts = [part.strip() for part in line[1:-1].split("|")]
    if len(parts) != 6 or any(part.count("`") % 2 for part in parts):
        raise ValueError(
            "unsupported routing row: six columns, balanced backticks, no embedded pipes"
        )
    return parts


def routing_rows(text):
    # Keep line boundaries: an example must neither supply nor extend a table.
    lines = []
    fence = None
    for line in text.splitlines():
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*",
                line,
            ):
                fence = None
            lines.append("")
            continue
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker and not (marker[1][0] == "`" and "`" in marker[2]):
            fence = marker[1]
            lines.append("")
        else:
            lines.append(line)
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == "## Task routing"), None
    )
    if start is None:
        return []
    index = start + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index == len(lines) or not lines[index].lstrip().startswith("|"):
        return []
    if [cell.replace("`", "") for cell in cells(lines[index])] != COLUMNS:
        raise ValueError("unsupported routing header; expected " + " | ".join(COLUMNS))
    index += 1
    if index == len(lines) or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in cells(lines[index])
    ):
        raise ValueError("malformed routing separator")
    rows = []
    for line in lines[index + 1 :]:
        if not line.lstrip().startswith("|"):
            break
        row = cells(line)
        if not row[0] or not row[1]:
            raise ValueError("routing rows require Task and Room")
        rows.append(row)
    return rows


def items(cell):
    return [
        part.strip()
        for part in re.split(r"[,;]", cell.replace("`", ""))
        if part.strip()
    ]


def safe_parts(path):
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.endswith("/")
        or path.split("/")[-1] == "."
        or PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).drive
        or ".." in path.split("/")
    ):
        raise ValueError("unsafe project-relative target: " + repr(path))
    parts = PurePosixPath(path).parts
    if not parts:
        raise ValueError("directory target is unsupported: " + repr(path))
    return parts


def descriptor_supported():
    return (
        all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
        and all(
            callable(getattr(os, name, None))
            for name in ("open", "stat", "dup", "close", "fstat")
        )
        and all(func in getattr(os, "supports_dir_fd", ()) for func in DIR_FD_FUNCTIONS)
        and NOFOLLOW_STAT in getattr(os, "supports_follow_symlinks", ())
    )


def target_stat(root_fd, path):
    """Descriptor walk when available; otherwise a static, non-raceproof walk."""
    parts = safe_parts(path)
    if not isinstance(root_fd, int):
        if not callable(getattr(os, "lstat", None)):
            raise ValueError("platform lacks lstat for safe path inspection")
        current = root_fd
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode) or getattr(
                info, "st_file_attributes", 0
            ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError("symlink/reparse target is unsupported: " + repr(path))
            if index == len(parts) - 1:
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("non-regular target is unsupported: " + repr(path))
                return info
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("non-directory ancestor: " + repr(path))
        return None
    parent = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            try:
                info = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symlink target is unsupported: " + repr(path))
            if index == len(parts) - 1:
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("non-regular target is unsupported: " + repr(path))
                return info
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("non-directory ancestor: " + repr(path))
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
    finally:
        os.close(parent)


def compile_packet(root_fd, project, task, room, budget, stamp):
    instruction = target_stat(root_fd, "CLAUDE.md")
    text = ""
    if instruction is not None:
        if not callable(getattr(os, "fstat", None)):
            raise ValueError("platform lacks fstat for instruction file inspection")
        if isinstance(root_fd, int):
            fd = os.open(
                "CLAUDE.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd
            )
            source = os.fdopen(fd, "r", encoding="utf-8")
        else:
            source = open(os.path.join(root_fd, "CLAUDE.md"), "r", encoding="utf-8")
        with source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("CLAUDE.md must be a regular file")
            text = source.read()
    rows = routing_rows(text)
    selected = next((row for row in rows if row[0] == task), None)
    if selected is None and room is not None:
        selected = next((row for row in rows if row[1] == room), None)
    if rows and selected is None:
        raise ValueError("unknown task/room: no exact routing match")
    packet = {
        "schema_version": 1,
        "disposition": "ROUTED" if selected else "KERNEL_ONLY",
        "task": task,
        "project": project,
        "room": selected[1] if selected else None,
        "read": items(selected[2]) if selected else ["CLAUDE.md"],
        "exclude": items(selected[3]) if selected else [],
        "skills": [],
        "agent": [],
        "budget_tokens": budget,
        "expected_turns": None,
        "provenance": {"generator": "context_packet.py", "date": stamp},
    }
    if selected:
        for capability in items(selected[4]):
            if capability.lower().startswith("agent:"):
                name = capability[6:].strip()
                if not name:
                    raise ValueError("empty agent capability")
                packet["agent"].append(name)
            else:
                packet["skills"].append(capability)
    else:
        packet["note"] = "no task routing; kernel-only manual manifest"
    # Validate both columns before any target metadata lookup.
    targets = packet["read"] + packet["exclude"]
    for target in targets:
        safe_parts(target)
    metadata = {target: target_stat(root_fd, target) for target in targets}
    packet["missing_paths"] = list(
        dict.fromkeys(target for target in targets if metadata[target] is None)
    )
    packet["estimated_tokens"] = (
        sum(
            metadata[path].st_size
            for path in packet["read"]
            if metadata[path] is not None
        )
        // 4
    )
    packet["over_budget"] = packet["estimated_tokens"] > budget
    return packet


def nonnegative(value):
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError("budget must be a nonnegative integer")
    try:
        return int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("budget integer is too large") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="explicit project directory")
    parser.add_argument("--task", required=True)
    parser.add_argument("--room")
    parser.add_argument("--budget", type=nonnegative, default=4000)
    parser.add_argument("--stamp", help="literal provenance string; no clock lookup")
    args = parser.parse_args(argv)
    if not args.task.strip():
        parser.error("task must be nonempty")
    if not args.project.strip():
        parser.error("project must be an explicit directory")
    try:
        project = os.path.abspath(args.project)
        use_descriptor = descriptor_supported()
        if use_descriptor:
            root_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
        else:
            if not stat.S_ISDIR(os.stat(project).st_mode):
                raise ValueError("project must be a directory")
            root_fd = project
        try:
            packet = compile_packet(
                root_fd, project, args.task, args.room, args.budget, args.stamp
            )
        finally:
            if use_descriptor:
                os.close(root_fd)
        output = json.dumps(packet, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    except (OSError, ValueError, UnicodeError, NotImplementedError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 2
    if packet["over_budget"]:
        print(
            "WARN: estimated tokens exceed budget (warn-only; no truncation)",
            file=sys.stderr,
        )
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
