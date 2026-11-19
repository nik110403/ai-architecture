#!/usr/bin/env python3
"""Lifecycle journey for modules/browser-operations against a loopback page (127.0.0.1 only, no real site).

journey(backend, out) serves site/lifecycle-form.html, runs survey -> propose -> review -> approve -> act through
ops.py with the chosen backend, verifies the receipt and returns (receipt_path, posts_seen).
"""
import contextlib
import io
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "modules" / "browser-operations"))
import ops  # noqa: E402

REVIEWER = HERE / "reviewer.py"
FACTS = HERE / "facts.json"


def serve():
    posts = []
    page = (HERE / "site" / "lifecycle-form.html").read_text().encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, body):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(page) if self.path.split("?")[0] in ("/", "/form") else self.send_error(404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            posts.append({k: v[0] for k, v in urllib.parse.parse_qs(raw).items()})
            self.reply(b"<html><body><p>Request received.</p></body></html>")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return httpd, posts


def step(out, *argv, **kw):
    o, e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        code = ops.main(["run", "--ops-out", str(out), *[str(a) for a in argv]], **kw)
    return code, o.getvalue(), e.getvalue()


def journey(backend, out):
    """Returns (receipt_path or None, posts, stderr_of_the_failing_step)."""
    out = Path(out)
    httpd, posts = serve()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/form"
    p = {k: out / f"{k}.json" for k in ("survey", "proposal", "review", "approval", "receipt")}
    try:
        steps = [("survey", "--mode", "form", "--url", url, "--out", p["survey"], "--backend", backend),
                 ("propose", "--survey", p["survey"], "--facts", FACTS, "--form", "library-card", "--out", p["proposal"]),
                 ("review", "--survey", p["survey"], "--proposal", p["proposal"], "--facts", FACTS, "--out", p["review"],
                  "--reviewer-cmd", sys.executable, REVIEWER)]
        for argv in steps:
            code, _, err = step(out, *argv)
            if code:
                return None, posts, err.strip()
        sha = json.loads(p["proposal"].read_text())["proposal_sha256"]
        for argv in (("approve", "--mode", "form", "--review", p["review"], "--user-confirm", sha, "--out", p["approval"]),
                     ("act", "--approval", p["approval"], "--out", p["receipt"], "--backend", backend)):
            code, _, err = step(out, *argv)
            if code:
                return None, posts, err.strip()
        return p["receipt"], posts, ""
    finally:
        httpd.shutdown()
        httpd.server_close()
