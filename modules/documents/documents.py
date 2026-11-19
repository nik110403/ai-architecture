#!/usr/bin/env python3
"""Documents: create, inspect, check, render and package office files; revision-bound cloud documents.

Subcommands: tools status, create, inspect, check-formulas, render, package, verify-package, cloud
(read|write|create). Stdlib only, offline. A stdlib writer and reader for docx/xlsx/pptx/pdf always run;
optional local converters (soffice, pdftotext, pandoc) are observed with --version and used only behind
--allow-tool. Providers are external connector commands declared in a connectors file, observed with
--describe and spawned only behind --allow-connector; the one built-in connector is `sim`, a local JSON
store with revisions. Nothing here sends, uploads or shares anything, and no command has a bypass flag.

Exit codes: 0 ok, 1 check failed, 2 refused or invalid input, 3 revision conflict or existing target,
4 connector unavailable or operation not offered, 5 tool unavailable. Every result is one JSON object on
stdout with --format json, carrying `status` and, for any non-zero exit, a machine-readable `reason`.
"""

import argparse
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

TOOLS = ("soffice", "pdftotext", "pandoc")
VERSION_FLAG = {"pdftotext": "-v"}  # poppler has no --version; everything else answers --version
MAX_FILE = 64 * 1024 * 1024
MAX_PART = 16 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_RATIO = 100
MAX_JSON = 1024 * 1024
STATUS = {1: "fail", 2: "refused", 3: "conflict", 4: "refused", 5: "unavailable"}
CELL = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
CRED_KEY = re.compile(r"token|password|passwd|secret|api[_-]?key|cookie", re.I)
TOKEN_VALUE = re.compile(
    r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{16,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # same shape as modules/writing/writing.py
PRIVATE = re.compile("/" + "home/|C:\\\\+Users", re.I)
NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
NS_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
NS_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
KIND_CT = {"docx": "wordprocessingml.document.main+xml", "xlsx": "spreadsheetml.sheet.main+xml",
           "pptx": "presentationml.presentation.main+xml"}


class Exit(Exception):
    def __init__(self, code, reason, **extra):
        super().__init__(reason)
        self.code, self.reason, self.extra = code, reason, extra


# ---- shared helpers (productivity.py imports these) ---------------------------------------------------------

def unique_keys(pairs):  # same shape as modules/contracts/contracts.py unique_keys
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(data, what="json"):
    try:
        return json.loads(data, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError) as error:
        raise Exit(2, "duplicate_key" if "duplicate" in str(error) else "invalid_json", what=what)


def read_bytes(path, what, limit=MAX_JSON):
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise Exit(2, "unreadable_file", what=what)
    if len(data) > limit:
        raise Exit(2, "too_large", what=what)
    return data


def load_json(path, what):
    data = read_bytes(path, what)
    try:
        return parse_json(data.decode("utf-8"), what)
    except UnicodeDecodeError:
        raise Exit(2, "invalid_json", what=what)


def canon(obj):  # same canonical form as modules/browser/backends.py canon
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o600):  # same shape as modules/writing/writing.py atomic_write
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def pretty(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def reject_secrets(obj, what, *, emails=True, private=True, depth=0):
    """Refuse credential-shaped keys, token-shaped values, email addresses and private paths anywhere in obj."""
    if depth > 40:
        raise Exit(2, "too_deep", what=what)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if CRED_KEY.search(str(key)):
                raise Exit(2, "credential_key", what=what)
            reject_secrets(value, what, emails=emails, private=private, depth=depth + 1)
        for key in obj:
            reject_secrets(str(key), what, emails=emails, private=private, depth=depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            reject_secrets(value, what, emails=emails, private=private, depth=depth + 1)
    elif isinstance(obj, str):
        if TOKEN_VALUE.search(obj):
            raise Exit(2, "token_value", what=what)
        if emails and EMAIL.search(obj):
            raise Exit(2, "email_address", what=what)
        if private and PRIVATE.search(obj):
            raise Exit(2, "private_path", what=what)


def emit(result, fmt, stream=None):
    stream = stream or sys.stdout
    if fmt == "json":
        stream.write(pretty(result))
    else:
        for key, value in result.items():
            stream.write(f"{key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}\n")


def run_cli(build, argv, prog):
    args = build().parse_args(argv)
    try:
        code, result = args.func(args)
    except Exit as error:
        code, result = error.code, {"status": STATUS[error.code], "reason": error.reason, **error.extra}
    emit(result, args.format, sys.stderr if code and args.format == "text" else None)
    return code


# ---- environment, timeouts and process groups ---------------------------------------------------------------

def child_env(home):
    """Allow-list environment for tools and connectors: PATH, LANG, HOME (a throwaway profile dir)."""
    env = {"LANG": "C.UTF-8", "HOME": str(home)}
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path
    return env


def _seconds(text, default):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def tool_timeout(default):
    return _seconds(os.environ.get("DOCUMENTS_TOOL_TIMEOUT"), default)


def connector_timeout(default=30.0):
    return _seconds(os.environ.get("DOCUMENTS_CONNECTOR_TIMEOUT"), default)


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def run_group(argv, *, timeout, env, cwd=None, stdin=None, limit=MAX_JSON):
    """Run argv in its own session; on timeout (and afterwards) kill the whole group so a grandchild cannot
    keep the pipes open. Raises subprocess.TimeoutExpired or OSError; returns (returncode, stdout, stderr)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd, shell=False,
                            start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        kill_group(proc)
    return proc.returncode, out[:limit], err[:limit]


# ---- optional tools ------------------------------------------------------------------------------------------

def observe_tool(name):
    path = shutil.which(name)
    if not path:
        return {"status": "unavailable", "reason": "not found on PATH"}
    seconds = tool_timeout(10)
    with tempfile.TemporaryDirectory() as home:
        try:
            code, out, err = run_group([path, VERSION_FLAG.get(name, "--version")], timeout=seconds, env=child_env(home))
        except subprocess.TimeoutExpired:
            return {"status": "unavailable", "reason": f"timeout after {seconds:g}s"}
        except OSError as error:
            return {"status": "unavailable", "reason": f"cannot start: {type(error).__name__}"}
    if code != 0:
        return {"status": "unavailable", "reason": f"version exited {code}"}
    found = re.search(r"\d+\.\d+(?:\.\d+)?", (out + err).decode("utf-8", "replace")[:300])
    if not found:
        return {"status": "unavailable", "reason": "version output not recognized"}
    return {"status": "available", "version": found.group(0)}


def cmd_tools(a):
    return 0, {"status": "ok", "tools": {name: observe_tool(name) for name in TOOLS}}


# ---- stdlib writers ------------------------------------------------------------------------------------------

def bad_spec(why):
    raise Exit(2, "bad_spec", detail=why)


def text_ok(value, what):
    if not isinstance(value, str) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        bad_spec(f"{what} must be a string without control characters")
    return value


def zip_bytes(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            z.writestr(info, data if isinstance(data, bytes) else data.encode("utf-8"))
    return buf.getvalue()


XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OD = "http://schemas.openxmlformats.org/officeDocument/2006"
CT_HEAD = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' \
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' \
          '<Default Extension="xml" ContentType="application/xml"/>'
CORE_CT = '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'


def core_xml(title):
    # Fixed, generic properties: a title and a constant date, no creator or last-modified-by field.
    return (XML + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{escape(title)}</dc:title><dcterms:created xsi:type="dcterms:W3CDTF">2000-01-01T00:00:00Z'
            '</dcterms:created></cp:coreProperties>')


def root_rels(main_type, main_target):
    return (XML + f'<Relationships xmlns="{REL_NS}"><Relationship Id="rId1" Type="{OD}/relationships/{main_type}" '
            f'Target="{main_target}"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
            'relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>')


def rels(items):
    body = "".join(f'<Relationship Id="{i}" Type="{OD}/relationships/{t}" Target="{target}"/>' for i, t, target in items)
    return XML + f'<Relationships xmlns="{REL_NS}">{body}</Relationships>'


def make_docx(spec):
    paragraphs = spec.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        bad_spec("docx needs a non-empty paragraphs list")
    title = text_ok(spec.get("title", ""), "title")
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(text_ok(p, "paragraph"))}</w:t></w:r></w:p>'
                   for p in paragraphs)
    document = (XML + f'<w:document xmlns:w="{NS_W[1:-1]}"><w:body>{body}<w:sectPr/></w:body></w:document>')
    ct = (XML + CT_HEAD + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.wordprocessingml.document.main+xml"/>' + CORE_CT + '</Types>')
    return zip_bytes([("[Content_Types].xml", ct), ("_rels/.rels", root_rels("officeDocument", "word/document.xml")),
                      ("docProps/core.xml", core_xml(title)), ("word/document.xml", document)])


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def num_text(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        bad_spec("numbers must be finite")
    return repr(value)


def make_xlsx(spec):
    sheets = spec.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        bad_spec("xlsx needs a non-empty sheets list")
    names, sheet_xml = [], []
    for sheet in sheets:
        name = sheet.get("name") if isinstance(sheet, dict) else None
        if not isinstance(name, str) or not 0 < len(name) <= 31 or re.search(r"[\[\]:*?/\\<>&\"']", name) or name in names:
            bad_spec("sheet names must be unique, 1-31 characters, without []:*?/\\<>&\"'")
        cells = sheet.get("cells")
        if not isinstance(cells, dict):
            bad_spec("sheet needs a cells object")
        rows = {}
        for ref, value in cells.items():
            m = CELL.match(ref)
            if not m:
                bad_spec(f"bad cell reference {ref}")
            rows.setdefault(int(m.group(2)), []).append((col_index(m.group(1)), ref, value))
        out = []
        for row in sorted(rows):
            cs = []
            for _, ref, value in sorted(rows[row]):
                if isinstance(value, dict):
                    if set(value) != {"f", "v"} or not isinstance(value["f"], str):
                        bad_spec("a formula cell is {f, v}")
                    cs.append(f'<c r="{ref}"><f>{escape(text_ok(value["f"], "formula"))}</f><v>{num_text(value["v"])}</v></c>')
                elif isinstance(value, str):
                    cs.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text_ok(value, "cell"))}'
                              '</t></is></c>')
                else:
                    cs.append(f'<c r="{ref}"><v>{num_text(value)}</v></c>')
            out.append(f'<row r="{row}">{"".join(cs)}</row>')
        names.append(name)
        sheet_xml.append(XML + f'<worksheet xmlns="{NS_S[1:-1]}"><sheetData>{"".join(out)}</sheetData></worksheet>')
    ct = (XML + CT_HEAD + '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'spreadsheetml.sheet.main+xml"/>' + "".join(
              f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              f'spreadsheetml.worksheet+xml"/>' for i in range(1, len(names) + 1)) + CORE_CT + "</Types>")
    workbook = (XML + f'<workbook xmlns="{NS_S[1:-1]}" xmlns:r="{NS_R[1:-1]}"><sheets>' + "".join(
        f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1)) + "</sheets></workbook>")
    wb_rels = rels([(f"rId{i}", "worksheet", f"worksheets/sheet{i}.xml") for i in range(1, len(names) + 1)])
    parts = [("[Content_Types].xml", ct), ("_rels/.rels", root_rels("officeDocument", "xl/workbook.xml")),
             ("docProps/core.xml", core_xml("")), ("xl/workbook.xml", workbook), ("xl/_rels/workbook.xml.rels", wb_rels)]
    parts += [(f"xl/worksheets/sheet{i}.xml", x) for i, x in enumerate(sheet_xml, 1)]
    return zip_bytes(parts)


NS_DECL = f'xmlns:a="{NS_A[1:-1]}" xmlns:r="{NS_R[1:-1]}" xmlns:p="{NS_P[1:-1]}"'
SP_TREE_HEAD = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
                '</a:xfrm></p:grpSpPr>')
CLR_SCHEME = "".join(f'<a:{n}><a:srgbClr val="{v}"/></a:{n}>' for n, v in (
    ("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "1F497D"), ("lt2", "EEECE1"), ("accent1", "4F81BD"),
    ("accent2", "C0504D"), ("accent3", "9BBB59"), ("accent4", "8064A2"), ("accent5", "4BACC6"), ("accent6", "F79646"),
    ("hlink", "0000FF"), ("folHlink", "800080")))
FILL3 = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
LINE3 = '<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3
THEME = (XML + f'<a:theme xmlns:a="{NS_A[1:-1]}" name="Plain"><a:themeElements><a:clrScheme name="Plain">{CLR_SCHEME}'
         '</a:clrScheme><a:fontScheme name="Plain"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/>'
         '</a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
         f'</a:fontScheme><a:fmtScheme name="Plain"><a:fillStyleLst>{FILL3}</a:fillStyleLst><a:lnStyleLst>{LINE3}</a:lnStyleLst>'
         '<a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 + '</a:effectStyleLst><a:bgFillStyleLst>'
         f'{FILL3}</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>')


def shape(sid, name, y, height, paragraphs, size):
    paras = "".join(f'<a:p><a:r><a:rPr lang="en-US" sz="{size}"/><a:t>{escape(p)}</a:t></a:r></a:p>' for p in paragraphs)
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm>'
            f'<a:off x="457200" y="{y}"/><a:ext cx="8229600" cy="{height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/>'
            f'</a:prstGeom></p:spPr><p:txBody><a:bodyPr wrap="square"/><a:lstStyle/>{paras}</p:txBody></p:sp>')


def make_pptx(spec):
    slides = spec.get("slides")
    if not isinstance(slides, list) or not slides:
        bad_spec("pptx needs a non-empty slides list")
    slide_xml = []
    for slide in slides:
        if not isinstance(slide, dict):
            bad_spec("a slide is an object")
        title = text_ok(slide.get("title", ""), "title")
        body = slide.get("body", [])
        if not isinstance(body, list):
            bad_spec("body is a list")
        shapes = shape(2, "Title", 274638, 1143000, [title], 3200) + shape(
            3, "Body", 1600200, 4525963, [text_ok(b, "body line") for b in body] or [""], 2000)
        slide_xml.append(XML + f'<p:sld {NS_DECL}><p:cSld><p:spTree>{SP_TREE_HEAD}{shapes}</p:spTree></p:cSld>'
                         '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
    n = len(slide_xml)
    ct = (XML + CT_HEAD + '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="'
          'application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/'
          'slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.'
          'slideLayout+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.theme+xml"/>' + "".join(
              f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              f'presentationml.slide+xml"/>' for i in range(1, n + 1)) + CORE_CT + "</Types>")
    presentation = (XML + f'<p:presentation {NS_DECL}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
                    '</p:sldMasterIdLst><p:sldIdLst>' + "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>'
                                                                for i in range(1, n + 1)) +
                    '</p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    master = (XML + f'<p:sldMaster {NS_DECL}><p:cSld><p:spTree>{SP_TREE_HEAD}</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" '
              'bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
              'accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" '
              'r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>')
    layout = (XML + f'<p:sldLayout {NS_DECL} type="blank"><p:cSld name="Blank"><p:spTree>{SP_TREE_HEAD}</p:spTree></p:cSld>'
              '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    parts = [("[Content_Types].xml", ct), ("_rels/.rels", root_rels("officeDocument", "ppt/presentation.xml")),
             ("docProps/core.xml", core_xml("")), ("ppt/presentation.xml", presentation),
             ("ppt/_rels/presentation.xml.rels", rels([("rId1", "slideMaster", "slideMasters/slideMaster1.xml")] + [
                 (f"rId{i + 1}", "slide", f"slides/slide{i}.xml") for i in range(1, n + 1)])),
             ("ppt/slideMasters/slideMaster1.xml", master),
             ("ppt/slideMasters/_rels/slideMaster1.xml.rels", rels([("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
                                                                    ("rId2", "theme", "../theme/theme1.xml")])),
             ("ppt/slideLayouts/slideLayout1.xml", layout),
             ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", rels([("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")])),
             ("ppt/theme/theme1.xml", THEME)]
    for i, x in enumerate(slide_xml, 1):
        parts += [(f"ppt/slides/slide{i}.xml", x),
                  (f"ppt/slides/_rels/slide{i}.xml.rels", rels([("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")]))]
    return zip_bytes(parts)


def pdf_escape(text):
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError:
        bad_spec("pdf text must be Latin-1/cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(spec):
    pages = spec.get("pages")
    if not isinstance(pages, list) or not pages:
        bad_spec("pdf needs a non-empty pages list of line lists")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for index, lines in enumerate(pages):
        if not isinstance(lines, list) or not lines or len(lines) > 45:
            bad_spec("a page is a list of 1-45 lines")
        stream = b"BT /F1 12 Tf 14 TL 72 720 Td\n"
        for line in lines:
            if len(text_ok(line, "line")) > 80:
                bad_spec("a line holds at most 80 characters")
            stream += b"(" + pdf_escape(line) + b") Tj T*\n"
        stream += b"ET"
        page_no = 4 + 2 * index
        kids.append(f"{page_no} 0 R")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {page_no + 1} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, start)
    return out


MAKERS = {"docx": make_docx, "xlsx": make_xlsx, "pptx": make_pptx, "pdf": make_pdf}


def cmd_create(a):
    spec = load_json(a.spec, "spec")
    if not isinstance(spec, dict):
        bad_spec("spec must be a JSON object")
    data = MAKERS[a.kind](spec)
    atomic_write(a.out, data, 0o644)
    return 0, {"status": "ok", "kind": a.kind, "sha256": sha(data), "bytes": len(data)}


# ---- stdlib reader -------------------------------------------------------------------------------------------

def unsafe_name(name):
    parts = re.split(r"[\\/]", name)
    return name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) is not None or ".." in parts or "\0" in name


def open_zip(data):
    """Open an archive in memory, refusing hostile structure before any part is read. Never extracts to disk."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Exit(2, "truncated" if b"PK\x05\x06" not in data[-66000:] else "corrupt")
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise Exit(2, "too_many_entries")
    seen, total = set(), 0
    for info in infos:
        name = info.filename
        if unsafe_name(name):
            raise Exit(2, "path_traversal")
        if name in seen:
            raise Exit(2, "duplicate_entry")
        seen.add(name)
        if name.rsplit("/", 1)[-1].lower() == "vbaproject.bin":
            raise Exit(2, "macro_present")
        if info.file_size > 64 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise Exit(2, "expansion_ratio")
        total += info.file_size
    if total > 4 * MAX_PART:
        raise Exit(2, "too_large")
    for name in sorted(n for n in seen if n.endswith(".rels")):
        for element in parse_xml(read_part(zf, name)).iter():  # parsed, so character references and encodings are decoded
            if element.get("TargetMode", "").strip().lower() == "external":
                raise Exit(2, "external_relationship")
    return zf


def read_part(zf, name, limit=MAX_PART):
    try:
        with zf.open(name) as handle:
            data = handle.read(limit + 1)
    except KeyError:
        raise Exit(2, "corrupt", detail="missing part")
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
        raise Exit(2, "corrupt")
    if len(data) > limit:
        raise Exit(2, "too_large")
    return data


def parse_xml(data):
    """UTF-8 only: a UTF-16 or other-encoded part would hide a DOCTYPE from the byte screen below."""
    declared = re.match(rb"\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)", data)
    if b"\x00" in data or data[:2] in (b"\xff\xfe", b"\xfe\xff") or (
            declared and declared.group(1).decode("ascii", "replace").lower().replace("_", "-") not in ("utf-8", "utf8")):
        raise Exit(2, "corrupt", detail="xml encoding")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise Exit(2, "dtd_present")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise Exit(2, "corrupt", detail="xml")


def resolve(base_part, target):
    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))
    if path.startswith(".."):
        raise Exit(2, "path_traversal")
    return path


def part_rels(zf, part):
    name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if name not in zf.namelist():
        return {}
    return {r.get("Id"): resolve(part, r.get("Target", "")) for r in parse_xml(read_part(zf, name)).iter(NS_REL + "Relationship")}


def num(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() and not re.search(r"[.eE]", text) else value


def paragraphs_of(root, ns, text_tag):
    return ["".join(t.text or "" for t in p.iter(text_tag)) for p in root.iter(ns + "p")]


def kind_of(zf):
    names = zf.namelist()
    if "[Content_Types].xml" not in names:
        raise Exit(2, "unknown_format")
    root = parse_xml(read_part(zf, "[Content_Types].xml"))
    parts = {o.get("PartName", "").lstrip("/"): o.get("ContentType", "") for o in root.iter(NS_CT + "Override")}
    for kind, suffix in KIND_CT.items():
        for part, ctype in parts.items():
            if ctype.endswith(suffix):
                return kind, part
    raise Exit(2, "unknown_format")


def core_title(zf):
    if "docProps/core.xml" not in zf.namelist():
        return ""
    node = parse_xml(read_part(zf, "docProps/core.xml")).find("{http://purl.org/dc/elements/1.1/}title")
    return (node.text or "") if node is not None else ""


def read_docx(zf, part):
    root = parse_xml(read_part(zf, part))
    return {"paragraphs": paragraphs_of(root, NS_W, NS_W + "t"), "title": core_title(zf)}


def read_xlsx(zf, part):
    relmap = part_rels(zf, part)
    shared = []
    for target in relmap.values():
        if target.endswith("sharedStrings.xml"):
            shared = ["".join(t.text or "" for t in si.iter(NS_S + "t"))
                      for si in parse_xml(read_part(zf, target)).iter(NS_S + "si")]
    sheets = []
    for sheet in parse_xml(read_part(zf, part)).iter(NS_S + "sheet"):
        target = relmap.get(sheet.get(NS_R + "id"))
        if target is None:
            raise Exit(2, "corrupt", detail="sheet relationship")
        cells, formulas = {}, {}
        for c in parse_xml(read_part(zf, target)).iter(NS_S + "c"):
            ref, kind = c.get("r"), c.get("t")
            v, f = c.find(NS_S + "v"), c.find(NS_S + "f")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(NS_S + "t"))
            elif kind == "s" and v is not None:
                value = shared[int(v.text)] if (v.text or "").isdigit() and int(v.text) < len(shared) else None
            elif kind in ("str", "e") and v is not None:
                value = v.text or ""
            elif kind == "b" and v is not None:
                value = v.text == "1"
            else:
                value = num(v.text) if v is not None else None
            if f is not None:
                formulas[ref] = f.text or ""
            if value is not None or f is not None:
                cells[ref] = value
        sheets.append({"name": sheet.get("name"), "cells": cells, "formulas": formulas})
    return {"sheets": sheets}


def read_pptx(zf, part):
    relmap = part_rels(zf, part)
    slides = []
    for sld in parse_xml(read_part(zf, part)).iter(NS_P + "sldId"):
        target = relmap.get(sld.get(NS_R + "id"))
        if target is None:
            raise Exit(2, "corrupt", detail="slide relationship")
        slides.append({"text": [t for t in paragraphs_of(parse_xml(read_part(zf, target)), NS_A, NS_A + "t") if t]})
    return {"slide_count": len(slides), "slides": slides}


def read_pdf(data):
    if b"%%EOF" not in data[-2048:] or b"startxref" not in data[-2048:]:
        raise Exit(2, "truncated")
    pages = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        body = m.group(1)
        head = data[max(0, m.start() - 300):m.start()]
        if b"FlateDecode" in head:
            try:
                body = zlib.decompressobj().decompress(body, MAX_PART)
            except zlib.error:
                raise Exit(2, "corrupt", detail="pdf stream")
        lines = []
        for t in re.finditer(rb"\(((?:\\.|[^\\()])*)\)\s*Tj", body, re.S):
            raw = re.sub(rb"\\([nrtbf()\\])|\\([0-7]{1,3})", pdf_unescape, t.group(1))
            lines.append(raw.decode("cp1252", "replace"))
        if lines or b"BT" in body:
            pages.append(lines)
    count = len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))
    return {"page_count": count, "pages": pages[:count] if count else pages}


