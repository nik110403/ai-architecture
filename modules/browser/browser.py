#!/usr/bin/env python3
"""State-bound browser protocol: survey -> proposal -> independent review -> approval -> act -> receipt.

Exit codes: 0 ok; 1 verify-receipt found the receipt invalid; 2 refused before anything (missing backend, tool or
reviewer, forbidden URL, wrong mode); 3 state changed or takeover, stopped before the action; 4 approval or review
invalid. Receipts are evidence against mistakes, not against a same-user forger. Stdlib only.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends  # noqa: E402

HEX64 = re.compile(r"[0-9a-f]{64}")
APPROVAL_KEYS = {"schema", "mode", "kind", "url", "revision", "dom_sha256", "survey_sha256", "proposal_sha256",
                 "review_sha256", "approval_sha256"}
KIND = {"form": "user", "agent": "reviewer"}
REVIEW_TIMEOUT = 30


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# --- hashing and files -------------------------------------------------------------------------------------------

def sha(doc):
    return hashlib.sha256(backends.canon(doc)).hexdigest()


def set_hash(doc, field):
    doc.pop(field, None)
    doc[field] = sha(doc)
    return doc[field]


def check_hash(doc, field):
    if not isinstance(doc, dict) or not isinstance(doc.get(field), str):
        return False
    rest = {k: v for k, v in doc.items() if k != field}
    return doc[field] == sha(rest)


def load(path, bad=2):
    try:
        doc = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        raise Fail(2, f"missing file: {Path(path).name}")
    except (ValueError, OSError, RecursionError):
        raise Fail(bad, f"unreadable JSON: {Path(path).name}")
    if not isinstance(doc, dict):
        raise Fail(bad, f"not a JSON object: {Path(path).name}")
    return doc


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise Fail(2, "refusing a symlinked directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def state_used_root():
    """Fixed per-user single-use root; no flag moves it. Refuses symlinked or foreign-owned directories."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    path = Path(base)
    for part in ("ai-architecture", "browser", "used"):
        path = path / part
        if path.is_symlink():
            raise Fail(2, "refusing a symlinked state directory")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise Fail(2, "state directory is not owned by you")
    return path


def write_atomic(path, text):
    path = Path(path)
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def store(path, doc, digest):
    """Write doc to path and a content-addressed copy under <dir>/artifacts/ so later steps can load it by hash."""
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    write_atomic(path, text)
    write_atomic(Path(path).parent / "artifacts" / f"{digest}.json", text)


def find(digest, field, *dirs):
    """Load an artifact by hash from <dir>/artifacts; verify its hash. Fail(4) when absent or altered."""
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise Fail(4, "malformed hash reference")
    for d in dirs:
        f = Path(d) / "artifacts" / f"{digest}.json"
        if f.is_file():
            doc = load(f, 4)
            ok = check_hash(doc, field) if field else sha(doc) == digest
            if ok and (field is None or doc[field] == digest):
                return doc
            raise Fail(4, "artifact does not match its hash")
    raise Fail(4, "referenced artifact not found")


def stop(outdir, reason, approval=None, detail=""):
    d = private_dir(outdir)
    fd = os.open(d / "stops.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"reason": reason, "approval_sha256": approval, "detail": detail}, sort_keys=True) + "\n")


def sensitive(field):
    tokens = field.get("autocomplete", "").split()
    return field.get("type") in ("password", "email", "tel") or any(
        t in ("name", "email", "tel") or t.startswith(("cc-", "tel-")) for t in tokens)


def get_backend(name):
    return backends.BACKENDS[name]()


def form_of(survey, form_id):
    for f in survey.get("forms", []):
        if f["id"] == form_id:
            return f
    return None


# --- commands ----------------------------------------------------------------------------------------------------

def cmd_status(a):
    st = {n: cls().probe() for n, cls in backends.BACKENDS.items()}
    if a.json:
        print(json.dumps({"backends": st}, sort_keys=True))
    else:
        for n, s in st.items():
            print(f"{n}: {s['state']}" + (f" ({s['setup']})" if s["setup"] else ""))
    return 0


def cmd_survey(a):
    b = get_backend(a.backend)
    if b.probe()["state"] != "available":
        raise Fail(2, f"backend {a.backend} unavailable")
    snap = b.survey(a.url)
    doc = {"schema": "browser-survey/1", "mode": a.mode, "backend": b.name, **snap}
    digest = sha(doc)
    store(a.out, doc, digest)
    print(json.dumps({"survey_sha256": digest, "takeover": doc["takeover"], "dom_sha256": doc["dom_sha256"]}))
    return 0


def load_survey(path):
    doc = load(path)
    if doc.get("schema") != "browser-survey/1":
        raise Fail(2, "not a survey")
    return doc


def cmd_propose(a):
    sv = load_survey(a.survey)
    if sv["mode"] not in KIND:
        raise Fail(2, "propose needs a form or agent mode survey")
    if sv["takeover"]:
        stop(Path(a.out).parent, "takeover", None, "survey page needs the user")
        raise Fail(3, "takeover: the page needs the user (login or password field)")
    facts = load(a.facts)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in facts.items()):
        raise Fail(2, "facts must map names to strings")
    form = form_of(sv, a.form)
    if form is None:
        raise Fail(4, "form not in the survey")
    backends.same_origin_action(sv["url"], form["action"])
    names = {f["name"] for f in form["fields"]}
    missing = sorted(set(facts) - names)
    if missing:
        raise Fail(4, f"fields not in the surveyed form: {', '.join(missing)}")
    doc = {"schema": "browser-proposal/1", "mode": sv["mode"], "url": sv["url"], "form_id": a.form,
           "action": form["action"], "values": dict(facts), "survey_sha256": sha(sv)}
    digest = set_hash(doc, "proposal_sha256")
    store(a.out, doc, digest)
    print(json.dumps({"proposal_sha256": digest}))
    return 0


