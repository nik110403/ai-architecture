"""documents acceptance tests (A1-A6, A8 partly, A11, A13 plus tribune amendments F1-F13). Synthetic data only.

Every CLI call is a subprocess whose PYTHONPATH holds a sitecustomize that makes socket connect and
getaddrinfo raise and write a sentinel (F7). Every exit-code assertion also checks the parsed JSON
reason/cell/status and that stderr holds no traceback (F2). No binary fixture is committed (F5):
every docx/xlsx/pptx/pdf/zip is generated here from a JSON spec.
"""

import ast
import getpass
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "modules" / "documents"
SCRIPT = MODULE / "documents.py"
EXAMPLE = ROOT / "examples" / "documents"
BASE = "1a8fc8b2dee30b03df1dd0f3127dc88572719dc0"
sys.path.insert(0, str(ROOT / "modules" / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))

HOME_FRAG = "/" + "home/"
GUARD = (
    "import os, socket\n"
    "def _deny(*a, **k):\n"
    "    open(os.environ['GUARD_SENTINEL'], 'a').write('net\\n')\n"
    "    raise OSError('network disabled')\n"
    "socket.socket.connect = _deny\n"
    "socket.getaddrinfo = _deny\n"
)
ENV_ALLOWED = {"PATH", "LANG", "HOME"}
AST_ENV_ALLOWED = {"PATH", "LANG", "HOME", "XDG_STATE_HOME", "DOCUMENTS_TOOL_TIMEOUT", "DOCUMENTS_CONNECTOR_TIMEOUT"}

SPECS = {
    "docx": {"title": "Synthetic Report", "paragraphs": ["First paragraph of the synthetic report.",
                                                         "Second paragraph with numbers 42 and 7."]},
    "xlsx": {"sheets": [{"name": "Sheet1", "cells": {
        "A1": 3, "B1": 4, "C1": {"f": "A1+B1*2", "v": 11}, "D1": {"f": "(A1+B1)*2", "v": 14},
        "A2": "Total label", "B2": {"f": "SUM(A1:B1)", "v": 7}}}]},
    "pptx": {"slides": [{"title": "Quarterly outlook", "body": ["Revenue grows", "Costs stay flat"]},
                        {"title": "Next steps", "body": ["Review plan"]}]},
    "pdf": {"pages": [["Synthetic PDF page one", "Second line of page one"], ["Page two text"]]},
}
STRINGS = {
    "docx": SPECS["docx"]["paragraphs"],
    "xlsx": ["Total label"],
    "pptx": ["Quarterly outlook", "Revenue grows", "Costs stay flat", "Next steps", "Review plan"],
    "pdf": ["Synthetic PDF page one", "Second line of page one", "Page two text"],
}


def tree(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class R:
    def __init__(self, done):
        self.code, self.out, self.err = done.returncode, done.stdout, done.stderr
        try:
            self.j = json.loads(self.out)
        except ValueError:
            self.j = None


class Box:
    """A sandbox: HOME, cwd and XDG_STATE_HOME redirected into one watched tmp dir."""

    def __init__(self, test):
        self.test = test
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("home", "cwd", "xdg", "work", "bin", "guard"):
            (self.tmp / name).mkdir()
        (self.tmp / "guard" / "sitecustomize.py").write_text(GUARD)
        self.sentinel = self.tmp.parent / (self.tmp.name + ".net")
        test.addCleanup(self.no_net)
        self.work = self.tmp / "work"

    def no_net(self):
        present = self.sentinel.exists()
        self.sentinel.unlink(missing_ok=True)
        self.test.assertFalse(present, "a subprocess tried to use the network")

    def env(self, path=None, extra=None):
        env = {"PATH": path if path is not None else os.environ["PATH"], "HOME": str(self.tmp / "home"),
               "XDG_STATE_HOME": str(self.tmp / "xdg"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(self.tmp / "guard"), "GUARD_SENTINEL": str(self.sentinel)}
        env.update(extra or {})
        return env

    def run(self, script, *args, path=None, extra=None, timeout=120, fmt=True):
        argv = [sys.executable, "-B", str(script), *[str(a) for a in args]]
        if fmt:
            argv += ["--format", "json"]
        done = subprocess.run(argv, capture_output=True, text=True, cwd=self.tmp / "cwd", timeout=timeout,
                              env=self.env(path, extra))
        r = R(done)
        self.test.assertNotIn("Traceback", r.err, r.err)
        self.test.assertNotIn(HOME_FRAG, r.out + r.err)
        for word in {socket.gethostname().split(".")[0], getpass.getuser()}:
            if len(word) >= 4:
                self.test.assertIsNone(re.search(r"\b" + re.escape(word) + r"\b", r.out + r.err), word)
        return r

    def docs(self, *args, **kw):
        return self.run(SCRIPT, *args, **kw)

    def write(self, name, data):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            data = json.dumps(data)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def create(self, kind, spec=None, name=None):
        spec_path = self.write(f"{kind}-spec.json", SPECS[kind] if spec is None else spec)
        out = self.work / (name or f"sample.{kind}")
        r = self.docs("create", "--kind", kind, "--spec", spec_path, "--out", out)
        self.test.assertEqual((r.code, r.j.get("status")), (0, "ok"), r.out + r.err)
        return out


def expect(test, r, code, **fields):
    test.assertEqual(r.code, code, r.out + r.err)
    test.assertIsNotNone(r.j, r.out + r.err)
    for key, value in fields.items():
        test.assertEqual(r.j.get(key), value, r.out)


def load_module(name="documents_under_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_stub(box, name, body):
    path = box.tmp / "bin" / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def zip_bytes(entries, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in entries:
            z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, compress_type=method)
    return buf.getvalue()


CT_DOCX = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/'
           'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
           '</Types>')
DOC_XML = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>x'
           '</w:t></w:r></w:p></w:body></w:document>')
RELS_EXT = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" '
            'Type="http://example.invalid/t" Target="http://example.invalid/x" TargetMode="External"/></Relationships>')


