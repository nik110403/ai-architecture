#!/usr/bin/env python3
"""Offline walkthrough of the documents and productivity modules. Synthetic data, no network.

Creates and inspects four file kinds, formula-checks, packages, renders a pdf if pdftotext is observed,
runs sim cloud read/write/conflict and a missing-connector refusal, one authorized and one refused
calendar add, a morning brief and a memory import preview/apply/rollback. Writes OUT/receipt.json and
prints one line per step.
"""

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = HERE.parents[1] / "modules"
sys.path.insert(0, str(MODULES / "documents"))


def load(name):
    spec = spec_from_file_location(name, MODULES / name / f"{name}.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


documents, productivity = load("documents"), load("productivity")


def call(module, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = module.main([str(a) for a in argv] + ["--format", "json"])
        except SystemExit as exit_:
            code = exit_.code
    try:
        return code, json.loads(out.getvalue())
    except ValueError:
        return code, {}


def tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--offline", action="store_true", required=True, help="required; this example never uses the network")
    p.add_argument("--out", required=True)
    out = Path(p.parse_args().out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_STATE_HOME"] = str(out / "state")  # single-use authority claims stay inside OUT
    files, store = out / "files", out / "store.json"
    files.mkdir(exist_ok=True)
    shutil.copyfile(HERE / "sim-store" / "store.json", store)
    steps, artifacts = [], {}

    def step(name, ok, reason=None, **detail):
        status = "pass" if ok else "fail"
        if ok == "unavailable":
            status = "unavailable"
        steps.append({"step": name, "status": status, **({"reason": reason} if reason else {}), **detail})
        print(f"{name}: {status}" + (f" ({reason})" if status == "unavailable" else ""))

    code, info = call(documents, "tools", "status")
    step("tools status", code == 0, tools={k: v["status"] for k, v in info.get("tools", {}).items()})
    tool_info = info.get("tools", {})

    for kind in ("docx", "xlsx", "pptx", "pdf"):
        target = files / f"sample.{kind}"
        code, made = call(documents, "create", "--kind", kind, "--spec", HERE / "specs" / f"{kind}.json", "--out", target)
        code2, seen = call(documents, "inspect", target)
        spec = json.loads((HERE / "specs" / f"{kind}.json").read_text())
        same = {"docx": lambda: seen.get("paragraphs") == spec["paragraphs"],
                "xlsx": lambda: seen.get("sheets", [{}])[0].get("cells", {}).get("A2") == "Total label",
                "pptx": lambda: seen.get("slide_count") == len(spec["slides"]),
                "pdf": lambda: seen.get("pages") == spec["pages"]}[kind]()
        step(f"create {kind}", code == 0 and code2 == 0 and seen.get("kind") == kind and same, sha256=made.get("sha256"))
        artifacts[f"files/sample.{kind}"] = made.get("sha256", "")

    code, res = call(documents, "check-formulas", files / "sample.xlsx")
    step("check-formulas", code == 0 and res.get("status") == "pass", formulas=res.get("formulas"))
    bad = json.loads((HERE / "specs" / "xlsx.json").read_text())
    bad["sheets"][0]["cells"]["C1"]["v"] = 12
    (out / "bad-xlsx.json").write_text(json.dumps(bad))
    call(documents, "create", "--kind", "xlsx", "--spec", out / "bad-xlsx.json", "--out", files / "wrong.xlsx")
    code, res = call(documents, "check-formulas", files / "wrong.xlsx")
    step("check-formulas names wrong cell", code == 1 and res.get("cell") == "Sheet1!C1")

    code, made = call(documents, "package", "--in", files, "--out", out / "package.zip")
    code2, ver = call(documents, "verify-package", out / "package.zip")
    step("package", code == 0 and code2 == 0 and ver.get("status") == "pass", files=made.get("files"))
    artifacts["package.zip"] = hashlib.sha256((out / "package.zip").read_bytes()).hexdigest()

    if tool_info.get("pdftotext", {}).get("status") == "available":
        code, res = call(documents, "render", files / "sample.pdf", "--spec", HERE / "specs" / "pdf.json", "--out",
                         out / "render", "--allow-tool", "pdftotext")
        step("render pdf", code == 0 and res.get("status") == "pass")
    else:
        step("render pdf", "unavailable", reason="pdftotext: " + tool_info.get("pdftotext", {}).get("reason", "not observed"))

    cloud = ("cloud",)
    code, res = call(documents, *cloud, "read", "--connector", "sim", "--store", store, "--doc", "doc-1")
    step("cloud read", code == 0 and res.get("revision") == "r1")
    (out / "new.txt").write_text("second text")
    code, res = call(documents, *cloud, "write", "--connector", "sim", "--store", store, "--doc", "doc-1",
                     "--base-revision", "r1", "--content", out / "new.txt")
    step("cloud write", code == 0 and res.get("revision_after") == "r2")
    before = store.read_bytes()
    code, res = call(documents, *cloud, "write", "--connector", "sim", "--store", store, "--doc", "doc-1",
                     "--base-revision", "r1", "--content", out / "new.txt")
    step("cloud conflict", code == 3 and res.get("reason") == "stale_revision" and store.read_bytes() == before)
    code, res = call(documents, *cloud, "read", "--connector", "missing", "--connectors", HERE / "connectors.json",
                     "--store", store, "--doc", "doc-1", "--allow-connector")
    step("cloud missing connector", code == 4 and res.get("reason") == "unknown_connector" and store.read_bytes() == before)
    code, res = call(documents, *cloud, "read", "--connector", "stub", "--connectors", HERE / "connectors.json",
                     "--store", store, "--doc", "doc-1", "--allow-connector")
    step("cloud stub read unverified", code == 0 and res.get("native") == "unverified" and res.get("revision", "").startswith("x-"))

    payload = out / "event.json"
    payload.write_text(json.dumps({"title": "Planning", "start": "2030-05-03T10:00", "end": "2030-05-03T11:00"}))
    expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    authority = out / "authority.json"
    authority.write_text(json.dumps(productivity.seal({
        "action": "calendar.add", "target": "primary", "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "expires": expires})))
    args = ("calendar", "add", "--target", "primary", "--payload", payload, "--store", store, "--connector", "sim",
            "--allow-side-effects")
    code, res = call(productivity, *args, "--authority", authority)
    step("calendar add authorized", code == 0 and res.get("authority_origin") == "unverified", payload_sha256=res.get("payload_sha256"))
    before = store.read_bytes()
    code, res = call(productivity, *args)
    step("calendar add refused", code == 2 and res.get("reason") == "authority_required" and store.read_bytes() == before)

    before = store.read_bytes()
    code, res = call(productivity, "morning", "--store", store, "--date", "2030-05-01", "--out", out / "brief.md")
    step("morning brief", code == 0 and res.get("unread") == 2 and [e["id"] for e in res["events"]] == ["ev-2", "ev-1"]
         and store.read_bytes() == before)
    artifacts["brief.md"] = hashlib.sha256((out / "brief.md").read_bytes()).hexdigest()

    memory = out / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "a.md").write_text("alpha local\n")
    snapshot = tree(memory)
    imp = ("--from", HERE / "specs" / "memory-export.json", "--memory-dir", memory)
    code, res = call(productivity, "import", "preview", *imp)
    step("import preview", code == 0 and res.get("add") == ["b.md", "notes/c.md"] and tree(memory) == snapshot)
    code, res = call(productivity, "import", "apply", *imp, "--allow-write")
    step("import apply", code == 0 and res.get("conflicts") == ["a.md"] and (memory / "b.md").exists())
    code, res = call(productivity, "import", "rollback", "--memory-dir", memory)
    step("import rollback", code == 0 and tree(memory) == snapshot)

    ok = all(s["status"] != "fail" for s in steps)
    (out / "receipt.json").write_text(json.dumps({"ok": ok, "steps": steps, "artifacts": artifacts}, indent=2) + "\n",
                                      encoding="utf-8")
    print("documents: OK" if ok else "documents: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
