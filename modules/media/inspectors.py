"""Independent inspectors: they read artifact bytes, the spec and (for a recap) the repository, never the builder's notes.

Each inspector produces named checks (pass, fail or error with what was measured). A cheap stage that needs no browser runs
first and ends the inspection at its first failure; the browser stage re-renders the artifact's page and compares what it
finds, and the pixels, with what was stored. The first failing check in a fixed order gives the reason.
Named inspectors.py (not inspect.py): a sibling called inspect would shadow the standard library module."""
import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _sib(name):
    key = "aiarch_media_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


common = _sib("common")
Exit, IFACE, checks = common.Exit, common.IFACE, common.checks
PNG_NAME = {"slides": "slide-1.png", "infographic": "infographic.png", "visual-plan": "visual-plan.png",
            "visual-recap": "visual-recap.png", "system-atlas": "system-atlas.png"}
PIXEL_ID = {"slides": "slide-1"}


class Report:
    def __init__(self, kind):
        self.kind, self.checks, self.failures, self.product = kind, [], [], ""

    def add(self, name, ok, measured=None, reason=None):
        row = {"check": name, "result": "pass" if ok else "fail", "measured": measured}
        if not ok:
            row["reason"] = reason
            self.failures.append(reason)
        self.checks.append(row)
        return ok

    def done(self):
        return {"kind": self.kind, "checks": self.checks, "failures": self.failures, "reason": self.failures[0] if self.failures else None,
                "product": self.product}


def by(facts, key):
    return {e["data"][key]: e for e in facts["entries"] if key in e["data"]}


def listed(facts, key):
    return [e["data"][key] for e in facts["entries"] if key in e["data"]]


def tree_entries(art, report):
    """False (and a failed check) when the artifact dir holds a link or a special file."""
    try:
        common.render.regular_files(art)
    except common.render.Refused:
        report.add("structure", False, None, "non-regular artifact entry")
        return False
    return True


def host_check(report, art, extra_blobs=(), repo=None):
    strings = common.host_strings(art, os.path.dirname(os.path.realpath(art)), repo)
    for name in common.render.regular_files(art):
        if name == "inspection.json":
            continue
        blob = common.read_artifact(art, name)
        if blob is not None and common.host_hit(blob, strings):
            return report.add("host-path", False, {"file": name}, "host path in artifact")
    for blob in extra_blobs:
        if common.host_hit(blob, strings):
            return report.add("host-path", False, {"file": "tool output"}, "host path in artifact")
    return report.add("host-path", True, {"files": len(common.render.regular_files(art))})


def png_check(report, data, canvas, name):
    if data is None:
        return report.add("png", False, None, f"missing artifact: {name}")
    problem = checks.png_problem(data, canvas[0], canvas[1])
    return report.add("png", problem is None, {"size": list(canvas), "problem": problem}, f"png invalid: {problem}")


def fonts_check(report, found, declared):
    extra = [f for f in found if f not in declared]
    return report.add("fonts", not extra, found, f"undeclared font: {extra[0] if extra else ''}")


def external_check(report, external):
    return report.add("external", not external, external, "external request")


def pixel_check(report, fresh, stored, ident):
    if fresh == stored:
        return report.add("pixel", True, {"identical": True, "id": ident})
    return report.add("pixel", common.png_close(fresh, stored), {"identical": False, "id": ident}, f"pixel mismatch: {ident}")


def overflow_check(report, facts, canvas, tall_ok=False):
    ok = facts["scroll"][0] <= canvas[0] and (tall_ok or facts["scroll"][1] <= canvas[1])
    return report.add("overflow", ok, facts["scroll"], "content overflow")


def missing_text(facts_text, expected):
    return [name for name, value in expected if value not in facts_text]


# ---- the four png visuals and the infographic ---------------------------------------------------------------------