def good_docx_entries():
    return [("[Content_Types].xml", CT_DOCX), ("word/document.xml", DOC_XML)]


class FormatsTests(unittest.TestCase):
    def test_a1_round_trip_all_kinds(self):
        box = Box(self)
        for kind in SPECS:
            first = box.create(kind)
            data = first.read_bytes()
            again = box.create(kind, name=f"again.{kind}")
            self.assertEqual(data, again.read_bytes(), f"{kind} not byte-identical")
            r = box.docs("inspect", first)
            expect(self, r, 0, status="ok", kind=kind)
            if kind == "docx":
                self.assertEqual(r.j["paragraphs"], SPECS["docx"]["paragraphs"])
                self.assertEqual(r.j["title"], "Synthetic Report")
            if kind == "xlsx":
                sheet = r.j["sheets"][0]
                self.assertEqual(sheet["name"], "Sheet1")
                self.assertEqual(sheet["cells"], {"A1": 3, "B1": 4, "C1": 11, "D1": 14, "A2": "Total label", "B2": 7})
                self.assertEqual(sheet["formulas"], {"C1": "A1+B1*2", "D1": "(A1+B1)*2", "B2": "SUM(A1:B1)"})
            if kind == "pptx":
                self.assertEqual(r.j["slide_count"], 2)
                self.assertEqual([s["text"] for s in r.j["slides"]],
                                 [["Quarterly outlook", "Revenue grows", "Costs stay flat"], ["Next steps", "Review plan"]])
            if kind == "pdf":
                self.assertEqual(r.j["page_count"], 2)
                self.assertEqual(r.j["pages"], SPECS["pdf"]["pages"])

    def test_a1_kind_comes_from_content_not_extension(self):
        box = Box(self)
        pdf = box.create("pdf")
        docx = box.create("docx")
        renamed = box.write("pdf-as.docx", pdf.read_bytes())
        expect(self, box.docs("inspect", renamed), 0, kind="pdf")
        renamed = box.write("docx-as.pdf", docx.read_bytes())
        expect(self, box.docs("inspect", renamed), 0, kind="docx")
        expect(self, box.docs("inspect", box.write("junk.docx", b"not a document at all")), 2, reason="unknown_format")

    def test_a1_core_props_are_generic(self):
        box = Box(self)
        for kind in ("docx", "xlsx", "pptx"):
            with zipfile.ZipFile(box.create(kind)) as z:
                core = z.read("docProps/core.xml").decode()
            self.assertNotIn("creator", core.lower())
            self.assertNotIn("lastModifiedBy", core)

    def test_bad_specs_refused(self):
        box = Box(self)
        for kind, spec in (("docx", {"paragraphs": []}), ("xlsx", {"sheets": [{"name": "S", "cells": {"a1": 1}}]}),
                           ("pdf", {"pages": [["x" * 200]]}), ("pptx", {"slides": [{"title": "t\x01"}]})):
            r = box.docs("create", "--kind", kind, "--spec", box.write("bad.json", spec), "--out", box.work / "o.bin")
            expect(self, r, 2, reason="bad_spec")
        dup = box.write("dup.json", '{"paragraphs": ["a"], "paragraphs": ["b"]}')
        expect(self, box.docs("create", "--kind", "docx", "--spec", dup, "--out", box.work / "o.bin"), 2,
               reason="duplicate_key")
        self.assertFalse((box.work / "o.bin").exists())

    def test_no_binary_fixture_committed(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        done = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "--",
                               "modules/documents", "modules/productivity", "examples/documents"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        files = [f for f in done.stdout.splitlines() if (ROOT / f).is_file()]
        self.assertTrue(files)
        for f in files:
            self.assertNotIn(b"\0", (ROOT / f).read_bytes(), f)


class RealToolTests(unittest.TestCase):
    """F1: real converters, only when observed available; otherwise a recorded skip."""

    def tools(self, box):
        r = box.docs("tools", "status")
        expect(self, r, 0, status="ok")
        return r.j["tools"]

    def test_a1b_real_tools_render_created_files(self):
        box = Box(self)
        tools = self.tools(box)
        if tools["pdftotext"]["status"] != "available":
            self.skipTest("pdftotext " + tools["pdftotext"]["reason"])
        pdf = box.create("pdf")
        text = subprocess.run(["pdftotext", "-q", str(pdf), "-"], capture_output=True, text=True).stdout
        for line in STRINGS["pdf"]:
            self.assertIn(line, text)
        r = box.docs("render", pdf, "--out", box.work / "r-pdf", "--allow-tool", "pdftotext")
        expect(self, r, 0, status="pass", missing=[])
        if tools["soffice"]["status"] != "available":
            self.skipTest("soffice " + tools["soffice"]["reason"])
        for kind in ("docx", "xlsx", "pptx"):
            created = box.create(kind)
            spec = box.write(f"{kind}-render-spec.json", SPECS[kind])
            r = box.docs("render", created, "--spec", spec, "--out", box.work / f"r-{kind}", "--allow-tool", "soffice",
                         "--allow-tool", "pdftotext", timeout=240)
            expect(self, r, 0, status="pass", missing=[])
            receipt = json.loads((box.work / f"r-{kind}" / "render-receipt.json").read_text())
            self.assertEqual(receipt["status"], "pass")

    def test_a1b_pandoc_reads_created_docx(self):
        box = Box(self)
        tools = self.tools(box)
        if tools["pandoc"]["status"] != "available":
            self.skipTest("pandoc " + tools["pandoc"]["reason"])
        docx = box.create("docx")
        text = subprocess.run(["pandoc", str(docx), "-t", "plain"], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "HOME": str(box.tmp / "home")}).stdout
        for line in STRINGS["docx"]:
            self.assertIn(line, " ".join(text.split()))


