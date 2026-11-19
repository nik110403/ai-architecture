#!/usr/bin/env python3
"""Combined memory view: curated notes (modules/memory) next to episodic candidates (modules/memory-backends).

Offline and self-contained: it copies the fictional example notes into a temp (or --workdir) root, captures the
fixture session into a temp store, then prints the curated result verbatim and, in a separate section, the episodic
candidates. Curated output is produced by running curated.py as a subprocess and is never modified.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKENDS = ROOT / "modules" / "memory-backends"
sys.path.insert(0, str(BACKENDS))
import observe  # noqa: E402
import recall  # noqa: E402

CURATED = ROOT / "modules" / "memory" / "curated.py"
EXAMPLE_ROOT = ROOT / "modules" / "memory" / "example" / "root"
FIXTURE = BACKENDS / "fixtures" / "session.jsonl"
SCOPE = "/work/demo"


def episodic(work, q, no_backend):
    if no_backend:
        return {"state": "unavailable", "candidates": [], "abstained": True,
                "setup": "no episodic backend configured (--no-backend); curated memory is unaffected"}
    if not observe.probe_fts5():
        return {"state": "unavailable", "candidates": [], "abstained": True, "setup": recall.SETUP}
    store = work / "store"
    store.mkdir(mode=0o700)
    (store / "OPT-IN").write_text("")
    observe.capture(observe.build_parser().parse_args(
        ["capture", "--store", str(store), "--events", str(FIXTURE), "--scope", SCOPE]))
    return recall.query(recall.build_parser().parse_args(["query", "--store", str(store), "--q", q, "--offline"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="accepted for clarity; this example never goes online")
    parser.add_argument("--no-backend", action="store_true", help="show curated working with episodic unavailable")
    parser.add_argument("--q", default="sourdough starter feeding")
    parser.add_argument("--workdir", help="existing empty directory to build in and keep")
    args = parser.parse_args(argv)
    holder = None
    if args.workdir:
        work = Path(args.workdir)
    else:
        holder = tempfile.TemporaryDirectory()
        work = Path(holder.name)
    try:
        shutil.copytree(EXAMPLE_ROOT, work / "curated")
        proc = subprocess.run([sys.executable, "-B", str(CURATED), "query", "--root", f"notes={work / 'curated'}",
                               "--q", args.q, "--format", "json"], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin"})
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            return proc.returncode
        try:
            epi = episodic(work, args.q, args.no_backend)
        except (observe.Refuse, recall.Refuse) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return exc.code
        sys.stdout.write("## curated\n" + proc.stdout + "## episodic\n" + json.dumps(epi, sort_keys=True) + "\n")
        sys.stdout.flush()
        return 0
    finally:
        if holder:
            holder.cleanup()


if __name__ == "__main__":
    sys.exit(main())
