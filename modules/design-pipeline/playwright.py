#!/usr/bin/env python3
"""Playwright contracts and a spawn-gated adapter (stdlib only). Nothing here installs or downloads anything.

  status [--json]                           resolve the executable from PATH or an absolute AIARCH_PLAYWRIGHT; spawns nothing
  run --tree DIR --out DIR --allow-playwright   spawn `playwright test --reporter=json --trace=on --output DIR` in DIR
  trace ZIP                                 parse a trace archive (trace.trace and trace.network)

The outcome is one of NOT RUN, unverified, ran and never feeds the visual verdict: the rendered evidence comes from the
CDP renderer. Without --allow-playwright nothing is spawned and the exit code is 2.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ENV_VAR = "AIARCH_PLAYWRIGHT"
CLI_CONTRACT = "test --reporter=json --trace=on --output DIR"
TRACE_MEMBERS = ("trace.trace", "trace.network")
COMPONENT_PACKAGES = ["@playwright/test", "@playwright/experimental-ct-core", "@playwright/experimental-ct-react",
                      "@playwright/experimental-ct-vue", "@playwright/experimental-ct-svelte"]
SPAWN_SECONDS = 120


def find(environ=None):
    """Absolute path of the playwright executable from AIARCH_PLAYWRIGHT (absolute) or PATH; never a package runner."""
    environ = os.environ if environ is None else environ
    cfg = environ.get(ENV_VAR)
    if cfg:
        return cfg if os.path.isabs(cfg) and os.access(cfg, os.X_OK) and os.path.isfile(cfg) else None
    return shutil.which("playwright", path=environ.get("PATH", os.defpath))


def status():
    exe = find()
    return {"state": "present-unverified" if exe else "unavailable", "path": exe, "spawns": 0,
            "outcome": "NOT RUN", "cli_contract": CLI_CONTRACT, "trace_contract": list(TRACE_MEMBERS),
            "component_contract": {"status": "unsupported here", "packages": COMPONENT_PACKAGES},
            "reason": "executable found but never run" if exe else "no playwright executable on PATH or AIARCH_PLAYWRIGHT"}


def read_trace(path):
    """(ok, reason): the archive holds trace.trace and trace.network, each a file of JSON lines."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for member in TRACE_MEMBERS:
                if member not in names:
                    return False, f"{member} missing from the archive"
                for line in archive.read(member).decode("utf-8", "replace").splitlines():
                    if line.strip():
                        json.loads(line)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return False, "trace archive unreadable"
    return True, ""


def run(tree, out, allow=False):
    """{outcome, reason}. Spawns only with allow=True and an executable; never reads npm or the network."""
    if not allow:
        return {"outcome": "NOT RUN", "reason": "--allow-playwright not given; nothing was spawned", "spawned": False}
    exe = find()
    if not exe:
        return {"outcome": "NOT RUN", "reason": "no playwright executable on PATH or AIARCH_PLAYWRIGHT", "spawned": False}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pw-home-") as home:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": home, "CI": "1"}
        try:
            proc = subprocess.run([exe, "test", "--reporter=json", "--trace=on", "--output", str(out)], cwd=tree, env=env,
                                  capture_output=True, text=True, timeout=SPAWN_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"outcome": "NOT RUN", "reason": f"spawn failed: {error.__class__.__name__}", "spawned": True}
    try:
        report = json.loads(proc.stdout)
        stats = report["stats"]
        passing = proc.returncode == 0 and stats["unexpected"] == 0 and stats["expected"] >= 1
    except (ValueError, KeyError, TypeError):
        return {"outcome": "unverified", "reason": "no readable JSON report", "spawned": True}
    if not passing:
        return {"outcome": "unverified", "reason": "report is not a passing run", "spawned": True}
    zips = sorted(out.rglob("trace.zip"))
    if not zips:
        return {"outcome": "unverified", "reason": "no trace.zip was written", "spawned": True}
    ok, why = read_trace(zips[0])
    return {"outcome": "ran" if ok else "unverified", "reason": why, "spawned": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--tree", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--allow-playwright", action="store_true")
    p = sub.add_parser("trace")
    p.add_argument("zip")
    args = parser.parse_args(argv)
    if args.command == "status":
        report = status()
        print(json.dumps(report, sort_keys=True, indent=2) if args.json else f"playwright: {report['state']} (NOT RUN)")
        return 0
    if args.command == "trace":
        ok, why = read_trace(args.zip)
        print(json.dumps({"ok": ok, "reason": why}))
        return 0 if ok else 1
    if not args.allow_playwright:
        print(json.dumps({"status": "refused", "reason": "run needs --allow-playwright; nothing was spawned"}))
        return 2
    result = run(args.tree, args.out, allow=True)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