def pdf_unescape(m):
    if m.group(1):
        return {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(m.group(1), m.group(1))
    return bytes([int(m.group(2), 8) & 255])


def inspect_file(path):
    data = read_bytes(path, "document", MAX_FILE)
    if data.startswith(b"%PDF-"):
        return {"kind": "pdf", **read_pdf(data)}
    if data.startswith(b"PK\x03\x04"):
        zf = open_zip(data)
        kind, part = kind_of(zf)
        return {"kind": kind, **{"docx": read_docx, "xlsx": read_xlsx, "pptx": read_pptx}[kind](zf, part)}
    raise Exit(2, "unknown_format")


def cmd_inspect(a):
    return 0, {"status": "ok", **inspect_file(a.file)}


# ---- formulas ------------------------------------------------------------------------------------------------

class Unsupported(Exception):
    pass


class Cycle(Exception):
    pass


class DivZero(Exception):
    pass


TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)|([A-Za-z_][A-Za-z0-9_.]*)|(.))")


def tokenize(text):
    tokens, pos = [], 0
    while pos < len(text.rstrip()):
        m = TOKEN.match(text, pos)
        if not m:
            break
        pos = m.end()
        tokens.append(("num", m.group(1)) if m.group(1) else ("id", m.group(2)) if m.group(2) else ("op", m.group(3)))
    return tokens


