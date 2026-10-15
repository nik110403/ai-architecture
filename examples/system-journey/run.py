#!/usr/bin/env python3
"""AUTHORED FIXTURE: route → brief → result → independent check → recovery."""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from route import route

NEXT = "inspect fixture-result.json with the documented verifier"


def recover(target):
    recovered = json.loads((target / "journal.json").read_text(encoding="utf-8"))
    if not recovered["ready"] or recovered["next"] != NEXT:
        raise ValueError("fresh-process recovery failed")


def main():
    available = {
        "capabilities": ["code.builder", "code.verifier"],
        "models": ["builder", "verifier"],
    }
    report = route("fix code test", available)
    if report["status"] != "COMPLETE":
        raise ValueError(report)
    negative = route("system atlas", available)
    pending = route("fix code test", available, pending_jobs=("checker",))
    missing = route(
        "fix code test",
        {"capabilities": ["code.builder"], "models": ["builder", "verifier"]},
    )
    if (
        negative["status"] != "REFUSE"
        or pending["status"] != "REFUSE"
        or missing["status"] != "REFUSE"
    ):
        raise ValueError("negative route failed")
    with tempfile.TemporaryDirectory(prefix="system-journey-") as raw:
        root = Path(raw)
        (root / "route.json").write_text(
            json.dumps(report, sort_keys=True), encoding="utf-8"
        )
        (root / "brief.json").write_text(
            json.dumps(
                {
                    "objective": "sum fixture values",
                    "acceptance": "total is 10",
                    "scope": "fixture only",
                }
            ),
            encoding="utf-8",
        )
        (root / "fixture-result.json").write_text(
            json.dumps({"values": [2, 3, 5], "total": 10}), encoding="utf-8"
        )
        result = json.loads((root / "fixture-result.json").read_text(encoding="utf-8"))
        if sum(result["values"]) != result["total"]:
            raise ValueError("independent check failed")
        artifact = root / "fixture-result.json"
        journal = {
            "evidence_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "pending_jobs": [],
            "unknown_arrivals": False,
            "ready": True,
            "next": NEXT,
        }
        (root / "journal.json").write_text(
            json.dumps(journal, sort_keys=True), encoding="utf-8"
        )
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--recover", str(root)],
            check=True,
        )
    print(
        "system journey: PASS (happy route, inventory-needed/critical/pending refusals, fresh-process data-only Next)"
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--recover":
        recover(Path(sys.argv[2]))
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit("usage: run.py [--recover JOURNAL_DIRECTORY]")
