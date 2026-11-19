#!/usr/bin/env python3
"""Offline walkthrough of the writing module with a deterministic template writer.

Renders one synthetic source into three registers via the stub writer, checks each render,
routes one pair no writer supports, and round-trips a style profile. Writes OUT/receipt.json.
"""

import argparse
import contextlib
import hashlib
import io
import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = spec_from_file_location("writing", HERE.parents[1] / "modules" / "writing" / "writing.py")
writing = module_from_spec(spec)
spec.loader.exec_module(writing)

REGISTERS = ("correspondence", "essay", "personal")
PROFILE = {"version": 1, "registers": {"essay": {"prefer": ["plain verbs"], "avoid": ["filler"]}},
           "samples": [{"register": "essay", "sha256": "0" * 64}]}


def call(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = writing.main([str(a) for a in argv])
    return code, out.getvalue()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--offline", action="store_true", required=True, help="required; this example never uses the network")
    p.add_argument("--out", required=True)
    out = Path(p.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    steps = []

    def step(name, ok, **extra):
        steps.append({"step": name, "ok": bool(ok), **extra})
        print(f"{name}: {'ok' if ok else 'FAILED'}")

    for register in REGISTERS:
        target = out / register
        code, _ = call("writer", "run", "--writers", HERE / "writers.json", "--name", "stub", "--register", register,
                       "--destination", "file", "--source", HERE / "source.md", "--out", target, "--allow-writer")
        receipt = json.loads((target / "fidelity.json").read_text()) if (target / "fidelity.json").exists() else {}
        digest = hashlib.sha256((target / "draft.md").read_bytes()).hexdigest() if (target / "draft.md").exists() else ""
        step(f"render {register}", code == 0 and receipt.get("verdict") == "pass", register=register,
             draft_sha256=digest)

    code, text = call("route", "--register", "fiction", "--destination", "newspaper", "--writers",
                      HERE / "writers.json", "--format", "json")
    info = json.loads(text) if text.strip() else {}
    step("route fiction newspaper", code == 0 and info.get("writer") == "assistant-draft"
         and info.get("destination_updated") is False)

    work = out / "profile-work"
    work.mkdir(exist_ok=True)
    source = work / "profile-in.json"
    source.write_text(json.dumps(PROFILE), encoding="utf-8")
    codes = [call("profile", "import", source, "--profile-dir", work / "a")[0],
             call("profile", "export", "--profile-dir", work / "a", "--out", work / "export.json")[0],
             call("profile", "import", work / "export.json", "--profile-dir", work / "b")[0],
             call("profile", "export", "--profile-dir", work / "b", "--out", work / "export2.json")[0]]
    same = (work / "export.json").read_bytes() == (work / "export2.json").read_bytes()
    step("profile round trip", not any(codes) and same)

    ok = all(s["ok"] for s in steps)
    (out / "receipt.json").write_text(json.dumps({"ok": ok, "steps": steps}, indent=2) + "\n", encoding="utf-8")
    print("writing: OK" if ok else "writing: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