class Evaluator:
    """Closed grammar: numbers, same-sheet cell refs, + - * /, parentheses, SUM(range). Anything else is unsupported."""

    def __init__(self, cells, formulas):
        self.cells, self.formulas, self.memo, self.active = cells, formulas, {}, set()

    def value(self, ref):
        if ref in self.formulas:
            if ref in self.memo:
                return self.memo[ref]
            if ref in self.active:
                raise Cycle(ref)
            self.active.add(ref)
            try:
                self.memo[ref] = self.evaluate(self.formulas[ref])
            finally:
                self.active.discard(ref)
            return self.memo[ref]
        v = self.cells.get(ref)
        if v is None:
            return 0
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise Unsupported("non-numeric reference " + ref)
        return v

    def evaluate(self, text):
        saved = getattr(self, "toks", None), getattr(self, "pos", 0)  # value() re-enters mid-parse on forward references
        self.toks, self.pos = tokenize(text), 0
        try:
            result = self.expr()
            if self.pos != len(self.toks):
                raise Unsupported("unexpected token")
            return result
        finally:
            self.toks, self.pos = saved

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def take(self, op=None):
        tok = self.peek()
        if op is not None and tok != ("op", op):
            raise Unsupported("expected " + op)
        self.pos += 1
        return tok

    def expr(self):
        left = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            right = self.term()
            left = left + right if op == "+" else left - right
        return left

    def term(self):
        left = self.factor()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            right = self.factor()
            if op == "*":
                left *= right
            elif right == 0:
                raise DivZero()
            else:
                left /= right
        return left

    def factor(self):
        kind, text = self.take()
        if kind == "num":
            return num(text) if num(text) is not None else float(text)
        if kind == "op" and text == "-":
            return -self.factor()
        if kind == "op" and text == "+":
            return self.factor()
        if kind == "op" and text == "(":
            value = self.expr()
            self.take(")")
            return value
        if kind == "id" and self.peek() == ("op", "("):
            if text != "SUM":
                raise Unsupported("function " + text)
            self.take("(")
            total = self.sum_range()
            self.take(")")
            return total
        if kind == "id" and CELL.match(text):
            if self.peek() == ("op", "!") or self.peek() == ("op", ":"):
                raise Unsupported("reference form")
            return self.value(text)
        raise Unsupported("token " + str(text))

    def sum_range(self):
        kind, first = self.take()
        if kind != "id" or not CELL.match(first):
            raise Unsupported("SUM needs a range")
        self.take(":")
        kind, last = self.take()
        if kind != "id" or not CELL.match(last):
            raise Unsupported("SUM needs a range")
        (c1, r1), (c2, r2) = (CELL.match(first).groups(), CELL.match(last).groups())
        cols = range(min(col_index(c1), col_index(c2)), max(col_index(c1), col_index(c2)) + 1)
        rows = range(min(int(r1), int(r2)), max(int(r1), int(r2)) + 1)
        if len(cols) * len(rows) > 100000:
            raise Unsupported("range too large")
        total = 0
        for r in rows:
            for c in cols:
                ref = col_letters(c) + str(r)
                if ref in self.formulas or isinstance(self.cells.get(ref), (int, float)) and not isinstance(self.cells.get(ref), bool):
                    total += self.value(ref)
        return total


