#!/usr/bin/env python3
"""Build or validate the deterministic preview archive (Python 3.11+)."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README.md",
    "INSTALL.md",
    "COVERAGE.md",
    "RELEASE.md",
    "LICENSE",
    "RUN.md",
    "docs/LICENSE.md",
    "modules/planning/README.md",
    "modules/agents/README.md",
    "modules/context/README.md",
    "modules/continuity/README.md",
    "modules/continuity/journal.py",
    "modules/review/README.md",
    "modules/evaluation/README.md",
    "modules/workspace/README.md",
    "modules/design/README.md",
    "modules/research/README.md",
    "modules/safety/README.md",
    "modules/maintenance/README.md",
    "examples/continuity/run.py",
    "examples/continuity/RUN.md",
    "scripts/package_preview.py",
)
GENERATED = "inventory.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_files() -> set[str]:
    found = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("dist/") or "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"symlink refused: {relative}")
        if path.is_file():
            found.add(relative)
    return found


def payload() -> dict[str, object]:
    actual = source_files()
    expected = set(FILES)
    if actual != expected:
        raise ValueError(
            f"source inventory mismatch: missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    hashes = {}
    for name in FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing or unsafe source: {name}")
        hashes[name] = digest(path.read_bytes())
    return {"format": 1, "files": hashes, "generated": [GENERATED]}


def write_archive(destination: Path) -> None:
    inventory = (
        json.dumps(payload(), ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n"
    )
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, data in [(name, (ROOT / name).read_bytes()) for name in FILES] + [
            (GENERATED, inventory)
        ]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = 3, (0o100644 << 16)
            archive.writestr(
                info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )


def check_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if names != set(FILES) | {GENERATED} or len(archive.namelist()) != len(names):
            raise ValueError("archive inventory mismatch")
        inventory = json.loads(archive.read(GENERATED))
        if inventory.get("format") != 1 or inventory.get("generated") != [GENERATED]:
            raise ValueError("invalid generated inventory")
        for name, expected in inventory.get("files", {}).items():
            if name not in FILES or digest(archive.read(name)) != expected:
                raise ValueError(f"hash mismatch: {name}")
        if set(inventory["files"]) != set(FILES):
            raise ValueError("incomplete inventory hashes")
        for info in archive.infolist():
            if (
                info.date_time != (1980, 1, 1, 0, 0, 0)
                or stat.S_IMODE(info.external_attr >> 16) != 0o644
            ):
                raise ValueError(f"non-neutral metadata: {info.filename}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    archive = args.check or ROOT / "dist" / "ai-architecture-preview.zip"
    if args.check:
        check_archive(archive)
    else:
        write_archive(archive)
        check_archive(archive)
        print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