class HostileTests(unittest.TestCase):
    def cases(self):
        many = good_docx_entries() + [(f"extra/{i}.txt", "x") for i in range(1000)]
        bomb = good_docx_entries() + [("word/blob.bin", b"\0" * 3_000_000)]
        good = zip_bytes(good_docx_entries())
        return {
            "path_traversal": zip_bytes(good_docx_entries() + [("../evil.txt", "x")]),
            "path_traversal_abs": zip_bytes(good_docx_entries() + [("/abs/evil.txt", "x")]),
            "too_many_entries": zip_bytes(many),
            "expansion_ratio": zip_bytes(bomb),
            "macro_present": zip_bytes(good_docx_entries() + [("word/vbaProject.bin", b"stub")]),
            "external_relationship": zip_bytes(good_docx_entries() + [("word/_rels/document.xml.rels", RELS_EXT)]),
            "truncated": good[: len(good) // 2],
            "dtd_present": zip_bytes([("[Content_Types].xml", CT_DOCX), ("word/document.xml",
                                      '<!DOCTYPE d [<!ENTITY e "x">]>' + DOC_XML)]),
        }

    def test_a2_each_hostile_file_refused_with_its_reason(self):
        box = Box(self)
        for reason, data in self.cases().items():
            with self.subTest(reason=reason):
                path = box.write(f"hostile-{reason}.docx", data)
                before = tree(box.tmp)
                r = box.docs("inspect", path)
                expect(self, r, 2, status="refused", reason=reason.replace("_abs", ""))
                self.assertEqual(tree(box.tmp), before, "inspect wrote something")

    def test_r9_f1_encoding_tricks_cannot_hide_external_links_or_doctype(self):
        box = Box(self)
        rels = "word/_rels/document.xml.rels"
        cases = {
            "char_ref": (RELS_EXT.replace('"External"', '"Ext&#101;rnal"'), rels, "external_relationship"),
            "lowercase": (RELS_EXT.replace('"External"', '"external"'), rels, "external_relationship"),
            "utf16_rels": (RELS_EXT.encode("utf-16"), rels, None),
            "utf16_doctype": (('<!DOCTYPE d [<!ENTITY e "x">]>' + DOC_XML).encode("utf-16"), "word/document.xml", None),
            "declared_latin1": ('<?xml version="1.0" encoding="ISO-8859-1"?>' + DOC_XML, "word/document.xml", None),
        }
        for label, (data, part, reason) in cases.items():
            with self.subTest(case=label):
                entries = [e for e in good_docx_entries() if e[0] != part] + [(part, data)]
                r = box.docs("inspect", box.write(f"enc-{label}.docx", zip_bytes(entries)))
                expect(self, r, 2, status="refused")
                if reason:
                    self.assertEqual(r.j["reason"], reason)

    def test_a2_hostile_package_refused_too(self):
        box = Box(self)
        path = box.write("bad.zip", zip_bytes([("../evil.txt", "x")]))
        expect(self, box.docs("verify-package", path), 2, reason="path_traversal")

    def test_a2_truncated_pdf_refused(self):
        box = Box(self)
        data = box.create("pdf").read_bytes()
        expect(self, box.docs("inspect", box.write("cut.pdf", data[: len(data) - 40])), 2, reason="truncated")


class FormulaTests(unittest.TestCase):
    def sheet(self, box, cells, name="Sheet1"):
        return box.create("xlsx", {"sheets": [{"name": name, "cells": cells}]}, name=f"{len(cells)}-{abs(hash(str(cells)))}.xlsx")

    def test_a3_correct_sheet_passes_with_precedence_and_parens(self):
        box = Box(self)
        expect(self, box.docs("check-formulas", box.create("xlsx")), 0, status="pass")

    def test_a3_off_by_one_names_the_cell(self):
        box = Box(self)
        spec = json.loads(json.dumps(SPECS["xlsx"]))
        spec["sheets"][0]["cells"]["C1"]["v"] = 12
        r = box.docs("check-formulas", box.create("xlsx", spec, name="off.xlsx"))
        expect(self, r, 1, status="fail", reason="mismatch", cell="Sheet1!C1")

    def test_a3_token_boundaries_a1_vs_a10(self):
        box = Box(self)
        good = self.sheet(box, {"A1": 100, "A10": 5, "B1": {"f": "A10+1", "v": 6}, "B2": {"f": "A1+1", "v": 101}})
        expect(self, box.docs("check-formulas", good), 0, status="pass")
        bad = self.sheet(box, {"A1": 1, "A10": {"f": "A1+1", "v": 3}, "B1": {"f": "A1*2", "v": 2}})
        r = box.docs("check-formulas", bad)
        expect(self, r, 1, reason="mismatch", cell="Sheet1!A10")
        self.assertEqual([m["cell"] for m in r.j["mismatches"]], ["Sheet1!A10"])

    def test_a3_unsupported_never_passes(self):
        box = Box(self)
        for formula in ("INDIRECT(A1)", "Sheet2!A1+1", "SUM(A1:A2)+AVERAGE(A1:A2)", "A1^2", "foo"):
            with self.subTest(formula=formula):
                r = box.docs("check-formulas", self.sheet(box, {"A1": 1, "B1": {"f": formula, "v": 1}}))
                expect(self, r, 1, status="fail", reason="unsupported")
                self.assertEqual(r.j["cell"], "Sheet1!B1")

    def test_r9_f2_forward_reference_does_not_corrupt_the_outer_parse(self):
        box = Box(self)
        # A1 = A2 + 1 = 7, but the cached 6 is wrong; evaluating A2 mid-parse must not make A1 compute 6.
        r = box.docs("check-formulas", self.sheet(box, {"A1": {"f": "A2+1", "v": 6}, "A2": {"f": "3*2", "v": 6}}))
        expect(self, r, 1, status="fail", reason="mismatch", cell="Sheet1!A1")
        good = self.sheet(box, {"A1": {"f": "A2+1", "v": 7}, "A2": {"f": "3*2", "v": 6}, "A3": {"f": "A2*A1", "v": 42}})
        expect(self, box.docs("check-formulas", good), 0, status="pass")

    def test_a3_cycle_and_div_zero(self):
        box = Box(self)
        r = box.docs("check-formulas", self.sheet(box, {"A1": {"f": "B1", "v": 1}, "B1": {"f": "A1", "v": 1}}))
        expect(self, r, 1, status="fail", reason="cycle")
        r = box.docs("check-formulas", self.sheet(box, {"A1": 1, "B1": {"f": "A1/0", "v": 1}}))
        expect(self, r, 1, reason="div_by_zero", cell="Sheet1!B1")

    def test_a3_formula_without_cached_value_is_a_mismatch(self):
        box = Box(self)
        data = zip_bytes([("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                           '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                           'spreadsheetml.sheet.main+xml"/></Types>'),
                          ("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" '
                           'sheetId="1" r:id="rId1"/></sheets></workbook>'),
                          ("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                           'relationships"><Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>'),
                          ("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
                           'main"><sheetData><row r="1"><c r="A1"><v>1</v></c><c r="B1"><f>A1+1</f></c></row></sheetData>'
                           '</worksheet>')])
        expect(self, box.docs("check-formulas", box.write("nocache.xlsx", data)), 1, reason="mismatch", cell="S!B1")


class ToolTests(unittest.TestCase):
    def test_a4_missing_tools_unavailable_with_reason(self):
        box = Box(self)
        r = box.docs("tools", "status", path=str(box.tmp / "bin"))
        expect(self, r, 0, status="ok")
        for name, info in r.j["tools"].items():
            self.assertEqual(info["status"], "unavailable", name)
            self.assertTrue(info["reason"])

    def test_a4_hang_and_garbage_version(self):
        box = Box(self)
        make_stub(box, "soffice", "sleep 30 &\nsleep 30\n")
        make_stub(box, "pdftotext", "echo hello garbage\n")
        make_stub(box, "pandoc", "echo 'pandoc 3.1.3'\n")
        started = time.monotonic()
        r = box.docs("tools", "status", path=f"{box.tmp / 'bin'}:/usr/bin:/bin", extra={"DOCUMENTS_TOOL_TIMEOUT": "1"})
        self.assertLess(time.monotonic() - started, 5, "hang stub was not killed by group")
        expect(self, r, 0)
        tools = r.j["tools"]
        self.assertEqual(tools["soffice"]["status"], "unavailable")
        self.assertIn("timeout", tools["soffice"]["reason"])
        self.assertEqual(tools["pdftotext"]["status"], "unavailable")
        self.assertIn("version", tools["pdftotext"]["reason"])
        self.assertEqual(tools["pandoc"], {"status": "available", "version": "3.1.3"})

    def test_a4_render_without_tool_is_exit_5_and_unavailable_receipt(self):
        box = Box(self)
        docx = box.create("docx")
        r = box.docs("render", docx, "--out", box.work / "rr", "--allow-tool", "soffice", "--allow-tool", "pdftotext",
                     path=str(box.tmp / "bin"))
        expect(self, r, 5, status="unavailable")
        receipt = json.loads((box.work / "rr" / "render-receipt.json").read_text())
        self.assertEqual(receipt["status"], "unavailable")
        self.assertTrue(receipt["reason"])

    def test_a4_render_needs_allow_tool(self):
        box = Box(self)
        expect(self, box.docs("render", box.create("docx"), "--out", box.work / "rr"), 2, reason="tool_not_allowed")
        self.assertFalse((box.work / "rr").exists())

    def stub_tools(self, box, soffice_body, text):
        make_stub(box, "soffice", 'case "$1" in --version) echo "LibreOffice 7.6.2"; exit 0;; esac\n' + soffice_body)
        make_stub(box, "pdftotext", 'case "$1" in --version|-v) echo "pdftotext version 24.02.0" >&2; exit 0;; esac\n'
                  f'echo "{text}"\n')

    def render_stub(self, box):
        docx = box.create("docx")
        spec = box.write("spec.json", SPECS["docx"])
        return box.docs("render", docx, "--spec", spec, "--out", box.work / "rr", "--allow-tool", "soffice",
                        "--allow-tool", "pdftotext", path=f"{box.tmp / 'bin'}:/usr/bin:/bin")

    def test_f10_tool_exit_zero_without_output_is_failure(self):
        box = Box(self)
        self.stub_tools(box, "exit 0\n", "irrelevant")
        r = self.render_stub(box)
        expect(self, r, 1, status="fail", reason="no_pdf")
        self.assertNotEqual(json.loads((box.work / "rr" / "render-receipt.json").read_text())["status"], "pass")

    def test_f10_wrong_text_is_failure(self):
        box = Box(self)
        self.stub_tools(box, 'for a; do last="$a"; done\nwhile [ $# -gt 0 ]; do [ "$1" = --outdir ] && out="$2"; shift; done\n'
                        'n="$(basename "$last")"; echo pdf > "$out/${n%.*}.pdf"\n', "unrelated words only")
        r = self.render_stub(box)
        expect(self, r, 1, status="fail", reason="text_missing")
        self.assertEqual(len(r.j["missing"]), 2)

    def test_a4_stub_pipeline_passes_when_text_present(self):
        box = Box(self)
        self.stub_tools(box, 'for a; do last="$a"; done\nwhile [ $# -gt 0 ]; do [ "$1" = --outdir ] && out="$2"; shift; done\n'
                        'n="$(basename "$last")"; echo pdf > "$out/${n%.*}.pdf"\n',
                        "First paragraph of the synthetic report. Second paragraph with numbers 42 and 7.")
        expect(self, self.render_stub(box), 0, status="pass", missing=[])


class PackageTests(unittest.TestCase):
    def test_package_round_trip_and_one_byte_change(self):
        box = Box(self)
        src = box.work / "src"
        box.write("src/a.txt", "alpha content here")
        box.write("src/sub/b.txt", "beta content here")
        out = box.work / "p.zip"
        expect(self, box.docs("package", "--in", src, "--out", out), 0, status="ok")
        first = out.read_bytes()
        box.docs("package", "--in", src, "--out", out)
        self.assertEqual(first, out.read_bytes(), "package not deterministic")
        expect(self, box.docs("verify-package", out), 0, status="pass")
        entries = []
        with zipfile.ZipFile(out) as z:
            for name in z.namelist():
                data = z.read(name)
                if name == "a.txt":
                    data = b"alpha content herf"
                entries.append((name, data))
        tampered = box.write("t.zip", zip_bytes(entries))
        expect(self, box.docs("verify-package", tampered), 1, status="fail", reason="hash_mismatch", path="a.txt")

    def test_package_extra_and_missing_files(self):
        box = Box(self)
        box.write("src/a.txt", "alpha")
        out = box.work / "p.zip"
        box.docs("package", "--in", box.work / "src", "--out", out)
        with zipfile.ZipFile(out) as z:
            entries = [(n, z.read(n)) for n in z.namelist()]
        expect(self, box.docs("verify-package", box.write("x.zip", zip_bytes(entries + [("extra.txt", "x")]))), 1,
               reason="unlisted_file")
        expect(self, box.docs("verify-package", box.write("m.zip", zip_bytes([e for e in entries if e[0] != "a.txt"]))),
               1, reason="missing_file")
        expect(self, box.docs("verify-package", box.write("n.zip", zip_bytes([("a.txt", "alpha")]))), 1,
               reason="manifest_missing")


STORE = {"version": 1, "docs": {"doc-1": {"title": "Alpha", "content": "first text", "revision": "r1"}}}


def stub_connectors(box, operations="read,write,create", claim=("read", "write", "create"), extra_argv=()):
    stub = EXAMPLE / "stub_connector.py"
    argv = [sys.executable, "-B", str(stub), "--sentinel", str(box.tmp / "spawned"), "--env-dump",
            str(box.tmp / "env-keys"), "--operations", operations, *extra_argv]
    return box.write("connectors.json", {"connectors": {"stub": {"argv": argv, "operations": list(claim)},
                                                         "ghost": {"argv": [str(box.tmp / "nope")], "operations": ["read"]}}})


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)
        self.store = self.box.write("store.json", STORE)
        self.content = self.box.write("c.txt", "second text")

    def cloud(self, op, *args, **kw):
        return self.box.docs("cloud", op, "--connector", "sim", "--store", self.store, *args, **kw)

    def test_a5_revisions(self):
        expect(self, self.cloud("read", "--doc", "doc-1"), 0, revision="r1", content="first text")
        r = self.cloud("write", "--doc", "doc-1", "--base-revision", "r1", "--content", self.content)
        expect(self, r, 0, status="ok", revision_before="r1", revision_after="r2", native="unverified")
        expect(self, self.cloud("read", "--doc", "doc-1"), 0, revision="r2", content="second text")
        before = self.store.read_bytes()
        expect(self, self.cloud("write", "--doc", "doc-1", "--base-revision", "r1", "--content", self.content), 3,
               status="conflict", reason="stale_revision")
        self.assertEqual(before, self.store.read_bytes())
        expect(self, self.cloud("write", "--doc", "doc-1", "--content", self.content), 2, reason="base_revision_required")

    def test_a5_create_never_recreates_and_write_never_creates(self):
        before = self.store.read_bytes()
        expect(self, self.cloud("create", "--doc", "doc-2", "--title", "Alpha", "--content", self.content), 3,
               reason="exists")
        expect(self, self.cloud("create", "--doc", "doc-1", "--title", "Other", "--content", self.content), 3,
               reason="exists")
        expect(self, self.cloud("write", "--doc", "doc-9", "--base-revision", "r1", "--content", self.content), 3,
               reason="unknown_document")
        self.assertEqual(before, self.store.read_bytes())
        expect(self, self.cloud("create", "--doc", "doc-2", "--title", "Beta", "--content", self.content), 0,
               revision_after="r1")
        self.assertIn("doc-2", json.loads(self.store.read_text())["docs"])

    def test_a5_unknown_or_unavailable_connector_is_exit_4_and_changes_nothing(self):
        conns = stub_connectors(self.box)
        before, files = self.store.read_bytes(), tree(self.box.tmp)
        for name in ("nope", "ghost"):
            for op, extra in (("read", ()), ("write", ("--base-revision", "r1", "--content", self.content)),
                              ("create", ("--title", "Zed", "--content", self.content))):
                with self.subTest(name=name, op=op):
                    r = self.box.docs("cloud", op, "--connector", name, "--connectors", conns, "--store", self.store,
                                      "--doc", "doc-1", "--allow-connector", *extra)
                    expect(self, r, 4, status="refused")
                    self.assertEqual(self.store.read_bytes(), before)
                    self.assertEqual(tree(self.box.tmp), files)
        r = self.box.docs("cloud", "read", "--connector", "nope", "--store", self.store, "--doc", "doc-1")
        expect(self, r, 4, reason="unknown_connector")

    def test_a6_claims_are_ignored_describe_decides(self):
        conns = stub_connectors(self.box, operations="read")
        r = self.box.docs("cloud", "write", "--connector", "stub", "--connectors", conns, "--store", self.store,
                          "--doc", "doc-1", "--base-revision", "r1", "--content", self.content, "--allow-connector")
        expect(self, r, 4, status="refused", reason="operation_not_offered")
        self.assertFalse((self.box.tmp / "spawned").exists())

    def test_a6_native_op_needs_allow_flag_before_any_spawn(self):
        conns = stub_connectors(self.box)
        r = self.box.docs("cloud", "read", "--connector", "stub", "--connectors", conns, "--store", self.store,
                          "--doc", "doc-1")
        expect(self, r, 2, reason="connector_not_allowed")
        self.assertFalse((self.box.tmp / "spawned").exists())

    def stub_write(self, content_text, evidence=None, base="r1"):
        conns = stub_connectors(self.box)
        content = self.box.write("sc.txt", content_text)
        args = ["--evidence", evidence] if evidence else []
        return self.box.docs("cloud", "write", "--connector", "stub", "--connectors", conns, "--store", self.store,
                             "--doc", "doc-1", "--base-revision", base, "--content", content, "--allow-connector", *args)

    def test_a6_stub_output_is_derived_not_echoed(self):
        one, two = self.stub_write("alpha text"), self.stub_write("beta text")
        expect(self, one, 0, status="ok", native="unverified")
        self.assertTrue((self.box.tmp / "spawned").exists())
        self.assertNotEqual(one.j["revision_after"], "r1")
        self.assertNotEqual(one.j["revision_after"], two.j["revision_after"])
        self.assertEqual(one.j["revision_after"], self.stub_write("alpha text").j["revision_after"])

    def test_a6_native_verified_only_with_matching_disposable_evidence(self):
        after = self.stub_write("alpha text").j["revision_after"]
        good = {"disposable": True, "doc": "doc-1", "revision_before": "r1", "revision_after": after}
        r = self.stub_write("alpha text", self.box.write("ev.json", good))
        expect(self, r, 0, native="verified")
        bad = {"non-disposable": {**good, "disposable": False}, "wrong doc": {**good, "doc": "doc-2"},
               "wrong revision": {**good, "revision_after": "x-0000"}, "no change": {**good, "revision_before": after},
               "wrong before": {**good, "revision_before": "r7"}, "extra key": {**good, "note": "x"},
               "not boolean": {**good, "disposable": "true"}}
        for label, ev in bad.items():
            with self.subTest(label):
                expect(self, self.stub_write("alpha text", self.box.write("ev.json", ev)), 0, native="unverified")
        expect(self, self.stub_write("alpha text", self.box.tmp / "missing.json"), 0, native="unverified")
        expect(self, self.stub_write("alpha text", self.box.write("ev.json", "not json")), 0, native="unverified")

    def test_a8_environment_is_the_allow_list_only(self):
        canary = "ZZ_CANARY_SECRET"
        value = "canary-" + "value-" + "12345"
        conns = stub_connectors(self.box)
        content = self.box.write("sc.txt", "alpha text")
        r = self.box.docs("cloud", "write", "--connector", "stub", "--connectors", conns, "--store", self.store,
                          "--doc", "doc-1", "--base-revision", "r1", "--content", content, "--allow-connector",
                          extra={canary: value})
        expect(self, r, 0)
        dump = (self.box.tmp / "env-keys").read_text()
        keys = set(dump.split())
        self.assertTrue(keys and keys <= ENV_ALLOWED, keys)
        self.assertNotIn(canary, dump)
        self.assertNotIn(value, dump)
        self.box.docs("tools", "status", extra={canary: value})
        for blob in (r.out, self.store.read_text()):
            self.assertNotIn(canary, blob)
            self.assertNotIn(value, blob)

    def test_a8_connectors_file_refused_for_secrets(self):
        token = "sk" + "-" + "ab12" * 6
        address = "someone" + "@" + "example.invalid"
        cases = {"credential_key": [{"connectors": {"x": {"argv": ["a"], "api_key": "x"}}},
                                    {"connectors": {"x": {"argv": ["a"], "refresh_token": "x"}}},
                                    {"connectors": {"x": {"argv": ["a"], "cookie": "x"}}},
                                    {"connectors": {"x": {"argv": ["a"], "password": "x"}}},
                                    {"connectors": {"x": {"argv": ["a"], "secret": "x"}}},
                                    {"connectors": {"x": {"argv": ["a"], "token": "x"}}}],
                 "token_value": [{"connectors": {"x": {"argv": ["a", token]}}},
                                 {"connectors": {"x": {"argv": ["a", "Bearer " + "abcdef0123456789" * 2]}}}],
                 "email_address": [{"connectors": {"x": {"argv": ["a"], "kind": address}}}]}
        for reason, docs in cases.items():
            for doc in docs:
                with self.subTest(reason=reason, doc=json.dumps(doc)[:60]):
                    conns = self.box.write("bad-conns.json", doc)
                    r = self.box.docs("cloud", "read", "--connector", "x", "--connectors", conns, "--store", self.store,
                                      "--doc", "doc-1", "--allow-connector")
                    expect(self, r, 2, reason=reason)
                    self.assertNotIn(token, r.out)
                    self.assertFalse((self.box.tmp / "spawned").exists())
        dup = self.box.write("dup-conns.json", '{"connectors": {}, "connectors": {}}')
        expect(self, self.box.docs("cloud", "read", "--connector", "x", "--connectors", dup, "--store", self.store,
                                   "--doc", "doc-1"), 2, reason="duplicate_key")


class ProcessTests(unittest.TestCase):
    def test_f9_connector_hang_with_grandchild_is_killed_by_group(self):
        box = Box(self)
        script = box.write("hang.sh", "sleep 30 &\nsleep 30\n")
        conns = box.write("c.json", {"connectors": {"hang": {"argv": ["/bin/sh", str(script)], "operations": ["read"]}}})
        store = box.write("store.json", STORE)
        started = time.monotonic()
        r = box.docs("cloud", "read", "--connector", "hang", "--connectors", conns, "--store", store, "--doc", "doc-1",
                     "--allow-connector", extra={"DOCUMENTS_CONNECTOR_TIMEOUT": "1"})
        self.assertLess(time.monotonic() - started, 5)
        expect(self, r, 4, reason="connector_unavailable")


class SurfaceTests(unittest.TestCase):
    def walk(self, parser, path=""):
        import argparse
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    yield from self.walk(sub, f"{path} {name}".strip())
                    yield (f"{path} {name}".strip(), [name], sub)
        yield (path, [], parser)

    def options(self, build):
        seen = []
        for path, names, parser in self.walk(build()):
            seen += [(path, s) for a in parser._actions for s in a.option_strings] + [(path, n) for n in names]
        return seen

    def test_a11_no_bypass_options_documents(self):
        seen = self.options(load_module().build)
        self.assertTrue(seen)
        for path, text in seen:
            self.assertNotIn(text, {"--force", "--yes", "--no-confirm"}, path)
            for word in ("skip", "real", "force", "yes", "confirm"):
                self.assertNotIn(word, text.lower(), f"{path}: {text}")

    def test_f13_environment_reads_are_allow_listed(self):
        for script in (SCRIPT, ROOT / "modules" / "productivity" / "productivity.py"):
            tree_ = ast.parse(script.read_text())
            parents = {child: node for node in ast.walk(tree_) for child in ast.iter_child_nodes(node)}
            keys = set()
            for node in ast.walk(tree_):
                if not (isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name)
                        and node.value.id == "os"):
                    continue
                parent = parents[node]
                key = None
                if isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Constant):
                    key = parent.slice.value
                elif isinstance(parent, ast.Attribute) and parent.attr == "get":
                    call = parents[parent]
                    if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant):
                        key = call.args[0].value
                elif isinstance(parent, ast.Compare) and isinstance(parent.left, ast.Constant):
                    key = parent.left.value
                self.assertIsInstance(key, str, f"{script.name}:{node.lineno}: os.environ must be read by constant key")
                keys.add(key)
            self.assertTrue(keys <= AST_ENV_ALLOWED, (script.name, keys))
        for script in (SCRIPT, ROOT / "modules" / "productivity" / "productivity.py"):
            self.assertNotIn("os.getenv", script.read_text())
            self.assertNotIn("environ.copy", script.read_text())


