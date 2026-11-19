#!/usr/bin/env python3
"""Run the research-integrity lifecycle on the synthetic mini study and assert its integrity guarantees.

  python3 verify_receipt.py                       bare: no companion; asserts A1 (INCOMPLETE), A5 (stale
                                                  source after a check), A6, A7, lineage (a no-op script fails) and the receipt chain
  python3 verify_receipt.py --companion DIR       also reaches release PASS, asserts A5 after finalize and
                                                  shows A4 (a planted fake quote is refused)
  python3 verify_receipt.py --receipt FILE        verify the chain around one receipt file (exit 1 if edited)
AIARCH_REQUIRE_COMPANION=1 makes a missing --companion an error (exit 1). No network, no model call.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE.parents[1] / "modules" / "research-integrity" / "integrity.py"
MINI = HERE / "mini_study"
NOTE = "rivera2020synthetic"


class Failed(Exception):
    pass


def integrity(*args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], capture_output=True, text=True, env=env)
    return done.returncode, done.stdout + done.stderr


def expect(label, condition, detail=""):
    if not condition:
        raise Failed(f"{label}: {detail}".strip())
    print(f"{label}: ok")


def make_index(folder):
    (folder / "companion").mkdir(parents=True)
    receipt = folder / "companion" / "search.receipt.json"
    receipt.write_text(json.dumps({"status": "ok", "provider_status": {}}))
    (folder / "notes").mkdir()
    (folder / "notes" / "index.json").write_text(json.dumps({
        "companion": {"name": "provenote", "version": "0.2.x"},
        "companion_receipt": {"path": "companion/search.receipt.json",
                              "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()},
        "status": "ok", "provider_status": {}, "notes": []}))
    return folder / "notes" / "index.json"


def started(work, label, companion):
    """New project: init (new-project when a companion is given), mini study copied in, query, check."""
    project = work / label / "p"
    if companion:
        code, out = integrity("init", "--project", project, "--companion", companion)
        shutil.copytree(MINI, project, dirs_exist_ok=True)
    else:
        shutil.copytree(MINI, project)
        code, out = integrity("init", "--project", project)
    expect(f"{label} init", code == 0, out)
    return project


def checked(work, label, companion, plant=None):
    project = started(work, label, companion)
    if plant:
        plant(project)
    code, out = integrity("query", "--project", project, "--index", make_index(work / label / "index"))
    expect(f"{label} query", code == 0, out)
    extra = ["--companion", companion] if companion else []
    code, out = integrity("check", "--project", project, *extra)
    return project, code, out


def append(path, text):
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def run(companion):
    work = Path(tempfile.mkdtemp(prefix="ri-verify-"))
    try:
        # A1 and the receipt chain
        project, code, out = checked(work, "a1", companion)
        if companion:
            expect("check", code == 0, out)
            expect("finalize", integrity("finalize", "--project", project)[0] == 0)
            code, out = integrity("release", "--project", project, "--out", work / "a1" / "out")
            expect("release: PASS", code == 0 and (work / "a1" / "out" / "release-manifest.json").is_file(), out)
            print("release: PASS")
            project, code, out = checked(work, "a5", companion)
            expect("finalize", integrity("finalize", "--project", project)[0] == 0)
            append(project / "02_sources" / "md" / f"{NOTE}.md", "\nAn added sentence.\n")
            code, out = integrity("release", "--project", project, "--out", work / "a5" / "out")
            expect("A5 (after finalize): stale source refuses release",
                   code == 1 and f"stale: 02_sources/md/{NOTE}.md" in out, out)
        else:
            expect("A1: unavailable is incomplete",
                   code == 3 and "quotes: UNAVAILABLE: no companion given" in out
                   and "claims: UNAVAILABLE: no companion given" in out and "overall: INCOMPLETE" in out, out)
            expect("A1: finalize refuses INCOMPLETE", integrity("finalize", "--project", project)[0] == 3)
            expect("receipt chain verifies", integrity("verify-receipt", "--project", project)[0] == 0)
            project, code, out = checked(work, "a5", companion)
            append(project / "02_sources" / "md" / f"{NOTE}.md", "\nAn added sentence.\n")
            code, out = integrity("release", "--project", project, "--out", work / "a5" / "out")
            expect("A5 (bare): release is INCOMPLETE", code == 3, out)
            code, out = integrity("verify-receipt", "--project", project)
            expect("A5 (bare): verify-receipt reports the stale source",
                   code == 1 and f"stale: 02_sources/md/{NOTE}.md" in out, out)
        # A6
        project, code, out = checked(work, "a6", companion, lambda p: append(p / "10_paper" / "draft.md",
                                                                           "\nt(28) = 2.10, p = .001\n"))
        expect("A6: inconsistent p fails", code == 1 and "stats: FAIL" in out and "recomputed p" in out, out)
        project, code, out = checked(work, "a6b", companion, lambda p: append(p / "10_paper" / "draft.md",
                                                                            "\nr(30) = .41, p = .02\n"))
        expect("A6: unsupported grammar is not a pass", "stats: UNAVAILABLE: unsupported-grammar" in out, out)
        # A7
        project, code, out = checked(work, "a7", companion, lambda p: append(p / "10_paper" / "draft.md",
                                                                           "\nSee {{exhibit:ghost}}.\n"))
        expect("A7: unknown exhibit fails", code == 1 and "unknown exhibit: ghost" in out, out)
        project, code, out = checked(work, "a9", companion,
                                     lambda p: (p / "03_code" / "run_all.py").write_text("pass\n"))
        expect("lineage: a no-op script cannot claim the exhibits",
               code == 1 and "analysis: FAIL: missing output 06_tables/table1.csv" in out, out)
        if companion:
            def fake(p):
                note = p / "01_notes" / "lit" / f"{NOTE}.md"
                note.write_text(note.read_text(encoding="utf-8").replace(
                    "reach the target cell faster", "always arrive before every other agent"), encoding="utf-8")
            project, code, out = checked(work, "a4", companion, fake)
            released = integrity("release", "--project", project, "--out", work / "a4" / "out")[0]
            expect("A4: planted fake quote refused",
                   code == 1 and f"01_notes/lit/{NOTE}.md" in out and released != 0, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--companion", metavar="DIR")
    parser.add_argument("--receipt", metavar="FILE")
    args = parser.parse_args()
    if args.receipt:
        receipt = Path(args.receipt).resolve()
        code, out = integrity("verify-receipt", "--project", receipt.parent.parent.parent)
        print(out.strip())
        return code
    if args.companion:
        print("companion: given")
    elif os.environ.get("AIARCH_REQUIRE_COMPANION") == "1":
        print("companion: NOT RUN (required by AIARCH_REQUIRE_COMPANION=1 but --companion was not given)")
        return 1
    else:
        print("companion: NOT RUN (no --companion given; quote and claim checks stay UNAVAILABLE)")
    try:
        run(os.path.abspath(args.companion) if args.companion else None)
    except Failed as error:
        print(f"FAIL {error}")
        return 1
    print("verify_receipt: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
