#!/usr/bin/env python3
"""Print the companion receipt table (rows 21, 22, 64 and 70), each EXERCISED or MISSING with an owner and a reason.

    python3 verify_receipts.py

Always runs the offline fixture roundtrip first (so the adapter itself is exercised). When AIARCH_DISCOVERY_MANIFEST names
a manifest for a companion you installed, it also runs a real roundtrip in pack and inbox mode, and 70-discovery can then
be EXERCISED. AIARCH_REQUIRE_COMPANION=1 turns a MISSING 70-discovery into exit 3. Exit 0 when the table is complete.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISCOVERY = HERE.parents[1] / "modules" / "discovery-inbox" / "discovery.py"


def run(argv, tmp):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "AIARCH_REQUIRE_COMPANION": os.environ.get("AIARCH_REQUIRE_COMPANION", "")}
    return subprocess.run([sys.executable, "-B", *map(str, argv)], capture_output=True, text=True, timeout=600, env=env)


def main():
    manifest = os.environ.get("AIARCH_DISCOVERY_MANIFEST")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        done = run([HERE / "run.py", "--offline", "--out", out], tmp)
        if done.returncode != 0:
            print(done.stderr.strip(), file=sys.stderr)
            return 1
        if manifest:
            project = Path(tmp) / "targets" / "demo-project"
            project.mkdir(parents=True)
            (project / "AGENTS.md").write_text("# Demo project\n")
            for mode in ("pack", "inbox"):
                done = run([DISCOVERY, "roundtrip", "--companion", manifest, "--registry", Path(tmp) / "registry.json", "--project", project,
                            "--topic", "main", "--mode", mode, "--id", f"example/{mode}-widget", "--disposition", "deferred",
                            "--backend", "companion", "--out", out], tmp)
                if done.returncode != 0:
                    print(f"real-companion roundtrip ({mode}) failed: {done.stderr.strip()}", file=sys.stderr)
                    return done.returncode
        argv = [DISCOVERY, "companions", "--out", out]
        if manifest:
            argv += ["--companion", manifest]
        if shutil.which("graphify"):
            argv += ["--graphify", shutil.which("graphify")]
        done = run(argv, tmp)
        print(done.stdout, end="")
        if done.stderr.strip():
            print(done.stderr.strip(), file=sys.stderr)
        return done.returncode


if __name__ == "__main__":
    sys.exit(main())