class RepoTests(unittest.TestCase):
    def test_a13_descriptors(self):
        import contracts
        for name in ("documents", "productivity"):
            contracts.check_module_descriptor(ROOT / "modules" / name, module=name)
            data = json.loads((ROOT / "modules" / name / "descriptor.json").read_text())
            self.assertEqual((data["claimed_support"], data["evidence"], data["schemas"], data["dependencies"]),
                             ("planned", [], [], []))

    def test_a13_fenced_paths_untouched(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        branch = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
        if branch != "r9-u23":
            self.skipTest(f"fence diff is only meaningful on the build branch, on {branch}")
        done = subprocess.run(["git", "-C", str(ROOT), "diff", BASE, "--", "capabilities", "modules/contracts",
                               "modules/writing", "modules/browser", "scripts", "tools", "tests"],
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a13_privacy_scan_clean(self):
        import privacy_scan
        for rel in ("modules/documents", "modules/productivity", "examples/documents"):
            self.assertEqual(privacy_scan.scan(ROOT / rel), [], rel)


class ExampleTests(unittest.TestCase):
    def run_example(self, out):
        return subprocess.run([sys.executable, "-B", str(EXAMPLE / "run.py"), "--offline", "--out", str(out)],
                              capture_output=True, text=True, timeout=300,
                              env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(out.parent)})

    def verify(self, *args):
        return subprocess.run([sys.executable, "-B", str(EXAMPLE / "verify_receipts.py"), *args], capture_output=True,
                              text=True, timeout=300)

    def test_a12_example_and_receipt_verifier(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        out = tmp / "out"
        done = self.run_example(out)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.strip().splitlines()
        self.assertEqual(lines[-1], "documents: OK")
        receipt = json.loads((out / "receipt.json").read_text())
        steps = [s["step"] for s in receipt["steps"]]
        for needle in ("create docx", "create xlsx", "create pptx", "create pdf", "check-formulas", "package",
                       "cloud read", "cloud write", "cloud conflict", "cloud missing connector", "calendar add authorized",
                       "calendar add refused", "morning brief", "import preview", "import apply", "import rollback"):
            self.assertTrue(any(s.startswith(needle) for s in steps), needle)
        self.assertEqual(len(lines), len(steps) + 1, "one line per step plus the verdict")
        for step in receipt["steps"]:
            self.assertIn(step["status"], ("pass", "unavailable"))
            if step["status"] == "unavailable":
                self.assertTrue(step.get("reason"))
        self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 0)
        # tampered receipts
        for label, mutate in (("status", lambda r: r["steps"][0].update(status="fail")),
                              ("dropped step", lambda r: r["steps"].pop()),
                              ("unavailable without reason", lambda r: r["steps"][0].update(status="unavailable")),
                              ("bad artifact hash", lambda r: r.update(artifacts={k: "0" * 64 for k in r["artifacts"]}))):
            bad = json.loads(json.dumps(receipt))
            mutate(bad)
            path = out / "tampered.json"
            path.write_text(json.dumps(bad))
            self.assertEqual(self.verify("--receipt", path).returncode, 1, label)
        artifact = out / sorted(receipt["artifacts"])[0]
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"x")
        self.assertEqual(self.verify("--receipt", out / "receipt.json").returncode, 1, "tampered artifact")
        artifact.write_bytes(original)

    def test_a12_verify_receipts_reruns_in_temp_dir(self):
        done = self.verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("verify_receipts: OK", done.stdout)


if __name__ == "__main__":
    unittest.main()
