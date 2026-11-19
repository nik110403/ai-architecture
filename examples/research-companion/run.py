#!/usr/bin/env python3
"""Offline demo of the research-adapter against a provenote companion checkout.

    python3 run.py --offline [--companion DIR --out DIR] [--brief FILE] [--fixture FILE]

--offline is required: the demo only ever serves the canned responses in fixture.json and
never opens a socket. Without --companion it reports that the companion is unavailable and
exits 0; the RESEARCH_COMPANION_DIR variable used by the test suite is not read here.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent.parent / "modules" / "research-adapter" / "research_adapter.py"


def load_adapter():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("research_adapter", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", required=True, help="serve fixture.json only")
    ap.add_argument("--companion", metavar="DIR")
    ap.add_argument("--out", metavar="DIR", help="required with --companion; everything is written here")
    ap.add_argument("--brief", default=str(HERE / "brief.json"))
    ap.add_argument("--fixture", default=str(HERE / "fixture.json"))
    args = ap.parse_args(argv)
    adapter = load_adapter()
    try:
        status = adapter.handshake(args.companion)
    except adapter.Exit as stop:
        print(f"research companion: refused ({stop.message})", file=sys.stderr)
        return 2
    if not status["integrated"]:
        caps = "; ".join(f"{k}: {v}" for k, v in status["capabilities"].items())
        print(f"research companion: unavailable ({caps}) - research-adapter NOT integrated")
        return 0
    if not args.out:
        print("run.py: --out is required with --companion", file=sys.stderr)
        return 2
    info = status["companion"]
    print(f"research companion: {info['name']} {info['version']} (integrated)")
    out = Path(args.out)
    code = adapter.main(["search", "--companion", args.companion, "--brief", args.brief,
                         "--out", args.out, "--fixture", args.fixture])
    if code != 0:
        return code
    index = json.loads((out / "notes" / "index.json").read_text(encoding="utf-8"))
    dois = [n["doi"] for n in index["notes"] if n["doi"]][:2]
    if dois:
        (out / "paper.md").write_text("Draft placeholder.\n", encoding="utf-8")
        adapter.main(["cite", "--companion", args.companion, "--dois", *dois,
                      "--bib", str(out / "references.bib"), "--paper", str(out / "paper.md"),
                      "--out", args.out, "--fixture", args.fixture])
    return 0


if __name__ == "__main__":
    sys.exit(main())
