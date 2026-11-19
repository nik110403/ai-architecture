"""Test helper: render a built artifact dir in the design pipeline's Chrome and read the MAIN world (page scripts run there;
the renderer's own evaluator is an isolated world and cannot see a page global)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main_world(tree):
    """(document text content, typeof window.__pwned) of tree/index.html after its scripts ran."""
    render = _load("aiarch_media_probe_render", ROOT / "modules" / "design-pipeline" / "render.py")
    r = render.Renderer(str(tree))
    try:
        r.start()
        r.load(render.ORIGIN, "probe", (1280, 720), first=True)
        r.settle(0.3)
        got = r.br.call("Runtime.evaluate", {"expression": "JSON.stringify([document.documentElement.textContent,"
                                                           "typeof window.__pwned])", "returnByValue": True})
        import json
        text, kind = json.loads(got["result"]["value"])
        return text, kind
    finally:
        r.close()
