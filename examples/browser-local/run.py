#!/usr/bin/env python3
"""Local browser example: form-mode and agent-mode journeys against a loopback page. No network, no real browser.

The script plays the user for the form-mode confirmation (pass --ask to be prompted instead). A receipt is
evidence against mistakes, not against a same-user forger. Exits 0 only when both receipts verify.
"""
import argparse
import contextlib
import io
import json
import os
import stat
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "modules" / "browser"))
import browser  # noqa: E402

REVIEWER = HERE / "reviewer.py"


def make_server(site_dir, log_path):
    """HTTP server on 127.0.0.1, port 0. state['mutate'] set to dom|revision|login changes /form."""
    state = {"mutate": None}
    site = Path(site_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, status, body, headers=()):
            data = body.encode()
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/form"):
                if state["mutate"] == "login":
                    return self.reply(302, "", [("Location", "/login")])
                body = (site / "form.html").read_text()
                if state["mutate"] == "dom":
                    body = body.replace("</form>", '<input name="extra" type="text"></form>')
                if state["mutate"] == "revision":
                    body = body.replace('content="site-1"', 'content="site-2"')
                return self.reply(200, body)
            if path == "/login":
                return self.reply(200, (site / "login.html").read_text())
            self.reply(404, "not found")

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            values = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            with open(log_path, "a") as f:
                f.write(json.dumps({"path": self.path, "values": values}, sort_keys=True) + "\n")
            self.reply(200, "<html><body><p>Request received.</p></body></html>")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return httpd, state


def default_out():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "ai-architecture" / "browser-local"


def prepare_out(out):
    """The output directory, 0700, never a symlink or someone else's; its realpath."""
    out = Path(out).expanduser()
    if out.is_symlink() or os.path.abspath(out) != os.path.realpath(out):
        raise SystemExit("refused: --out is, or lies below, a symlink")
    if out.exists():
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SystemExit("refused: --out is not a directory owned by you")
        if any(out.iterdir()) and not (out / "latest.json").is_file():
            raise SystemExit("refused: --out is not empty and holds no earlier run (no latest.json)")
        if info.st_mode & 0o077:
            out.chmod(0o700)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(mode=0o700)
    return Path(os.path.realpath(out))


def cli(*argv):
    """Run one browser.py command in-process; only the reviewer is a separate process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = browser.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def journey(mode, url, run, facts, state, args):
    d = run
    step = {"form": "form", "agent": "agent"}[mode]
    p = {k: d / f"{step}-{k}.json" for k in ("survey", "proposal", "review", "approval", "receipt")}
    code, out, err = cli("survey", "--mode", mode, "--url", url, "--out", p["survey"])
    if code == 0:
        code, out, err = cli("propose", "--survey", p["survey"], "--facts", facts, "--form", "library-card",
                             "--out", p["proposal"])
    if code == 0:
        code, out, err = cli("review", "--survey", p["survey"], "--proposal", p["proposal"], "--facts", facts,
                             "--out", p["review"], "--reviewer-cmd", sys.executable, REVIEWER)
    if code == 0:
        argv = ["approve", "--mode", mode, "--review", p["review"], "--out", p["approval"]]
        if mode == "form":
            sha = json.loads(p["proposal"].read_text())["proposal_sha256"]
            if args.ask:
                print(f"proposal {sha[:12]} would submit: {Path(facts).read_text().strip()}")
                if input("approve as the user? [y/N] ").strip().lower() != "y":
                    return 4
            argv += ["--user-confirm", sha]
        code, out, err = cli(*argv)
    if code == 0:
        state["mutate"] = args.mutate_before_act  # the page changes between approval and action
        code, out, err = cli("act", "--approval", p["approval"], "--out", p["receipt"])
        state["mutate"] = None
    if code != 0:
        print(f"{mode} journey stopped (exit {code}): {err.strip()}")
        return code
    code, out, err = cli("verify-receipt", p["receipt"])
    print(f"{mode} receipt: {p['receipt'].relative_to(run.parent.parent)} -> {out.strip() or err.strip()}")
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="state directory (default: XDG state dir)")
    ap.add_argument("--yes", action="store_true", help="confirm as the user without asking (the default)")
    ap.add_argument("--ask", action="store_true", help="prompt for the form-mode confirmation")
    ap.add_argument("--mutate-before-act", choices=["dom", "revision", "login"], help="test hook")
    ap.add_argument("--port-file", help="write the server port here once it is listening")
    args = ap.parse_args(argv)
    out = prepare_out(args.out or default_out())
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run = out / "runs" / run_id
    run.mkdir(parents=True, mode=0o700)
    (out / "latest.json").write_text(json.dumps({"run": f"runs/{run_id}"}))
    log = run / "server-submissions.jsonl"
    httpd, state = make_server(HERE / "site", log)
    port = httpd.server_address[1]
    if args.port_file:
        Path(args.port_file).write_text(str(port))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    url = f"http://127.0.0.1:{port}/form"
    print(f"serving 127.0.0.1:{port}")
    try:
        # form mode first; a mutation applies to whichever journey runs, so the second is skipped on a stop
        rc = journey("form", url, run, HERE / "facts.json", state, args)
        if rc == 0:
            rc = journey("agent", url, run, HERE / "facts.json", state, args)
    finally:
        httpd.shutdown()
        httpd.server_close()
    n = len(log.read_text().splitlines()) if log.exists() else 0
    print(f"server saw {n} submission(s); run directory: {run.relative_to(out.parent)}")
    print("PASS: both receipts verify" if rc == 0 else f"STOPPED: exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