def bar_checks(report, spec, facts, stored, canvas):
    stats = spec["stats"]
    rows, bars = by(facts, "row"), by(facts, "stat")
    gone = [s["id"] for s in stats if s["id"] not in rows or s["id"] not in bars
            or s["label"] not in rows[s["id"]]["text"] or common.fmt_num(s["value"]) not in rows[s["id"]]["text"]]
    if spec["title"] not in facts["text"]:
        gone.append("title")
    report.add("text", not gone, gone, f"text missing: {gone[0] if gone else ''}")
    biggest = max(stats, key=lambda s: s["value"])
    wmax = bars[biggest["id"]]["rect"][2] if biggest["id"] in bars else 0
    wrong = []
    for s in stats:
        width = bars[s["id"]]["rect"][2] if s["id"] in bars else None
        if width is None or wmax <= 0 or abs(width / wmax - s["value"] / biggest["value"]) * wmax > 1:
            wrong.append(s["id"])
    report.add("geometry", not wrong, {"max": round(wmax, 2)}, f"bar geometry: {wrong[0] if wrong else ''}")
    # the bars must really be drawn: the pixel at each bar's centre is not the page background
    unpainted = []
    try:
        wanted = {}
        for s in stats:
            if s["id"] in bars and bars[s["id"]]["rect"][2] >= 3:
                x, y, w, h = bars[s["id"]]["rect"]
                wanted.setdefault(int(y + h / 2), []).append((s["id"], int(x + w / 2)))
        head, last = checks.png_header(stored), max(wanted, default=0)
        bpp, background = (4 if head[3] == 6 else 3), None
        for index, line in enumerate(checks.png_rows(stored)):
            if index == 2:
                background = line[8 * bpp:8 * bpp + 3]
            for ident, x in wanted.get(index, []):
                pixel = line[x * bpp:x * bpp + 3]
                if background is not None and max(abs(a - b) for a, b in zip(pixel, background)) <= 16:
                    unpainted.append(ident)
            if index >= last:
                break
    except (ValueError, TypeError, OSError):
        unpainted.append("png")
    report.add("bars-drawn", not unpainted, unpainted, f"bar pixels: {unpainted[0] if unpainted else ''}")


def plan_checks(report, spec, facts):
    steps = {s["id"]: s for s in spec["steps"]}
    nodes = by(facts, "step")
    report.add("nodes", set(listed(facts, "step")) == set(steps) and len(listed(facts, "step")) == len(steps), sorted(nodes),
               "node set mismatch")
    want_edges = {f"{n}->{s['id']}" for s in spec["steps"] for n in s["needs"]}
    got_edges = listed(facts, "edge")
    report.add("edges", set(got_edges) == want_edges and len(got_edges) == len(want_edges), sorted(got_edges), "edge set mismatch")
    rows = by(facts, "file")
    want_files = {f["path"]: f["step"] for f in spec["files"]}
    got_files = listed(facts, "file")
    ok = set(got_files) == set(want_files) and len(got_files) == len(want_files) \
        and all(want_files[p] in rows[p]["text"] for p in want_files if p in rows)
    report.add("files", ok, sorted(got_files), "file set mismatch")
    gone = [i for i, s in steps.items() if i in nodes and s["title"] not in nodes[i]["text"]]
    if spec["plan_id"] not in facts["text"]:
        gone.append("title")
    report.add("text", not gone, gone, f"text missing: {gone[0] if gone else ''}")


def recap_checks(report, spec, facts, ctx):
    want = common.recap_data(ctx.get("repo"), ctx.get("base"), ctx.get("head"))
    rows = {}
    for e in facts["entries"]:
        if "file" in e["data"]:
            rows[e["data"]["file"]] = (e["data"].get("add"), e["data"].get("del"))
    wrong = [p for p in sorted(set(rows) | set(want["files"])) if rows.get(p) != want["files"].get(p)]
    report.add("numstat", not wrong, {"files": len(want["files"])}, f"numstat mismatch: {wrong[0] if wrong else ''}")
    got = listed(facts, "commit")
    subjects = {e["data"]["commit"]: e["text"] for e in facts["entries"] if "commit" in e["data"]}
    ok = got == [c[0] for c in want["commits"]] and all(s in subjects.get(c, "") for c, s in want["commits"])
    report.add("commits", ok, got, "commit set mismatch")
    report.add("text", spec["title"] in facts["text"], None, "text missing: title")


