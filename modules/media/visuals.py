"""Generic HTML builders (inline CSS, inline SVG, one declared font) for slides and the four visuals, plus the video page.

Re-authored for this collection: no upstream template, palette or type scale is used. Every spec string goes through
`html.escape`; colours are fixed `#rrggbb` literals; nothing is fetched from outside the tree. `produce` renders the page in
headless Chrome and writes the artifact files; judging them is inspectors.py's job."""
import importlib.util
import sys
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
esc, fmt_num, IFACE = common.esc, common.fmt_num, common.IFACE
INK, MUTED, PANEL, LINE, ACCENT = "#1b1f24", "#5b6573", "#eef1f5", "#c9d1db", "#c2410c"


def page(title, font, css, body):
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f"<title>{esc(title)}</title><style>html,body{{margin:0;padding:0;background:#ffffff;color:{INK};"
            f'font-family:"{font}"}}{css}</style></head><body>{body}</body></html>\n')


def logo_tag(spec):
    return f'<img class="logo" alt="" src="{esc(spec["_logo"])}" width="48" height="48">' if spec.get("_logo") else ""


# ---- slides -------------------------------------------------------------------------------------------------------

def slides_page(spec, font):
    parts = []
    for slide in spec["slides"]:
        items = "".join(f"<li>{esc(b)}</li>" for b in slide["bullets"])
        parts.append(f'<section class="slide" data-slide="{slide["id"]}"><div class="deck">{esc(spec["title"])}</div>'
                     f"<h1>{esc(slide['title'])}</h1><ul>{items}</ul>{logo_tag(spec)}</section>")
    css = ("@page{size:1920px 1080px;margin:0}"
           ".slide{box-sizing:border-box;position:relative;width:1920px;height:1080px;padding:120px 160px;overflow:hidden;"
           "break-after:page;page-break-after:always;background:#ffffff}"
           ".slide:last-child{break-after:auto;page-break-after:auto}"
           f".deck{{font-size:28px;color:{MUTED};margin-bottom:40px}}h1{{font-size:96px;margin:0 0 56px}}"
           "ul{margin:0;padding-left:56px;font-size:48px;line-height:1.5}.logo{position:absolute;right:80px;top:60px}")
    return page(spec["title"], font, css, "".join(parts))


# ---- infographic --------------------------------------------------------------------------------------------------

