"""Pure checks over a render inspection (stdlib only). No browser, no file system, no network.

Every check returns {check, result, evidence}; result is pass | fail | error | blocked and an `error` is never a
pass. Evidence rows carry {check, selector, viewport, measured, screenshot_sha, result}; ids are assigned later.
"""
import re
import zlib

CORE = ("overflow", "required-present", "interaction", "external-request")
INTERACTIVE_ROLES = ("button", "link", "textbox", "checkbox", "img", "image")
LARGE_PX, LARGE_BOLD_PX = 24.0, 18.66
MOTION_MAX_SECONDS = 0.01


# --- colors ---------------------------------------------------------------------------------------------------------

def parse_color(text):
    """(r, g, b, alpha) from a computed color string or a six or three digit hex literal."""
    text = str(text).strip().lower()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 1.0)
    if text.startswith("rgb"):
        nums = [float(x) for x in re.findall(r"[\d.]+", text)]
        return (nums[0], nums[1], nums[2], nums[3] if len(nums) > 3 else 1.0)
    if text == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    raise ValueError("unparsed color")


def luminance(rgb):
    def lin(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    alpha = fg[3]
    mixed = tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))
    hi, lo = sorted((luminance(mixed), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# --- png (stdlib zlib decode) ---------------------------------------------------------------------------------------

def png_header(data):
    """(width, height, bit_depth, color_type) or None when this is not a PNG we can read."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR" or len(data) < 33:
        return None
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), data[24], data[25])


def png_rows(data):
    """Yield unfiltered scanlines (bytes). Only 8 bit RGB / RGBA, non interlaced; anything else raises ValueError."""
    head = png_header(data)
    if head is None or head[2] != 8 or head[3] not in (2, 6) or data[28] != 0:
        raise ValueError("unsupported png")
    width, height, _, ctype = head
    bpp = 3 if ctype == 2 else 4
    idat, pos = [], 8
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        if kind == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + size])
        pos += 12 + size
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    if len(raw) != height * (stride + 1):
        raise ValueError("png size mismatch")
    prev = bytearray(stride)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind != 0:
            raise ValueError("bad png filter")
        yield bytes(line)
        prev = line


def png_problem(data, width, height):
    """None when `data` is a PNG of exactly width x height with at least two distinct pixels, else a short reason."""
    head = png_header(data)
    if head is None:
        return "not a png"
    if (head[0], head[1]) != (width, height):
        return f"png is {head[0]}x{head[1]}, expected {width}x{height}"
    try:
        first = None
        for line in png_rows(data):
            bpp = 3 if head[3] == 2 else 4
            if first is None:
                first = line[:bpp]
            if line != first * width:
                return None
    except (ValueError, zlib.error):
        return "png cannot be decoded"
    return "png is one uniform color"


def png_pixels(data):
    return b"".join(png_rows(data))


# --- evidence helpers -----------------------------------------------------------------------------------------------

def row(check, selector, viewport, measured, shot, result):
    return {"check": check, "selector": selector, "viewport": viewport, "measured": measured, "screenshot_sha": shot,
            "result": result}


def aggregate(check, rows, errors=None):
    """One check result from its evidence rows; a browser error recorded for the check always wins over a pass."""
    cause = None
    if errors and check in errors:
        rows = [row(check, "", "", {"error": errors[check]}, "", "error")] + list(rows)
        cause = "browser"
    results = [r["result"] for r in rows]
    if "fail" in results:
        result = "fail"
    elif "error" in results:
        result = "error"
        cause = cause or "indeterminate"
    elif "blocked" in results:
        result = "blocked"
    else:
        result = "pass"
    return {"check": check, "result": result, "cause": cause, "evidence": rows}


# --- individual checks ----------------------------------------------------------------------------------------------

def check_a11y_name(insp):
    out = []
    for vp in insp["viewports"]:
        bad = [n for n in vp["ax"] if n["role"] in INTERACTIVE_ROLES and not str(n["name"]).strip()]
        out += [row("a11y-name", n["sel"], vp["name"], {"role": n["role"], "name": n["name"]}, vp["shot_sha"], "fail")
                for n in bad]
        if not bad:
            out.append(row("a11y-name", "", vp["name"], {"checked": len(vp["ax"])}, vp["shot_sha"], "pass"))
    return out


def large_text(el):
    return el["fontSize"] >= LARGE_PX or (el["fontSize"] >= LARGE_BOLD_PX and el["fontWeight"] >= 700)


def check_contrast(insp):
    out = []
    for vp in insp["viewports"]:
        for el in vp["elements"]:
            if not el["text"]:
                continue
            if el["bgImg"] or el["bg"] is None:
                why = "background image" if el["bgImg"] else "no opaque ancestor background"
                out.append(row("contrast", el["sel"], vp["name"], {"indeterminate": why}, vp["shot_sha"], "error"))
                continue
            need = 3.0 if large_text(el) else 4.5
            got = ratio(parse_color(el["color"]), parse_color(el["bg"]))
            out.append(row("contrast", el["sel"], vp["name"],
                           {"ratio": round(got, 2), "required": need, "color": el["color"], "background": el["bg"]},
                           vp["shot_sha"], "pass" if got >= need else "fail"))
    return out


def check_overflow(insp):
    out = []
    for vp in insp["viewports"]:
        doc, found = vp["doc"], 0
        if doc["scrollWidth"] > doc["clientWidth"]:
            out.append(row("overflow", "html", vp["name"], {"scrollWidth": doc["scrollWidth"],
                                                           "clientWidth": doc["clientWidth"]}, vp["shot_sha"], "fail"))
            found += 1
        for el in vp["elements"]:
            if el["rect"][2] > doc["innerWidth"] + 1:
                out.append(row("overflow", el["sel"], vp["name"], {"right": el["rect"][2], "viewport": doc["innerWidth"]},
                               vp["shot_sha"], "fail"))
                found += 1
        if not found:
            out.append(row("overflow", "", vp["name"], {"scrollWidth": doc["scrollWidth"]}, vp["shot_sha"], "pass"))
    return out


def check_required(insp, record):
    out = []
    for vp in insp["viewports"]:
        for sel in record["required_elements"]:
            ok = vp["required"].get(sel, False)
            out.append(row("required-present", sel, vp["name"], {"present": ok}, vp["shot_sha"], "pass" if ok else "fail"))
    return out


def check_keyboard(insp, record):
    kb, out = insp["keyboard"], []
    shot = insp["viewports"][0]["shot_sha"]
    for target in sorted({i["target"] for i in record["interactions"]}):
        ok = kb["reached"].get(target, False)
        out.append(row("keyboard-reach", target, kb["viewport"], {"reached": ok}, shot, "pass" if ok else "fail"))
    if kb["trap"]:
        out.append(row("keyboard-reach", kb["trap"], kb["viewport"], {"trap": True}, shot, "fail"))
    return out


def check_focus(insp, record):
    out, shot = [], insp["viewports"][0]["shot_sha"]
    for target in sorted({i["target"] for i in record["interactions"]}):
        info = insp["focus"].get(target)
        if not info or not info["reached"]:
            out.append(row("focus-visible", target, insp["keyboard"]["viewport"], {"blocked_by": "keyboard-reach"}, shot,
                           "blocked"))
        else:
            out.append(row("focus-visible", target, insp["keyboard"]["viewport"],
                           {"changed_pixels": info["diff"], "has_focus": info["has_focus"]}, info["focused_sha"],
                           "pass" if info["diff"] > 0 else "fail"))
    return out


def check_interaction(insp, record):
    out = []
    for item in insp["interactions"]:
        if not item["reached"]:
            out.append(row("interaction", item["target"], insp["keyboard"]["viewport"],
                           {"id": item["id"], "blocked_by": "keyboard-reach"}, item["shot_sha"], "blocked"))
        else:
            out.append(row("interaction", item["target"], insp["keyboard"]["viewport"],
                           {"id": item["id"], "key": item["key"], "expect": item["expect"], "observed": item["observed"]},
                           item["shot_sha"], "pass" if item["ok"] else "fail"))
    return out


def check_external(insp):
    ext = insp["external"]
    if not ext:
        return [row("external-request", "", "", {"blocked": 0}, "", "pass")]
    return [row("external-request", "", "", {"blocked": e}, "", "fail") for e in ext]


def seconds(text):
    values = []
    for part in str(text).split(","):
        part = part.strip()
        if part.endswith("ms"):
            values.append(float(part[:-2]) / 1000.0)
        elif part.endswith("s"):
            values.append(float(part[:-1]))
    return values


def check_motion(insp, record):
    out = []
    for target in record.get("motion_targets", []):
        got = insp["motion"]["reduced"].get(target)
        if got is None:
            out.append(row("reduced-motion", target, "", {"found": False}, "", "fail"))
            continue
        longest = max(seconds(got["animation"]) + seconds(got["transition"]) + [0.0])
        out.append(row("reduced-motion", target, "", {"longest_seconds": longest, "emulated": insp["motion"]["emulated"]},
                       "", "pass" if longest <= MOTION_MAX_SECONDS else "fail"))
    return out


def norm_font(text):
    return str(text).split(",")[0].strip().strip("\"'").lower()


def check_tokens(insp, record):
    tokens = record["tokens"]
    colors = {parse_color(c)[:3] for c in tokens.get("colors", [])}
    fonts = {norm_font(f) for f in tokens.get("fonts", [])}
    vp, out = insp["viewports"][0], []
    for el in vp["elements"]:
        seen = [("color", el["color"], True), ("background-color", el["ownBg"], el["ownBg"] is not None)]
        seen += [("border-color", c, True) for c in el["borders"]]
        for prop, value, applies in seen:
            if applies and parse_color(value)[:3] not in colors:
                out.append(row("tokens", el["sel"], vp["name"], {"property": prop, "value": value}, vp["shot_sha"], "fail"))
        if norm_font(el["fontFamily"]) not in fonts:
            out.append(row("tokens", el["sel"], vp["name"], {"property": "font-family", "value": el["fontFamily"]},
                           vp["shot_sha"], "fail"))
    return out or [row("tokens", "", vp["name"], {"elements": len(vp["elements"])}, vp["shot_sha"], "pass")]


def run_checks(insp, record, adapters):
    """The ordered list of check results for the active adapters; evidence ids ev-001.. are assigned here."""
    errors = insp.get("errors", {})
    plan = [("overflow", lambda: check_overflow(insp)), ("required-present", lambda: check_required(insp, record)),
            ("interaction", lambda: check_interaction(insp, record)), ("external-request", lambda: check_external(insp))]
    if "accessibility" in adapters:
        plan += [("a11y-name", lambda: check_a11y_name(insp)), ("contrast", lambda: check_contrast(insp)),
                 ("keyboard-reach", lambda: check_keyboard(insp, record)),
                 ("focus-visible", lambda: check_focus(insp, record))]
    if "motion" in adapters:
        plan.append(("reduced-motion", lambda: check_motion(insp, record)))
    if "style" in adapters:
        plan.append(("tokens", lambda: check_tokens(insp, record)))
    results, number = [], 0
    for name, fn in plan:
        try:
            rows = [] if name in errors else fn()
        except (KeyError, TypeError, ValueError, IndexError):
            rows = [row(name, "", "", {"error": "inspection data missing"}, "", "error")]
        res = aggregate(name, rows, errors)
        for evidence in res["evidence"]:
            number += 1
            evidence["id"] = f"ev-{number:03d}"
        results.append(res)
    return results