def atlas_twin(spec):
    lines = [f"node {n['id']} {n['kind']} {n['label']}" for n in spec["nodes"]]
    lines += [f"edge {e['from']}->{e['to']} {e['label']}" for e in spec["edges"]]
    lines += ["loop " + ",".join(loop) for loop in spec["loops"]]
    return "\n".join(sorted(lines)) + "\n"


def atlas_checks(report, spec, facts):
    nodes = by(facts, "node")
    report.add("nodes", sorted(listed(facts, "node")) == sorted(n["id"] for n in spec["nodes"]), sorted(nodes), "node set mismatch")
    want_edges = sorted(f"{e['from']}->{e['to']}" for e in spec["edges"])
    report.add("edges", sorted(listed(facts, "edge")) == want_edges, sorted(listed(facts, "edge")), "edge set mismatch")
    report.add("loops", sorted(listed(facts, "loop")) == sorted(",".join(l) for l in spec["loops"]), None, "loop set mismatch")
    gone = [n["id"] for n in spec["nodes"] if n["id"] in nodes and n["label"] not in nodes[n["id"]]["text"]]
    report.add("text", not gone, gone, f"text missing: {gone[0] if gone else ''}")


def inspect_page(kind, spec, art, ctx):
    rep = Report(kind)
    if not tree_entries(art, rep):
        return rep.done()
    canvas = tuple(spec["canvas"]) if kind == "infographic" else (1920, 1080) if kind == "slides" else tuple(IFACE["canvas"][kind])
    stored = common.read_artifact(art, PNG_NAME[kind])
    png_check(rep, stored, canvas, PNG_NAME[kind])
    if common.read_artifact(art, "index.html") is None:
        rep.add("structure", False, None, "missing artifact: index.html")
    if kind == "system-atlas":
        twin = common.read_artifact(art, "atlas.txt")
        rep.add("twin", twin is not None and twin.decode("utf-8", "replace") == atlas_twin(spec), None, "text twin mismatch")
    blobs = []
    if kind == "slides":
        blobs = slides_pdf_checks(rep, spec, art, ctx)
    host_check(rep, art, blobs, ctx.get("repo"))
    if rep.failures:
        return rep.done()
    with common.Session(art, canvas) as session:
        rep.product = session.product
        facts, found, fresh, external = session.facts(), session.fonts(), session.shot(), session.external()
    if kind == "infographic":
        bar_checks(rep, spec, facts, stored, canvas)
    elif kind == "visual-plan":
        plan_checks(rep, spec, facts)
    elif kind == "visual-recap":
        recap_checks(rep, spec, facts, ctx)
    elif kind == "system-atlas":
        atlas_checks(rep, spec, facts)
    elif kind == "slides":
        slides_dom_checks(rep, spec, facts)
    if kind != "infographic":
        overflow_check(rep, facts, canvas, tall_ok=kind == "slides")
    fonts_check(rep, found, ctx["fonts"])
    external_check(rep, external)
    pixel_check(rep, fresh, stored, PIXEL_ID.get(kind, kind))
    return rep.done()


# ---- slides pdf ---------------------------------------------------------------------------------------------------

def poppler(tool, *args):
    exe = shutil.which(tool)
    if not exe:
        raise Exit(3, f"tool unavailable: {tool}")
    code, out, err = common.run_tool([exe, *args], tool)
    return code, out.decode("utf-8", "replace")


