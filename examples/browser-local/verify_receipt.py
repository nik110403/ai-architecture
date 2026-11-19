#!/usr/bin/env python3
"""Run the lifecycle journey and verify every receipt it produces, or verify one receipt with --receipt R.

Bare run: a fixture-backend journey always; a real headless Chrome journey too when Chrome is available. Prints
`real_backend: ran` or `real_backend: NOT RUN (<reason>)`. Exit 0 when every receipt verifies; 1 otherwise, and 1 when
AIARCH_REQUIRE_BROWSER=1 and the real backend did not run. 127.0.0.1 only; a real model is never launched.
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "modules" / "browser"))
sys.path.insert(0, str(HERE.parents[1] / "modules" / "browser-operations"))
import browser  # noqa: E402
import chrome  # noqa: E402
import lifecycle  # noqa: E402


def verify(receipt):
    o, e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        code = browser.main(["verify-receipt", str(receipt)])
    return code, (o.getvalue() or e.getvalue()).strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--receipt", help="verify this receipt only (its approval chain must sit beside it)")
    args = ap.parse_args(argv)
    if args.receipt:
        code, msg = verify(args.receipt)
        print(msg)
        return 1 if code else 0
    require = os.environ.get("AIARCH_REQUIRE_BROWSER") == "1"
    real = "NOT RUN (no Chrome on PATH and AIARCH_CHROME unset)"
    rc = 0
    with tempfile.TemporaryDirectory() as tmp:
        for backend in ("fixture", "chrome"):
            if backend == "chrome" and not chrome.find_chrome():
                break
            out = Path(tmp) / backend
            receipt, posts, err = lifecycle.journey(backend, out)
            if receipt is None:
                if backend == "chrome":
                    real = f"NOT RUN (Chrome found but the journey stopped: {err[:160]})"
                    break
                print(f"{backend} journey stopped: {err}")
                return 1
            code, msg = verify(receipt)
            print(f"{backend} receipt: {msg}; server saw {len(posts)} submission(s)")
            if code or len(posts) != 1:
                rc = 1
            if backend == "chrome":
                real = "ran"
    print(f"real_backend: {real}")
    if require and real != "ran":
        print("AIARCH_REQUIRE_BROWSER=1: the real backend did not run", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
