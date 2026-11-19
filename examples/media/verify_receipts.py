#!/usr/bin/env python3
"""Re-inspect a media receipts table instead of trusting it.

Usage: verify_receipts.py [OUT_DIR] [--keep DIR] [--only KIND,...] [--specs DIR] [--format text|json]
With no OUT_DIR the example is run first (into --keep DIR, or a temp dir that is removed afterwards). Structure comes first and
needs no browser: all seven rows, reasons from the closed vocabulary, fixture rows never EXERCISED, artifact sha256 values.
Then every EXERCISED artifact is inspected again from its bytes, the spec and, for the recap, the repository; the recorded
check results are not used. Prints the seven-row table and the renderer rows. Exit 0 ok, 1 tampered or failed, 2 invalid, 3 unavailable."""
import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load_media():
    spec = importlib.util.spec_from_file_location("aiarch_media_main", ROOT / "modules" / "media" / "media.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["aiarch_media_main"] = module
    spec.loader.exec_module(module)
    return module


def table(doc):
    lines = [f"{r['id']} {r['status']}" + (f" {r['blocker_class']}: {r['reason']}" if r["status"] == "BLOCKED" else "") for r in doc["rows"]]
    lines += [f"{r['id']} {r['status']}" for r in doc["renderers"]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", nargs="?")
    parser.add_argument("--keep")
    parser.add_argument("--only")
    parser.add_argument("--specs")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    media = load_media()
    temp = None
    out = args.out
    if not out:
        out = args.keep or (temp := tempfile.mkdtemp(prefix="media-verify-"))
        ran = subprocess.run([sys.executable, "-B", str(HERE / "run.py"), "--offline", "--out", out, "--format", "json"],
                             capture_output=True, text=True)
        if ran.returncode != 0:
            print(ran.stdout or ran.stderr)
            return ran.returncode
    try:
        doc = media.verify(out, only=[k for k in (args.only or "").split(",") if k] or None, specs_dir=args.specs)
        code = 0
    except media.Exit as error:
        doc, code = {"status": media.common.STATUS[error.code], "reason": error.reason, **error.extra}, error.code
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    print(json.dumps(doc, sort_keys=True) if args.format == "json" else (table(doc) if code == 0 else f"{doc['status']}: {doc['reason']}"))
    return code


if __name__ == "__main__":
    sys.exit(main())