def slides_pdf_checks(rep, spec, art, ctx):
    """The PDF checks (poppler, no browser). Returns tool output blobs for the host path scan."""
    pdf = os.path.join(art, "deck.pdf")
    data = common.read_artifact(art, "deck.pdf")
    if data is None:
        rep.add("pdf-structure", False, None, "missing artifact: deck.pdf")
        return []
    rep.add("pdf-structure", data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:], len(data) > 0, "pdf malformed")
    if rep.failures:
        return []
    count = len(spec["slides"])
    code, info = poppler("pdfinfo", pdf)
    pages = re.search(r"^Pages:\s+(\d+)", info, re.M)
    size = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+) pts", info, re.M)
    rep.add("pdf-pages", code == 0 and bool(pages) and int(pages.group(1)) == count, int(pages.group(1)) if pages else None,
            "page count mismatch")
    rep.add("pdf-size", bool(size) and abs(float(size.group(1)) - 1440) <= 1 and abs(float(size.group(2)) - 810) <= 1,
            [size.group(1), size.group(2)] if size else None, "page size mismatch")
    if rep.failures:
        return [info.encode()]
    gone, digests = [], []
    for index, slide in enumerate(spec["slides"], 1):
        _, page_text = poppler("pdftotext", "-f", str(index), "-l", str(index), pdf, "-")
        flat = " ".join(page_text.split())
        digests.append(hashlib.sha256(flat.encode("utf-8")).hexdigest()[:16])
        if " ".join(slide["title"].split()) not in flat:
            gone.append(f"{slide['id']} page {index}")
    rep.add("pdf-text", not gone, digests, f"text missing: {gone[0] if gone else ''}")
    _, listing = poppler("pdffonts", pdf)
    _, names = common.font_names(ctx["fonts"][0])
    bad, unembedded, seen = [], [], []
    for line in listing.splitlines()[2:]:
        match = re.match(r"^(\S+)\s+.+?\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", line)
        if match:
            name = re.sub(r"^[A-Z]{6}\+", "", match.group(1))
            seen.append(name)
            if name not in names:
                bad.append(name)
            elif match.group(2) != "yes":
                unembedded.append(name)
    rep.add("pdf-fonts", bool(seen) and not bad, seen, f"undeclared font: {bad[0] if bad else 'none'}")
    rep.add("pdf-embedded", not unembedded, unembedded, f"font not embedded: {unembedded[0] if unembedded else ''}")
    return [info.encode(), listing.encode()]


def slides_dom_checks(rep, spec, facts):
    ids = listed(facts, "slide")
    rep.add("slides", ids == [s["id"] for s in spec["slides"]], ids, "slide set mismatch")
    slides = by(facts, "slide")
    gone = [s["id"] for s in spec["slides"] if s["id"] in slides and s["title"] not in slides[s["id"]]["text"]]
    rep.add("text", not gone, gone, f"text missing: {gone[0] if gone else ''}")


# ---- video --------------------------------------------------------------------------------------------------------

def boxes(data, start, end):
    pos = start
    while pos + 8 <= end:
        size, kind, header = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4:pos + 8].decode("latin-1"), 8
        if size == 1:
            size, header = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def container(data):
    """(top-level box names, (width, height) of the avc1 sample entry or None) from a plain ISO-BMFF walk."""
    top = {kind: (s, e) for kind, s, e in boxes(data, 0, len(data))}
    found, stack = None, ([top["moov"]] if "moov" in top else [])
    while stack:
        start, end = stack.pop()
        for kind, s, e in boxes(data, start, end):
            if kind in ("trak", "mdia", "minf", "stbl"):
                stack.append((s, e))
            elif kind == "stsd":
                for k2, s2, e2 in boxes(data, s + 8, e):
                    if k2 == "avc1" and e2 - s2 >= 28:
                        found = (int.from_bytes(data[s2 + 24:s2 + 26], "big"), int.from_bytes(data[s2 + 26:s2 + 28], "big"))
    return set(top), found


