#!/usr/bin/env python3
"""Report-only integrity check for one explicit Markdown memory bundle."""

import argparse
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path, PurePosixPath

WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
INDEX_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
PATH_TOKEN = re.compile(r"(?<!\S)(/[^\s`'\"<>()[\]{}|,;]+)")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--root", required=True, help="explicit Markdown bundle directory"
    )
    result.add_argument(
        "--path-root", help="optional in-root prefix for literal path checks"
    )
    result.add_argument(
        "--stale-before",
        default="2026-04-15",
        help="ISO date cutoff (default: 2026-04-15)",
    )
    result.add_argument("--format", choices=("text", "json"), default="text")
    result.add_argument(
        "--strict", action="store_true", help="exit 1 if drift is found"
    )
    return result


def controlled(message):
    print(f"memory-check: invalid input: {message}", file=sys.stderr)
    return 2


def resolve_root(value, label):
    path = Path(value)
    if not path.exists():
        raise ValueError(f"{label} does not exist: {value}")
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is not a non-symlink directory: {value}")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"cannot resolve {label}: {error}") from error


def relative(root, path):
    return path.relative_to(root).as_posix()


def inside(root, path):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def add(findings, severity, check, path, detail):
    findings.append(
        {"severity": severity, "check": check, "path": path, "detail": detail}
    )


def safe_text(root, path, findings):
    label = relative(root, path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        add(findings, "unsupported", "encoding", label, "not UTF-8")
    except OSError as error:
        add(findings, "inaccessible", "read", label, str(error))
    return None


def content_without_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + 5 :]
    return text


def lexical_parts(base, raw):
    """Return root-relative components or None without resolving filesystem paths."""
    if not raw or raw.startswith("/") or "\\" in raw:
        return None
    parts = list(base)
    for part in PurePosixPath(raw).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return parts


def inspect_components(root, parts):
    """lstat root-relative components; never follow a symlink or inspect below one."""
    if not parts:
        return "present", (root, os.lstat(root).st_mode)
    candidate = root
    for position, part in enumerate(parts):
        candidate /= part
        try:
            mode = os.lstat(candidate).st_mode
        except FileNotFoundError:
            return "missing", candidate
        except OSError as error:
            return "inaccessible", (candidate, str(error))
        if stat.S_ISLNK(mode):
            return "symlink", candidate
        if position != len(parts) - 1 and not stat.S_ISDIR(mode):
            return "missing", candidate
    return "present", (candidate, mode)


def index_links(text, findings):
    """Return supported index targets and surface incomplete simple link constructs."""
    links = []
    for number, line in enumerate(text.splitlines(), 1):
        position = 0
        while (start := line.find("](", position)) >= 0:
            opening, closing = line.rfind("[", 0, start), line.find(")", start + 2)
            if opening < 0 or closing < 0:
                add(
                    findings,
                    "unsupported",
                    "malformed-index-link",
                    "MEMORY.md",
                    f"line {number}",
                )
                position = start + 2
                continue
            raw = line[start + 2 : closing]
            target = raw.split("#", 1)[0]
            if (
                not target
                or any(character.isspace() for character in raw)
                or not target.endswith(".md")
            ):
                add(
                    findings, "unsupported", "index-link", "MEMORY.md", f"line {number}"
                )
            else:
                links.append(raw)
            position = closing + 1
    return links