def run_reviewer(cmd, payload):
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except (OSError, ValueError) as e:
        raise Fail(2, f"reviewer cannot run: {type(e).__name__}")
    try:
        out, _ = proc.communicate(payload, timeout=REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise Fail(4, "reviewer timed out")
    if proc.returncode != 0:
        raise Fail(4, f"reviewer exited {proc.returncode}")
    try:
        res = json.loads(out)
    except ValueError:
        raise Fail(4, "reviewer output is not JSON")
    if not isinstance(res, dict) or res.get("verdict") not in ("APPROVE", "REJECT"):
        raise Fail(4, "reviewer gave no verdict")
    return proc.pid, res


def cmd_review(a):
    if not a.reviewer_cmd:
        raise Fail(2, "--reviewer-cmd needs a command")
    sv = load_survey(a.survey)
    prop = load(a.proposal, 4)
    if not check_hash(prop, "proposal_sha256"):
        raise Fail(4, "proposal hash mismatch")
    facts = load(a.facts)
    if prop.get("survey_sha256") != sha(sv):
        raise Fail(4, "proposal was not made from this survey")
    pid, res = run_reviewer(a.reviewer_cmd, json.dumps({"survey": sv, "proposal": prop, "facts": facts}))
    verdict = res["verdict"]
    reasons = [str(r)[:200] for r in (res.get("reasons") or [])][:10] if isinstance(res.get("reasons"), list) else []
    if res.get("dom_sha256_seen") != sv["dom_sha256"]:
        verdict = "REJECT"
        reasons.append("reviewer saw a different page than the survey")
    if prop["values"] != {k: facts.get(k) for k in prop["values"]}:
        verdict = "REJECT"
        reasons.append("proposal values are not the facts")
    doc = {"schema": "browser-review/1", "verdict": verdict, "reasons": reasons,
           "dom_sha256_seen": res.get("dom_sha256_seen") if isinstance(res.get("dom_sha256_seen"), str) else "",
           "reviewer_pid": pid, "reviewer_reported_pid": res.get("pid") if isinstance(res.get("pid"), int) else None,
           "survey_sha256": sha(sv), "proposal_sha256": prop["proposal_sha256"]}
    digest = set_hash(doc, "review_sha256")
    store(a.out, doc, digest)
    print(json.dumps({"verdict": verdict, "review_sha256": digest}))
    return 0


def cmd_approve(a):
    rpath = Path(a.review)
    rv = load(rpath)
    if rv.get("schema") != "browser-review/1" or not check_hash(rv, "review_sha256"):
        raise Fail(4, "review hash mismatch")
    if rv.get("verdict") != "APPROVE":  # ignoring this would let a REJECTed proposal through
        raise Fail(4, "review is not an APPROVE")
    sv = find(rv["survey_sha256"], None, rpath.parent)
    prop = find(rv["proposal_sha256"], "proposal_sha256", rpath.parent)
    if prop["survey_sha256"] != rv["survey_sha256"]:
        raise Fail(4, "review binds a proposal to another survey")
    if a.proposal and load(a.proposal, 4).get("proposal_sha256") != rv["proposal_sha256"]:
        raise Fail(4, "review is for another proposal")
    if sv["mode"] != a.mode or prop["mode"] != a.mode:
        raise Fail(2, "survey mode does not match --mode")
    if a.mode == "form":
        if not a.user_confirm:
            raise Fail(2, "form mode needs --user-confirm <proposal_sha256>")
        if a.user_confirm != rv["proposal_sha256"]:
            raise Fail(4, "user confirmation does not match the proposal")
    elif needs_user(sv, prop):
        raise Fail(4, "needs_user: the proposal touches a sensitive field")
    doc = {"schema": "browser-approval/1", "mode": a.mode, "kind": KIND[a.mode], "url": sv["url"],
           "revision": sv["revision"], "dom_sha256": sv["dom_sha256"], "survey_sha256": rv["survey_sha256"],
           "proposal_sha256": rv["proposal_sha256"], "review_sha256": rv["review_sha256"]}
    digest = set_hash(doc, "approval_sha256")
    store(a.out, doc, digest)
    print(json.dumps({"approval_sha256": digest}))
    return 0


def needs_user(sv, prop):
    form = form_of(sv, prop["form_id"]) or {"fields": []}
    return any(sensitive(f) for f in form["fields"] if f["name"] in prop["values"])


def cmd_act(a):
    outdir, apath = Path(a.out).parent, Path(a.approval)
    b = get_backend(a.backend)
    if b.probe()["state"] != "available":
        stop(outdir, "backend_unavailable", None, a.backend)
        raise Fail(2, f"backend {a.backend} unavailable")
    app = load(apath, 4)
    sha_seen = app.get("approval_sha256") if isinstance(app.get("approval_sha256"), str) else None
    if app.get("mode") not in KIND:
        raise Fail(2, "act needs a form or agent mode approval")

    def invalid(detail):
        stop(outdir, "approval_invalid", sha_seen, detail)
        raise Fail(4, f"approval_invalid: {detail}")

    if set(app) != APPROVAL_KEYS or not check_hash(app, "approval_sha256"):
        invalid("approval hash mismatch")
    if app["kind"] != KIND[app["mode"]]:
        invalid("kind does not match mode")
    digest = app["approval_sha256"]
    try:
        dirs = (apath.parent, outdir)
        rv = find(app["review_sha256"], "review_sha256", *dirs)
        sv = find(app["survey_sha256"], None, *dirs)
        prop = find(app["proposal_sha256"], "proposal_sha256", *dirs)
    except Fail as e:
        invalid(e.msg)
    if rv["verdict"] != "APPROVE" or rv["proposal_sha256"] != app["proposal_sha256"] \
            or rv["survey_sha256"] != app["survey_sha256"] or prop["survey_sha256"] != app["survey_sha256"]:
        invalid("review does not bind this proposal and survey")
    if sv["mode"] != app["mode"] or prop["mode"] != app["mode"] or sv["url"] != app["url"] \
            or sv["dom_sha256"] != app["dom_sha256"] or sv["revision"] != app["revision"]:
        invalid("approval does not match its survey")
    if app["mode"] == "agent" and needs_user(sv, prop):
        invalid("needs_user: sensitive field")
    legacy = (apath.parent / "used" / digest, outdir / "used" / digest)  # markers from earlier versions
    if any(m.exists() for m in legacy):
        stop(outdir, "approval_used", digest, "")
        raise Fail(4, "approval_used")
    backends.same_origin_action(app["url"], prop["action"])
    b.can_submit(app["url"])
    now = b.survey(app["url"])
    for reason, changed in (("takeover", now["takeover"]), ("revision_changed", now["revision"] != app["revision"]),
                            ("dom_changed", now["dom_sha256"] != app["dom_sha256"])):  # takeover first (F4)
        if changed:
            stop(outdir, "takeover" if reason == "takeover" else reason, digest, "")
            raise Fail(3, reason)
    used = state_used_root() / digest  # per user, keyed by the approval hash only
    try:  # claimed before submitting: two concurrent acts cannot both reach the form
        os.close(os.open(used, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        stop(outdir, "approval_used", digest, "")
        raise Fail(4, "approval_used")
    try:
        result = b.submit(app["url"], prop["form_id"], prop["values"], prop["action"],
                          {"revision": app["revision"], "dom_sha256": app["dom_sha256"]})
    except backends.Changed as e:
        used.unlink(missing_ok=True)  # nothing was posted, so the approval is not spent
        stop(outdir, e.reason, digest, "at submit time")
        raise Fail(3, e.reason)
    rec = {"schema": "browser-receipt/1", "mode": app["mode"], "backend": b.name, "approval_sha256": digest,
           "action": prop["action"], "pre_act": {"revision": now["revision"], "dom_sha256": now["dom_sha256"]},
           "submitted": prop["values"], "status": result["status"], "after_sha256": sha(result["after"]),
           "reviewer_pid": rv["reviewer_pid"], "act_pid": os.getpid()}
    set_hash(rec, "receipt_sha256")
    for d in (apath.parent, outdir):  # make the chain loadable by hash next to the receipt
        for h, fld in ((app["approval_sha256"], "approval_sha256"), (rv["review_sha256"], "review_sha256"),
                       (prop["proposal_sha256"], "proposal_sha256"), (app["survey_sha256"], None)):
            src = Path(d) / "artifacts" / f"{h}.json"
            dst = outdir / "artifacts" / f"{h}.json"
            if src.is_file() and not dst.exists():
                write_atomic(dst, src.read_text())
    write_atomic(a.out, json.dumps(rec, indent=2, sort_keys=True) + "\n")
    write_atomic(used, json.dumps({"receipt_sha256": rec["receipt_sha256"]}) + "\n")  # replaces our own marker
    print(json.dumps({"receipt_sha256": rec["receipt_sha256"], "status": rec["status"]}))
    return 0


def cmd_verify(a):
    rpath = Path(a.receipt)
    try:
        rec = load(rpath, 1)
        if rec.get("schema") != "browser-receipt/1" or not check_hash(rec, "receipt_sha256"):
            raise Fail(1, "receipt hash mismatch")
        d = rpath.parent
        app = find(rec["approval_sha256"], "approval_sha256", d)
        rv = find(app["review_sha256"], "review_sha256", d)
        sv = find(app["survey_sha256"], None, d)
        prop = find(app["proposal_sha256"], "proposal_sha256", d)
        checks = [rec["submitted"] == prop["values"], rec["action"] == prop["action"], rec["pre_act"]["dom_sha256"] == app["dom_sha256"] == sv["dom_sha256"],
                  rec["pre_act"]["revision"] == app["revision"] == sv["revision"],
                  rec["mode"] == app["mode"] == sv["mode"] == prop["mode"], app["kind"] == KIND.get(app["mode"]),
                  rv["verdict"] == "APPROVE", rv["proposal_sha256"] == prop["proposal_sha256"],
                  rv["survey_sha256"] == app["survey_sha256"], prop["survey_sha256"] == app["survey_sha256"],
                  rec["reviewer_pid"] == rv["reviewer_pid"], rec["act_pid"] != rec["reviewer_pid"]]
        if not all(checks):
            raise Fail(1, "receipt does not match its approval chain")
    except (Fail, KeyError, TypeError) as e:
        print(f"invalid: {getattr(e, 'msg', 'malformed receipt')}", file=sys.stderr)
        return 1
    print("receipt verified")
    return 0


# --- entry -------------------------------------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="browser.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("survey")
    s.add_argument("--mode", choices=["read", "form", "agent"], required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--backend", choices=sorted(backends.BACKENDS), default="fixture")
    s = sub.add_parser("propose")
    s.add_argument("--survey", required=True)
    s.add_argument("--facts", required=True)
    s.add_argument("--form", required=True)
    s.add_argument("--out", required=True)
    s = sub.add_parser("review")
    s.add_argument("--survey", required=True)
    s.add_argument("--proposal", required=True)
    s.add_argument("--facts", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--reviewer-cmd", nargs=argparse.REMAINDER, default=[])  # must be last
    s = sub.add_parser("approve")
    s.add_argument("--mode", choices=["form", "agent"], required=True)
    s.add_argument("--review", required=True)
    s.add_argument("--proposal")
    s.add_argument("--user-confirm")
    s.add_argument("--out", required=True)
    s = sub.add_parser("act")
    s.add_argument("--approval", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--backend", choices=sorted(backends.BACKENDS), default="fixture")
    s = sub.add_parser("verify-receipt")
    s.add_argument("receipt")
    return p


COMMANDS = {"status": cmd_status, "survey": cmd_survey, "propose": cmd_propose, "review": cmd_review,
            "approve": cmd_approve, "act": cmd_act, "verify-receipt": cmd_verify}


def main(argv=None):
    try:
        args = parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    try:
        return COMMANDS[args.cmd](args)
    except Fail as e:
        print(f"{'refused' if e.code == 2 else 'stopped'}: {e.msg}", file=sys.stderr)
        return e.code
    except (backends.Refused, backends.Unavailable) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