def inspect_video(spec, art, ctx):
    rep = Report("video")
    if not tree_entries(art, rep):
        return rep.done()
    width, height = spec["canvas"]
    fps, seconds = spec["fps"], spec["seconds"]
    count = fps * seconds
    picks = sorted({0, count // 2, count - 1})
    mp4 = common.read_artifact(art, "video.mp4")
    if not mp4:
        rep.add("probe", False, None, "no output")
        return rep.done()
    stored = {k: common.read_artifact(art, f"frames/frame{k:04d}.png") for k in picks}
    if any(v is None for v in stored.values()):
        rep.add("frames", False, None, "missing artifact: frames")
        return rep.done()
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    for tool, exe in (("ffprobe", ffprobe), ("ffmpeg", ffmpeg)):
        if not exe:
            raise Exit(3, f"tool unavailable: {tool}")
    path = os.path.join(art, "video.mp4")
    code, out, _ = common.run_tool([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                    "stream=codec_name,width,height,pix_fmt,nb_read_frames", "-of", "json", path], "ffprobe", 60)
    try:
        stream = common.parse_json(out.decode("utf-8", "replace"), "ffprobe")["streams"][0]
    except (Exit, KeyError, IndexError, TypeError):
        stream = {}
    problems = []
    if stream.get("codec_name") != "h264":
        problems.append("codec mismatch")
    if (stream.get("width"), stream.get("height")) != (width, height):
        problems.append("size mismatch")
    if stream.get("pix_fmt") != "yuv420p":
        problems.append("pixel format mismatch")
    if str(stream.get("nb_read_frames")) != str(count):
        problems.append("frame count mismatch")
    if not stream:
        problems = ["probe failed"]
    rep.add("probe", not problems, {k: stream.get(k) for k in ("codec_name", "width", "height", "pix_fmt", "nb_read_frames")},
            problems[0] if problems else "")
    names, avc1 = container(mp4)
    missing = [b for b in ("ftyp", "moov", "mdat") if b not in names]
    rep.add("container", not missing and avc1 == (width, height), {"boxes": sorted(names), "avc1": avc1},
            f"container invalid: {missing[0]}" if missing else "container invalid: avc1 size")
    _, tags, _ = common.run_tool([ffprobe, "-v", "error", "-show_entries", "format_tags", "-of", "default=nw=1", path], "ffprobe", 60)
    host_check(rep, art, [tags], ctx.get("repo"))
    if rep.failures:
        return rep.done()
    decoded, wrong = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        for k in picks:
            target = os.path.join(tmp, f"d{k}.png")
            common.run_tool([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file", "-i", path,
                             "-vf", f"select=eq(n\\,{k})", "-frames:v", "1", "-fps_mode", "passthrough", "-y", target], "ffmpeg", 60)
            decoded[k] = common.read_artifact(tmp, f"d{k}.png")
            mad = common.png_mad(decoded[k], stored[k]) if decoded[k] else None
            # mean difference per brief, plus a cap on strongly differing pixels: text-sized differences barely move a mean
            strong = common.png_diff_pixels(decoded[k], stored[k], 64) if decoded[k] else None
            if mad is None or mad > 8 or strong is None or strong > 0.005 * width * height:
                wrong.append(k)
    rep.add("frames", not wrong, picks, f"frame mismatch: {wrong[0] if wrong else ''}")
    moved = common.png_diff_pixels(decoded[picks[0]], decoded[picks[-1]]) if decoded[picks[0]] and decoded[picks[-1]] else None
    rep.add("motion", moved is not None and moved >= 20, moved, "no motion")
    if rep.failures:
        return rep.done()
    with common.Session(art, (width, height)) as session:
        rep.product = session.product
        facts, found = session.facts(), session.fonts()
        fresh = {}
        for k in picks:
            session.seek(k * 1000 / fps)
            fresh[k] = session.shot()
        external = session.external()
    scenes = by(facts, "scene")
    gone = [f"scene-{i}" for i, s in enumerate(spec["scenes"]) if str(i) not in scenes or s["text"] not in scenes[str(i)]["text"]]
    rep.add("text", not gone, gone, f"text missing: {gone[0] if gone else ''}")
    fonts_check(rep, found, ctx["fonts"])
    external_check(rep, external)
    off = [k for k in picks if not common.png_close(fresh[k], stored[k])]
    rep.add("pixel", not off, picks, f"pixel mismatch: frame{off[0] if off else ''}")
    return rep.done()


def inspect_kind(kind, spec, art, ctx):
    """Inspect the artifact dir `art` of `kind` against `spec`. ctx: fonts (declared families), repo/base/head for a recap."""
    if kind == "video":
        return inspect_video(spec, art, ctx)
    return inspect_page(kind, spec, art, ctx)