def scan(root, path_root, cutoff):
    findings, files, texts = [], [], {}

    def walk_error(error):
        location = Path(error.filename) if error.filename else root
        label = relative(root, location) if inside(root, location) else "."
        add(findings, "inaccessible", "walk", label, str(error))

    for current, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        current_path = Path(current)
        kept = []
        for name in sorted(dirs):
            child = current_path / name
            if child.is_symlink():
                add(
                    findings,
                    "unsupported",
                    "symlink-directory",
                    relative(root, child),
                    "not traversed",
                )
            elif "sync-conflict" not in name:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = current_path / name
            if "sync-conflict" in name or not name.endswith(".md"):
                continue
            if path.is_symlink():
                add(
                    findings,
                    "unsupported",
                    "symlink-file",
                    relative(root, path),
                    "not opened",
                )
                continue
            if not path.is_file():
                add(
                    findings,
                    "unsupported",
                    "non-regular",
                    relative(root, path),
                    "not opened",
                )
                continue
            text = safe_text(root, path, findings)
            if text is not None:
                files.append(path)
                texts[path] = text
    files.sort(key=lambda value: relative(root, value))
    index = root / "MEMORY.md"
    content = [path for path in files if path != index]
    if index not in texts:
        add(findings, "inaccessible", "index", "MEMORY.md", "missing or unreadable")
    indexed = set()
    if index in texts:
        index_base = list(index.parent.relative_to(root).parts)
        for raw in index_links(texts[index], findings):
            parts = lexical_parts(index_base, raw.split("#", 1)[0])
            if parts is None:
                add(findings, "unsupported", "index-target", "MEMORY.md", raw)
                continue
            state, detail = inspect_components(root, parts)
            if state == "symlink":
                add(findings, "unsupported", "index-target", "MEMORY.md", raw)
            elif state == "inaccessible":
                add(findings, "inaccessible", "index-target", "MEMORY.md", detail[1])
            elif state == "missing" or not stat.S_ISREG(detail[1]):
                add(findings, "drift", "missing-index-target", "MEMORY.md", raw)
            else:
                indexed.add(detail[0])
        for number, line in enumerate(texts[index].splitlines(), 1):
            if len(line) > 120:
                add(
                    findings,
                    "info",
                    "long-index-line",
                    "MEMORY.md",
                    f"line {number}: {len(line)} chars",
                )
    for path in content:
        if path not in indexed:
            add(
                findings,
                "drift",
                "unindexed-file",
                relative(root, path),
                "not linked by MEMORY.md",
            )
    outbound = {path: 0 for path in content}
    inbound = {path: 0 for path in content}
    for path in content:
        text = content_without_frontmatter(texts[path])
        for link in sorted(set(WIKILINK.findall(text))):
            if "/" in link or "#" in link or "|" in link or not link:
                add(findings, "unsupported", "wikilink", relative(root, path), link)
                continue
            outbound[path] += 1
            parts = lexical_parts(
                list(path.parent.relative_to(root).parts), f"{link}.md"
            )
            state, detail = inspect_components(root, parts)
            if state == "present" and stat.S_ISREG(detail[1]) and detail[0] in inbound:
                inbound[detail[0]] += 1
            elif state == "symlink":
                add(
                    findings,
                    "unsupported",
                    "wikilink-target",
                    relative(root, path),
                    f"[[{link}]]",
                )
            elif state == "inaccessible":
                add(
                    findings,
                    "inaccessible",
                    "wikilink-target",
                    relative(root, path),
                    detail[1],
                )
            else:
                add(
                    findings,
                    "info",
                    "dangling-wikilink",
                    relative(root, path),
                    f"[[{link}]]",
                )
        for token in sorted(set(DATE.findall(text))):
            try:
                if date.fromisoformat(token) < cutoff:
                    add(
                        findings,
                        "info",
                        "stale-explicit-date",
                        relative(root, path),
                        token,
                    )
            except ValueError:
                pass
        if path_root:
            prefix = str(path_root)
            for token in sorted(set(PATH_TOKEN.findall(text))):
                if token == prefix or token.startswith(prefix + "/"):
                    relative_token = token.removeprefix(prefix).lstrip("/")
                    parts = lexical_parts(
                        list(path_root.relative_to(root).parts), relative_token
                    )
                    if parts is None:
                        add(
                            findings,
                            "unsupported",
                            "path-reference",
                            relative(root, path),
                            token,
                        )
                        continue
                    state, detail = inspect_components(root, parts)
                    if state == "symlink":
                        add(
                            findings,
                            "unsupported",
                            "path-reference",
                            relative(root, path),
                            token,
                        )
                    elif state == "inaccessible":
                        add(
                            findings,
                            "inaccessible",
                            "path-reference",
                            relative(root, path),
                            detail[1],
                        )
                    elif state == "missing":
                        add(
                            findings,
                            "info",
                            "missing-path-reference",
                            relative(root, path),
                            token,
                        )
    for path in content:
        if not outbound[path] and not inbound[path]:
            add(
                findings,
                "info",
                "isolated-file",
                relative(root, path),
                "no inbound or outbound wikilinks",
            )
    findings.sort(
        key=lambda item: (item["severity"], item["check"], item["path"], item["detail"])
    )
    counts = {
        key: sum(item["severity"] == key for item in findings)
        for key in ("drift", "info", "unsupported", "inaccessible")
    }
    return {
        "status": "incomplete"
        if counts["unsupported"] or counts["inaccessible"]
        else "complete",
        "root": str(root),
        "files": len(content),
        "counts": counts,
        "findings": findings,
    }


def emit(report, output):
    if output == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(f"memory-check: {report['status']}")
    print(f"root: {report['root']}")
    print(f"files: {report['files']}")
    for kind, count in report["counts"].items():
        print(f"{kind}: {count}")
    for item in report["findings"]:
        print("{severity} | {check} | {path} | {detail}".format(**item))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cutoff = date.fromisoformat(args.stale_before)
        root = resolve_root(args.root, "root")
        path_root = None
        if args.path_root:
            raw_path_root = Path(os.path.abspath(args.path_root))
            if not inside(root, raw_path_root):
                raise ValueError("path root must be the root or a descendant of it")
            state, detail = inspect_components(
                root, list(raw_path_root.relative_to(root).parts)
            )
            if state != "present" or not stat.S_ISDIR(detail[1]):
                raise ValueError(
                    "path root must be an existing in-root non-symlink directory"
                )
            path_root = detail[0]
    except ValueError as error:
        return controlled(str(error))
    report = scan(root, path_root, cutoff)
    emit(report, args.format)
    return 1 if args.strict and report["counts"]["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
