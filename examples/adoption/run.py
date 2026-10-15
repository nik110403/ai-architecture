#!/usr/bin/env python3
"""AUTHORED FIXTURE: copy selective tools and exercise recipient-owned artifacts."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NEXT = "write adoption-summary.json from verified inputs.json"
BUNDLES = {
    "discovery": (
        "SKILL.md",
        "DEPENDENCIES.md",
        "memory-check-output.md",
        "fixtures",
        "scripts",
    ),
    "maintenance": ("SKILL.md", "DEPENDENCIES.md", "maintenance.md", "scripts"),
    "evaluation": (
        "SKILL.md",
        "DEPENDENCIES.md",
        "measurement.md",
        "receipt-report.md",
        "evaluation-report.md",
        "scripts",
    ),
    "continuity": ("SKILL.md", "resume.md", "continuity-check.md", "scripts"),
}


def run(*command):
    return subprocess.run(
        command, check=True, capture_output=True, text=True, encoding="utf-8"
    )


def copy_bundle(recipient, name):
    source = ROOT / "skills" / name
    destination = recipient / ".claude" / "skills" / name
    destination.mkdir(parents=True)
    for item in BUNDLES[name]:
        origin, target = source / item, destination / item
        if not origin.exists():
            raise ValueError(f"declared selective asset is missing: {name}/{item}")
        shutil.copytree(origin, target) if origin.is_dir() else shutil.copy2(
            origin, target
        )


def write_artifacts(recipient):
    artifacts = recipient / "artifacts"
    memory = artifacts / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [note](note.md)\n", encoding="utf-8")
    (memory / "note.md").write_text("[[note]]\n2026-10-15\n", encoding="utf-8")
    (artifacts / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "loops": [
                    {"id": "authored-loop", "adapter": "manual", "status": "live"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "adapters.json").write_text(
        json.dumps(
            {"schema_version": 1, "adapters": [{"id": "manual", "state": "active"}]}
        ),
        encoding="utf-8",
    )
    (artifacts / "receipts.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "authored-adoption",
                "source": "authored-fixture",
                "harness": "not-run",
                "role": "not-an-agent",
                "model": "not-invoked",
                "verify": "none",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    project = artifacts / "project"
    project.mkdir()
    (project / "inputs.json").write_text("[2, 3, 5]\n", encoding="utf-8")
    tracker = artifacts / "TRACKER.md"
    tracker.write_text(
        "## Resume here\n\n- **Next:** inspect authored input\n", encoding="utf-8"
    )
    tracker_info = tracker.stat()
    (artifacts / "continuity-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sha256": hashlib.sha256(tracker.read_bytes()).hexdigest(),
                "mtime_ns": tracker_info.st_mtime_ns,
                "edit_observed": True,
                "active_jobs": [],
                "arrivals": ["authored-check"],
                "banked_arrivals": ["authored-check"],
                "usage_known": True,
            }
        ),
        encoding="utf-8",
    )
    return artifacts, project


def journal(tool, project, command, *args):
    return json.loads(
        run(
            sys.executable, "-B", str(tool), command, "--target", str(project), *args
        ).stdout
    )


def fixed_next(project, journal_state):
    checkpoint = journal_state["journal"]["checkpoint"]
    if not journal_state["ready"] or checkpoint["next"] != NEXT:
        raise ValueError("fixed adoption checkpoint is not ready")
    values = json.loads((project / "inputs.json").read_text(encoding="utf-8"))
    if values != [2, 3, 5]:
        raise ValueError("unexpected authored input")
    (project / "adoption-summary.json").write_text(
        json.dumps({"total": sum(values), "executed_fixed_next": NEXT}),
        encoding="utf-8",
    )


def adopt(recipient):
    recipient.mkdir(parents=True, exist_ok=False)
    for name in BUNDLES:
        copy_bundle(recipient, name)
    artifacts, project = write_artifacts(recipient)
    skills = recipient / ".claude" / "skills"
    memory = run(
        sys.executable,
        "-B",
        str(skills / "discovery/scripts/memory_check.py"),
        "--root",
        str(artifacts / "memory"),
        "--format",
        "json",
    )
    loops = run(
        sys.executable,
        "-B",
        str(skills / "maintenance/scripts/loop_check.py"),
        "--registry",
        str(artifacts / "registry.json"),
        "--adapters",
        str(artifacts / "adapters.json"),
        "--format",
        "json",
    )
    receipts = run(
        sys.executable,
        "-B",
        str(skills / "evaluation/scripts/receipt_report.py"),
        "--input",
        str(artifacts / "receipts.jsonl"),
        "--format",
        "json",
    )
    evaluation = run(
        sys.executable,
        "-B",
        str(skills / "evaluation/scripts/evaluation_report.py"),
        "--input",
        str(artifacts / "receipts.jsonl"),
        "--format",
        "json",
    )
    continuity = run(
        sys.executable,
        "-B",
        str(skills / "continuity/scripts/continuity_check.py"),
        "--tracker",
        str(artifacts / "TRACKER.md"),
        "--receipt",
        str(artifacts / "continuity-receipt.json"),
        "--format",
        "json",
    )
    state = journal(
        skills / "continuity/scripts/journal.py",
        project,
        "start",
        "--job",
        "authored-check",
    )
    if state["ready"]:
        raise ValueError("new journal unexpectedly ready")
    journal(
        skills / "continuity/scripts/journal.py",
        project,
        "finish",
        "--job",
        "authored-check",
        "--result",
        "inputs.json",
    )
    state = journal(
        skills / "continuity/scripts/journal.py",
        project,
        "checkpoint",
        "--state",
        "authored inputs checked",
        "--evidence",
        "inputs.json",
        "--next",
        NEXT,
    )
    fixed_next(project, state)
    return {
        "memory_status": json.loads(memory.stdout)["status"],
        "loop_complete": json.loads(loops.stdout)["complete"],
        "receipt_runs": json.loads(receipts.stdout)["groups"][0]["runs"],
        "evaluation_evidence": json.loads(evaluation.stdout)["evidence"],
        "continuity_status": json.loads(continuity.stdout)["status"],
        "summary": json.loads(
            (project / "adoption-summary.json").read_text(encoding="utf-8")
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", type=Path)
    args = parser.parse_args()
    if args.recipient:
        report = adopt(args.recipient)
    else:
        with tempfile.TemporaryDirectory(prefix="adoption-fixture-") as raw:
            report = adopt(Path(raw) / "recipient")
    if report != {
        "memory_status": "complete",
        "loop_complete": True,
        "receipt_runs": 1,
        "evaluation_evidence": "observed",
        "continuity_status": "CONSISTENT",
        "summary": {"total": 10, "executed_fixed_next": NEXT},
    }:
        raise ValueError("adoption report mismatch")
    print(
        "adoption fixture: PASS (authored artifacts; no agent or native harness invoked)"
    )


if __name__ == "__main__":
    main()