def col_letters(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def cmd_check_formulas(a):
    info = inspect_file(a.file)
    if info["kind"] != "xlsx":
        raise Exit(2, "not_xlsx", kind=info["kind"])
    unsupported, cycle, div, mismatches, count = None, None, None, [], 0
    for sheet in info["sheets"]:
        ev = Evaluator(sheet["cells"], sheet["formulas"])
        order = sorted(sheet["formulas"], key=lambda r: (int(CELL.match(r).group(2)), col_index(CELL.match(r).group(1)))
                       if CELL.match(r) else (0, 0))
        for ref in order:
            name, count = f"{sheet['name']}!{ref}", count + 1
            if not CELL.match(ref):
                unsupported = unsupported or (name, "cell reference")
                continue
            try:
                computed = ev.value(ref)
            except Unsupported as error:
                unsupported = unsupported or (name, str(error))
                continue
            except Cycle:
                cycle = cycle or name
                continue
            except DivZero:
                div = div or name
                continue
            cached = sheet["cells"].get(ref)
            if isinstance(cached, bool) or not isinstance(cached, (int, float)) or not math.isclose(
                    computed, cached, rel_tol=1e-9, abs_tol=1e-9):
                mismatches.append({"cell": name, "expected": computed, "cached": cached})
    if unsupported:
        return 1, {"status": "fail", "reason": "unsupported", "cell": unsupported[0], "detail": unsupported[1]}
    if cycle:
        return 1, {"status": "fail", "reason": "cycle", "cell": cycle}
    if div:
        return 1, {"status": "fail", "reason": "div_by_zero", "cell": div}
    if mismatches:
        return 1, {"status": "fail", "reason": "mismatch", "cell": mismatches[0]["cell"], "mismatches": mismatches}
    return 0, {"status": "pass", "formulas": count}


# ---- render --------------------------------------------------------------------------------------------------

def spec_strings(spec, kind):
    """Strings a render must show. A docx title lives in the properties and an xlsx sheet name in the tab, so
    neither is expected in the page text."""
    out = []

    def walk(node):
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key not in ("f", "name") and not (kind == "docx" and key == "title"):
                    walk(value)
    walk(spec)
    return out


def file_strings(info):
    if info["kind"] == "docx":
        return [p for p in info["paragraphs"] if p]
    if info["kind"] == "xlsx":
        return [v for s in info["sheets"] for v in s["cells"].values() if isinstance(v, str)]
    if info["kind"] == "pptx":
        return [t for s in info["slides"] for t in s["text"]]
    return [line for page in info["pages"] for line in page]


def cmd_render(a):
    info = inspect_file(a.file)
    spec = load_json(a.spec, "spec") if a.spec else None
    expected = [s for s in (spec_strings(spec, info["kind"]) if spec is not None else file_strings(info)) if s.strip()]
    needs = ["pdftotext"] if info["kind"] == "pdf" else ["soffice", "pdftotext"]
    allowed = set(a.allow_tool or [])
    if not allowed >= set(needs):
        raise Exit(2, "tool_not_allowed", needs=needs)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    receipt = {"status": "fail", "kind": info["kind"], "expected": expected, "missing": expected, "tools": {}}

    def finish(code, **fields):
        receipt.update(fields)
        atomic_write(out / "render-receipt.json", pretty(receipt))
        return code, {k: v for k, v in receipt.items() if k != "expected"}

    for name in needs:
        receipt["tools"][name] = observe_tool(name)
    down = [n for n in needs if receipt["tools"][n]["status"] != "available"]
    if down:
        return finish(5, status="unavailable", reason=f"{down[0]}: {receipt['tools'][down[0]]['reason']}")
    seconds = tool_timeout(120)
    pdf = Path(a.file)
    with tempfile.TemporaryDirectory() as home:
        env = child_env(home)
        if info["kind"] != "pdf":
            work = out / "converted"
            work.mkdir(exist_ok=True)
            source = work / f"doc.{info['kind']}"
            shutil.copyfile(a.file, source)
            profile = (out / "profile").resolve()
            argv = [shutil.which("soffice"), "--headless", f"-env:UserInstallation=file://{profile}", "--convert-to", "pdf",
                    "--outdir", str(work), str(source)]
            try:
                code, _, _ = run_group(argv, timeout=seconds, env=env)
            except subprocess.TimeoutExpired:
                return finish(5, status="unavailable", reason=f"soffice timeout after {seconds:g}s")
            except OSError as error:
                return finish(5, status="unavailable", reason=f"soffice cannot start: {type(error).__name__}")
            pdf = work / "doc.pdf"
            if code != 0:
                return finish(1, status="fail", reason="convert_failed")
            if not pdf.is_file():
                return finish(1, status="fail", reason="no_pdf")
        try:
            code, text, _ = run_group([shutil.which("pdftotext"), "-q", str(pdf), "-"], timeout=seconds, env=env)
        except subprocess.TimeoutExpired:
            return finish(5, status="unavailable", reason=f"pdftotext timeout after {seconds:g}s")
        except OSError as error:
            return finish(5, status="unavailable", reason=f"pdftotext cannot start: {type(error).__name__}")
    if code != 0:
        return finish(1, status="fail", reason="text_extraction_failed")
    flat = " ".join(text.decode("utf-8", "replace").split())
    missing = [s for s in expected if " ".join(s.split()) not in flat]
    if missing:
        return finish(1, status="fail", reason="text_missing", missing=missing)
    return finish(0, status="pass", missing=[])


# ---- packages ------------------------------------------------------------------------------------------------

def cmd_package(a):
    src, out = Path(a.indir).resolve(), Path(a.out).resolve()
    if not src.is_dir():
        raise Exit(2, "not_a_directory")
    files = []
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise Exit(2, "symlink_in_input")
        if path.is_file() and path != out:
            files.append(path)
    if not files or len(files) > MAX_ENTRIES - 1:
        raise Exit(2, "bad_file_count")
    entries, manifest = [], []
    for path in files:
        rel = path.relative_to(src).as_posix()
        if rel == "manifest.json":
            raise Exit(2, "reserved_name")
        data = path.read_bytes()
        manifest.append({"path": rel, "sha256": sha(data), "size": len(data)})
        entries.append((rel, data))
    body = pretty({"version": 1, "files": manifest})
    atomic_write(out, zip_bytes([("manifest.json", body)] + entries), 0o644)
    return 0, {"status": "ok", "files": len(manifest)}


def cmd_verify_package(a):
    zf = open_zip(read_bytes(a.file, "package", MAX_FILE))
    try:
        if "manifest.json" not in zf.namelist():
            return 1, {"status": "fail", "reason": "manifest_missing"}
        manifest = parse_json(read_part(zf, "manifest.json").decode("utf-8", "replace"), "manifest")
        listed = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(listed, list) or not all(isinstance(f, dict) and {"path", "sha256"} <= set(f) for f in listed):
            return 1, {"status": "fail", "reason": "manifest_invalid"}
        names = {n for n in zf.namelist() if n != "manifest.json" and not n.endswith("/")}
        for entry in listed:
            if entry["path"] not in names:
                return 1, {"status": "fail", "reason": "missing_file", "path": entry["path"]}
            if sha(read_part(zf, entry["path"])) != entry["sha256"]:
                return 1, {"status": "fail", "reason": "hash_mismatch", "path": entry["path"]}
        extra = sorted(names - {e["path"] for e in listed})
        if extra:
            return 1, {"status": "fail", "reason": "unlisted_file", "path": extra[0]}
    except Exit as error:
        if error.reason == "corrupt":
            return 1, {"status": "fail", "reason": "corrupt"}
        raise
    return 0, {"status": "pass", "files": len(listed)}


# ---- connectors ----------------------------------------------------------------------------------------------

def load_connectors(path):
    doc = load_json(path, "connectors")
    reject_secrets(doc, "connectors", private=False)  # argv may hold absolute local paths
    table = doc.get("connectors") if isinstance(doc, dict) and set(doc) <= {"version", "connectors"} else None
    if not isinstance(table, dict):
        raise Exit(2, "bad_connectors")
    base = Path(path).resolve().parent
    result = {}
    for name, spec in table.items():
        ok = isinstance(spec, dict) and set(spec) <= {"argv", "operations", "kind"} and isinstance(spec.get("argv"), list) \
            and spec["argv"] and all(isinstance(x, str) for x in spec["argv"])
        if not ok:
            raise Exit(2, "bad_connectors", connector=name)
        result[name] = ([str(base / x[2:]) if x.startswith("./") else x for x in spec["argv"]], base)
    return result


def observe_connector(entry):
    """Run `argv --describe` (nothing on stdin) and report what the connector itself says, never what the file claims."""
    argv, base = entry
    seconds = connector_timeout(10.0)
    with tempfile.TemporaryDirectory() as home:
        try:
            code, out, _ = run_group(argv + ["--describe"], timeout=seconds, env=child_env(home), cwd=base)
        except subprocess.TimeoutExpired:
            return {"status": "unavailable", "reason": f"timeout after {seconds:g}s"}
        except OSError as error:
            return {"status": "unavailable", "reason": f"cannot start: {type(error).__name__}"}
    if code != 0:
        return {"status": "unavailable", "reason": f"describe exited {code}"}
    try:
        info = json.loads(out.decode("utf-8"), object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        return {"status": "unavailable", "reason": "describe output is not JSON"}
    ops = info.get("operations") if isinstance(info, dict) else None
    if not isinstance(ops, list) or not all(isinstance(x, str) for x in ops):
        return {"status": "unavailable", "reason": "describe lacks an operations list"}
    return {"status": "available", "operations": ops}


class External:
    def __init__(self, name, entry):
        self.name, self.entry = name, entry

    def call(self, op, request):
        argv, base = self.entry
        with tempfile.TemporaryDirectory() as home:
            try:
                code, out, _ = run_group(argv + ["--op", op], timeout=connector_timeout(), env=child_env(home), cwd=base,
                                         stdin=json.dumps(request).encode("utf-8"))
            except subprocess.TimeoutExpired:
                raise Exit(4, "connector_unavailable", detail="timeout")
            except OSError:
                raise Exit(4, "connector_unavailable", detail="cannot start")  # the only failure known to precede the effect
        if code != 0:
            raise Exit(4, "connector_failed", detail=f"exit {code}")
        try:
            result = json.loads(out.decode("utf-8"), object_pairs_hook=unique_keys)
        except (ValueError, RecursionError):
            raise Exit(4, "connector_failed", detail="output is not JSON")
        if not isinstance(result, dict):
            raise Exit(4, "connector_failed", detail="output is not an object")
        reject_secrets(result, "connector output", emails=False, private=False)
        return result


def get_backend(name, connectors, allow, op, sim):
    """Resolve a backend. `sim` is built in; any other name must be declared in the connectors file, be allowed
    explicitly, answer --describe, and offer `op` itself. Nothing falls back to sim or to a local file."""
    if name == "sim":
        return sim
    if not connectors:
        raise Exit(4, "unknown_connector")
    table = load_connectors(connectors)
    if name not in table:
        raise Exit(4, "unknown_connector")
    if not allow:
        raise Exit(2, "connector_not_allowed")
    info = observe_connector(table[name])
    if info["status"] != "available":
        raise Exit(4, "connector_unavailable", detail=info["reason"])
    if op not in info["operations"]:
        raise Exit(4, "operation_not_offered")
    return External(name, table[name])


# ---- sim store and cloud -------------------------------------------------------------------------------------

def load_store(path):
    if not Path(path).exists():
        return {"version": 1}
    store = load_json(path, "store")
    if not isinstance(store, dict):
        raise Exit(2, "bad_store")
    return store


def save_store(path, store):
    atomic_write(path, pretty(store))


def next_revision(rev):
    if not isinstance(rev, str) or not re.fullmatch(r"r[0-9]+", rev):
        raise Exit(2, "bad_store", detail="revision")
    return "r" + str(int(rev[1:]) + 1)


class Sim:
    name = "sim"

    def __init__(self, path):
        self.path = Path(path)

    def call(self, op, request):
        store = load_store(self.path)
        handler = getattr(self, "op_" + op.replace(".", "_"), None)
        if handler is None:
            raise Exit(4, "operation_not_offered")
        try:
            result = handler(store, request)
        except (KeyError, TypeError, AttributeError):
            raise Exit(2, "bad_store")
        if result.pop("_dirty", False):
            save_store(self.path, store)
        return result

    def doc(self, store, request):
        docs = store.get("docs", {})
        if request["doc"] not in docs:
            raise Exit(3, "unknown_document")
        return docs[request["doc"]]

    def op_read(self, store, request):
        doc = self.doc(store, request)
        return {"revision": doc["revision"], "content": doc["content"], "title": doc["title"]}

    def op_write(self, store, request):
        doc = self.doc(store, request)
        if doc["revision"] != request["base_revision"]:
            raise Exit(3, "stale_revision")
        doc["content"] = request["content"]
        doc["revision"] = next_revision(doc["revision"])
        return {"revision": doc["revision"], "_dirty": True}

    def op_create(self, store, request):
        docs = store.setdefault("docs", {})
        if request["doc"] in docs or any(d.get("title") == request["title"] for d in docs.values()):
            raise Exit(3, "exists")
        docs[request["doc"]] = {"title": request["title"], "content": request["content"], "revision": "r1"}
        return {"revision": "r1", "_dirty": True}


def native_state(backend, evidence_path, doc, before, after):
    """`verified` only for an external connector with disposable-file evidence that matches this receipt."""
    if backend.name == "sim":
        return "unverified", "built-in store, not a provider"
    if not evidence_path:
        return "unverified", "no evidence file"
    try:
        evidence = load_json(evidence_path, "evidence")
    except Exit:
        return "unverified", "evidence unreadable"
    keys = {"disposable", "doc", "revision_before", "revision_after"}
    if not isinstance(evidence, dict) or set(evidence) != keys:
        return "unverified", "evidence shape"
    if evidence["disposable"] is not True:
        return "unverified", "evidence is not from a disposable file"
    if evidence["doc"] != doc or evidence["revision_before"] != before or evidence["revision_after"] != after \
            or before == after:
        return "unverified", "evidence does not match the receipt"
    return "verified", "matching disposable-file evidence"


def read_content(path):
    if not path:
        raise Exit(2, "content_required")
    data = read_bytes(path, "content")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Exit(2, "content_not_utf8")


def cmd_cloud(a):
    op = a.op
    if op == "write" and not a.base_revision:
        raise Exit(2, "base_revision_required")
    if op == "create" and not a.title:
        raise Exit(2, "title_required")
    request = {"doc": a.doc}
    if op == "write":
        request.update(base_revision=a.base_revision, content=read_content(a.content))
    if op == "create":
        request.update(title=a.title, content=read_content(a.content))
    backend = get_backend(a.connector, a.connectors, a.allow_connector, op, Sim(a.store))
    result = backend.call(op, request)
    revision = result.get("revision")
    if not isinstance(revision, str):
        raise Exit(4, "connector_failed", detail="no revision in output")
    receipt = {"status": "ok", "op": op, "connector": backend.name, "doc": a.doc}
    if op == "read":
        content = result.get("content")
        if not isinstance(content, str):
            raise Exit(4, "connector_failed", detail="no content in output")
        receipt.update(revision=revision, content=content, native="unverified", native_reason="read does not change anything")
    else:
        before = a.base_revision if op == "write" else None
        native, why = native_state(backend, a.evidence, a.doc, before, revision)
        receipt.update(revision_before=before, revision_after=revision, content_sha256=sha(request["content"].encode()),
                       native=native, native_reason=why)
    return 0, receipt


# ---- cli -----------------------------------------------------------------------------------------------------

def build():
    top = argparse.ArgumentParser(prog="documents", description="Documents: create, inspect, check, render, package, cloud.")
    sub = top.add_subparsers(dest="cmd", required=True)

    def add(parent, name, func, help_, *flags):
        p = parent.add_parser(name, help=help_, description=help_)
        p.set_defaults(func=func)
        p.add_argument("--format", choices=("text", "json"), default="text")
        for flag, kw in flags:
            p.add_argument(flag, **kw)
        return p

    req = {"required": True}
    tools = sub.add_parser("tools", help="Optional local converters.")
    add(tools.add_subparsers(dest="sub", required=True), "status", cmd_tools, "Observe each optional tool with --version.")
    add(sub, "create", cmd_create, "Write a deterministic docx/xlsx/pptx/pdf from a JSON spec.",
        ("--kind", {"required": True, "choices": sorted(MAKERS)}), ("--spec", req), ("--out", req))
    p = add(sub, "inspect", cmd_inspect, "Report kind and content of a file; refuses hostile archives.")
    p.add_argument("file")
    p = add(sub, "check-formulas", cmd_check_formulas, "Recompute xlsx formulas and compare with cached values.")
    p.add_argument("file")
    p = add(sub, "render", cmd_render, "Convert with observed tools and check the text.",
            ("--out", req), ("--spec", {}), ("--allow-tool", {"action": "append", "choices": list(TOOLS)}))
    p.add_argument("file")
    add(sub, "package", cmd_package, "Zip a directory with a sha256 manifest.", ("--in", {"required": True, "dest": "indir"}),
        ("--out", req))
    p = add(sub, "verify-package", cmd_verify_package, "Check a package against its manifest.")
    p.add_argument("file")
    cloud = sub.add_parser("cloud", help="Revision-bound documents through a connector.")
    cs = cloud.add_subparsers(dest="op", required=True)
    for op in ("read", "write", "create"):
        add(cs, op, cmd_cloud, f"{op.capitalize()} a document through a connector.",
            ("--connector", req), ("--connectors", {}), ("--store", req), ("--doc", req), ("--base-revision", {}),
            ("--title", {}), ("--content", {}), ("--evidence", {}),
            ("--allow-connector", {"action": "store_true", "help": "allow spawning the declared connector command"}))
    return top


def main(argv=None):
    return run_cli(build, argv, "documents")


if __name__ == "__main__":
    sys.exit(main())