def infographic_page(spec, font):
    width, height = spec["canvas"]
    stats = spec["stats"]
    pad, top, value_w = 80, 220, 140
    label_w = width * 28 // 100
    track = width - 2 * pad - label_w - value_w - 16
    row_h = min(110, (height - top - 80) // len(stats))
    bar_h, size = row_h * 5 // 10, max(14, min(32, row_h * 3 // 10))
    biggest = max(s["value"] for s in stats)
    rows = []
    for index, stat in enumerate(stats):
        w = round(stat["value"] / biggest * track, 2)
        rows.append(f'<div class="row" data-row="{stat["id"]}" style="top:{top + index * row_h}px">'
                    f'<span class="label">{esc(stat["label"])}</span><div class="track">'
                    f'<div class="bar" data-stat="{stat["id"]}" style="width:{w:.2f}px"></div></div>'
                    f'<span class="value">{esc(fmt_num(stat["value"]))}</span></div>')
    css = (f"body{{position:relative;width:{width}px;height:{height}px;overflow:hidden}}"
           f".title{{position:absolute;left:{pad}px;top:70px;margin:0;font-size:56px}}"
           f".logo{{position:absolute;right:{pad}px;top:60px}}"
           f".row{{position:absolute;left:0;width:{width}px;height:{row_h}px}}"
           f".label{{position:absolute;left:{pad}px;width:{label_w - 16}px;line-height:{row_h}px;font-size:{size}px;"
           "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
           f".track{{position:absolute;left:{pad + label_w}px;top:{(row_h - bar_h) // 2}px;width:{track}px;height:{bar_h}px;background:{PANEL}}}"
           f".bar{{height:100%;background:{spec['bar_color']}}}"
           f".value{{position:absolute;left:{pad + label_w + track + 16}px;line-height:{row_h}px;font-size:{size}px}}")
    return page(spec["title"], font, css, f'<h1 class="title">{esc(spec["title"])}</h1>{logo_tag(spec)}{"".join(rows)}')


# ---- visual plan --------------------------------------------------------------------------------------------------

def plan_page(spec, font):
    steps = spec["steps"]
    centre = {s["id"]: 110 + i * 84 + 32 for i, s in enumerate(steps)}
    nodes = "".join(f'<div class="node" data-step="{s["id"]}" style="top:{110 + i * 84}px"><b>{esc(s["id"])}</b> {esc(s["title"])}</div>'
                    for i, s in enumerate(steps))
    paths, count = [], 0
    for step in steps:
        for need in step["needs"]:
            bulge = 50 + (count % 6) * 16
            ya, yb = centre[need], centre[step["id"]]
            paths.append(f'<path data-edge="{need}->{step["id"]}" d="M 620,{ya} C {620 + bulge},{ya} {620 + bulge},{yb} 620,{yb}" '
                         f'fill="none" stroke="{MUTED}" stroke-width="2" marker-end="url(#arrow)"/>')
            count += 1
    svg = ('<svg class="edges" width="800" height="1200" viewBox="0 0 800 1200"><defs><marker id="arrow" markerWidth="8" markerHeight="8" '
           f'refX="8" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{MUTED}"/></marker></defs>{"".join(paths)}</svg>')
    files = "".join(f'<tr data-file="{esc(f["path"])}"><td>{esc(f["path"])}</td><td>{esc(f["step"])}</td></tr>' for f in spec["files"])
    css = ("body{position:relative;width:1600px;height:1200px;overflow:hidden}"
           ".title{position:absolute;left:60px;top:30px;margin:0;font-size:44px}.logo{position:absolute;right:60px;top:30px}"
           f".node{{position:absolute;left:60px;width:540px;height:64px;box-sizing:border-box;padding:0 16px;line-height:64px;font-size:22px;"
           f"background:{PANEL};border:2px solid {LINE};border-radius:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}"
           ".edges{position:absolute;left:0;top:0}"
           f".files{{position:absolute;left:900px;top:110px;width:640px;border-collapse:collapse;font-size:20px}}"
           f".files caption{{text-align:left;font-size:28px;font-weight:bold;padding-bottom:12px}}"
           f".files td{{height:44px;padding:0 10px;border-bottom:1px solid {LINE}}}")
    body = (f'<h1 class="title">Plan {esc(spec["plan_id"])}</h1>{logo_tag(spec)}{svg}{nodes}'
            f'<table class="files"><caption>File map</caption>{files}</table>')
    return page("Plan " + spec["plan_id"], font, css, body)


# ---- visual recap -------------------------------------------------------------------------------------------------

def recap_page(spec, data, font):
    commits = "".join(f'<li data-commit="{esc(sha)}">{esc(sha)} {esc(subject)}</li>' for sha, subject in data["commits"])
    files = "".join(f'<tr data-file="{esc(path)}" data-add="{esc(a)}" data-del="{esc(d)}"><td class="path">{esc(path)}</td>'
                    f'<td class="add">+{esc(a)}</td><td class="del">-{esc(d)}</td></tr>' for path, (a, d) in sorted(data["files"].items()))
    css = ("body{position:relative;width:1400px;height:1100px;overflow:hidden}"
           ".title{margin:0;position:absolute;left:60px;top:40px;font-size:48px}.logo{position:absolute;right:60px;top:40px}"
           "h2{position:absolute;left:60px;margin:0;font-size:28px}"
           f".commits{{position:absolute;left:60px;top:190px;margin:0;padding:0;list-style:none;width:1280px;font-size:22px}}"
           ".commits li{height:32px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
           ".files{position:absolute;left:60px;top:520px;width:1280px;border-collapse:collapse;font-size:22px}"
           f".files td{{height:36px;padding:0 10px;border-bottom:1px solid {LINE}}}"
           f".add{{color:#15803d;width:100px}}.del{{color:#b91c1c;width:100px}}")
    body = (f'<h1 class="title">{esc(spec["title"])}</h1>{logo_tag(spec)}<h2 style="top:140px">Commits</h2>'
            f'<ul class="commits">{commits}</ul><h2 style="top:470px">Files changed</h2><table class="files">{files}</table>')
    return page(spec["title"], font, css, body)


# ---- system atlas -------------------------------------------------------------------------------------------------

def atlas_geometry(spec):
    count = len(spec["nodes"])
    cols = 2 if count <= 4 else 3 if count <= 9 else 4
    return {n["id"]: (60 + (i % cols) * (1480 // cols), 120 + (i // cols) * 170) for i, n in enumerate(spec["nodes"])}


def atlas_page(spec, font):
    pos = atlas_geometry(spec)
    centre = {k: (x + 150, y + 42) for k, (x, y) in pos.items()}
    edges = []
    for edge in spec["edges"]:
        (x1, y1), (x2, y2) = centre[edge["from"]], centre[edge["to"]]
        edges.append(f'<path data-edge="{edge["from"]}->{edge["to"]}" d="M {x1},{y1} L {x2},{y2}" fill="none" stroke="{MUTED}" '
                     f'stroke-width="2" marker-end="url(#arrow)"/><text class="e" x="{(x1 + x2) // 2}" y="{(y1 + y2) // 2 - 8}">'
                     f"{esc(edge['label'])}</text>")
    loops = []
    for loop in spec["loops"]:
        points = " ".join(f"{centre[m][0] + 8},{centre[m][1] + 8}" for m in loop + loop[:1])
        loops.append(f'<polyline data-loop="{",".join(loop)}" points="{points}" fill="none" stroke="{ACCENT}" stroke-width="2" '
                     'stroke-dasharray="8 6"/>')
    nodes = "".join(f'<g data-node="{n["id"]}" data-kind="{esc(n["kind"])}"><rect x="{pos[n["id"]][0]}" y="{pos[n["id"]][1]}" width="300" '
                    f'height="84" rx="10" fill="{PANEL}" stroke="{LINE}" stroke-width="2"/><text class="n" x="{pos[n["id"]][0] + 16}" '
                    f'y="{pos[n["id"]][1] + 38}">{esc(n["label"])}</text><text class="k" x="{pos[n["id"]][0] + 16}" '
                    f'y="{pos[n["id"]][1] + 66}">{esc(n["kind"])}</text></g>' for n in spec["nodes"])
    css = ("body{width:1600px;height:1000px;overflow:hidden}svg{display:block}.n{font-size:24px;font-weight:bold}"
           f".k{{font-size:16px;fill:{MUTED}}}.e{{font-size:16px;fill:{MUTED};text-anchor:middle}}.t{{font-size:40px;font-weight:bold}}")
    svg = ('<svg width="1600" height="1000" viewBox="0 0 1600 1000"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="8" '
           f'refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{MUTED}"/></marker></defs>'
           f'<text class="t" x="60" y="70">System atlas</text>{"".join(edges)}{"".join(loops)}{nodes}</svg>')
    return page("System atlas", font, css, svg)


def atlas_twin(spec):
    lines = [f"node {n['id']} {n['kind']} {n['label']}" for n in spec["nodes"]]
    lines += [f"edge {e['from']}->{e['to']} {e['label']}" for e in spec["edges"]]
    lines += ["loop " + ",".join(loop) for loop in spec["loops"]]
    return "\n".join(sorted(lines)) + "\n"


# ---- video page ---------------------------------------------------------------------------------------------------

def video_page(spec, font):
    width, height = spec["canvas"]
    scenes = "".join(f'<div class="scene" data-scene="{i}" style="top:{int(height * 0.2 + i * height * 0.12)}px;'
                     f'animation-delay:{scene["at"]}s">{esc(scene["text"])}</div>' for i, scene in enumerate(spec["scenes"]))
    bar = f'<div class="progress" style="animation-duration:{spec["seconds"]}s"></div>' if spec["scenes"] else ""
    css = (f"body{{position:relative;width:{width}px;height:{height}px;overflow:hidden;background:#0b1020;color:#f5f7fa}}"
           f".mark{{position:absolute;left:0;top:0;width:{width}px;height:{height // 9}px;background:#1d2a4d}}"
           f".scene{{position:absolute;left:{width // 10}px;font-size:{height // 9}px;opacity:0;"
           "animation-name:appear;animation-duration:0.5s;animation-timing-function:ease-out;animation-fill-mode:both}"
           f".progress{{position:absolute;left:0;bottom:0;height:{max(4, height // 30)}px;width:0;background:#38bdf8;"
           "animation-name:grow;animation-timing-function:linear;animation-fill-mode:both}"
           "@keyframes appear{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}"
           "@keyframes grow{from{width:0}to{width:100%}}")
    return page("Video", font, css, f'<div class="mark"></div>{logo_tag(spec)}{scenes}{bar}')


# ---- producing the artifacts --------------------------------------------------------------------------------------

def write(directory, name, data):
    path = Path(directory, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def produce(kind, spec, kdir, font, recap=None):
    """Write the artifact files of one rendered kind into kdir. Returns {'product': <browser product>, 'files': [names]}."""
    if kind == "slides":
        html_, canvas, shots = slides_page(spec, spec["font"]), (1920, 1080), "slide-1.png"
    elif kind == "infographic":
        html_, canvas, shots = infographic_page(spec, font), spec["canvas"], "infographic.png"
    elif kind == "visual-plan":
        html_, canvas, shots = plan_page(spec, font), tuple(IFACE["canvas"][kind]), "visual-plan.png"
    elif kind == "visual-recap":
        html_, canvas, shots = recap_page(spec, recap, font), tuple(IFACE["canvas"][kind]), "visual-recap.png"
    elif kind == "system-atlas":
        html_, canvas, shots = atlas_page(spec, font), tuple(IFACE["canvas"][kind]), "system-atlas.png"
        write(kdir, "atlas.txt", atlas_twin(spec))
    else:
        raise common.Exit(2, "unknown kind")
    write(kdir, "index.html", html_)
    files = ["index.html", shots] + (["atlas.txt"] if kind == "system-atlas" else [])
    with common.Session(kdir, canvas) as session:
        write(kdir, shots, session.shot())
        if kind == "slides":
            write(kdir, "deck.pdf", session.pdf())
            files.append("deck.pdf")
        product = session.product
    return {"product": product, "files": files}
