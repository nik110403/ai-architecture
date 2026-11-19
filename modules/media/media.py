#!/usr/bin/env python3
"""Media: slides, visuals, video and illustration adapters with inspected artifacts and an honest receipts table.

Subcommands (each takes --format text|json, or --json):
  renderers [--providers P] [--allow-provider]   every inventoried renderer: observed, or blocked with a fixed reason class
  build --kind K --spec S [--out O] [--repo R --base B --head H]   build one artifact and inspect it
  inspect --kind K --spec S --artifact DIR [--repo R --base B --head H]   re-inspect an artifact from its bytes and the spec
  illustrate --provider N --providers P --prompt-file F [--out O] [--allow-provider] [--allow-network] [--allow-spend]
  receipts [--out O] [--enable K,...] [--specs DIR] [--providers P --allow-provider]   the 7-row obligation table + renderer rows
Exit codes: 0 ok, 1 inspection failed or tampered, 2 refused or invalid, 3 renderer or tool unavailable, 4 provider refused.
JSON stdout always carries `status` and `reason`. Every adapter is optional; a missing dependency blocks only its own rows.
Nothing here uses the network; Chrome and FFmpeg are whatever you installed yourself."""
import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import uuid
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


common, visuals, video, inspectors, illustration = (_sib(n) for n in ("common", "visuals", "video", "inspectors", "illustration"))
Exit, IFACE = common.Exit, common.IFACE
ORDER = [o["id"] for o in IFACE["obligations"]]
OB = {o["id"]: o for o in IFACE["obligations"]}
KINDS = ORDER[:6]
EXAMPLE_SPECS = common.ROOT / "examples" / "media" / "specs"
SETUP = {"chrome-html": common.chrome.SETUP, "chrome-pdf": common.chrome.SETUP,
         "pdf-inspect": "install poppler-utils (pdfinfo, pdftotext, pdffonts) from your package manager",
         "ffmpeg-encode": "install FFmpeg with libx264 from your package manager",
         "ffprobe-inspect": "install FFmpeg (ffprobe comes with it) from your package manager"}
TOOLS = {"slides": ("pdfinfo", "pdftotext", "pdffonts"), "video": ("ffmpeg", "ffprobe"), "visual-recap": ("git",)}


# ---- rows and the one rule for EXERCISED --------------------------------------------------------------------------

def reason_ok(reason, klass):
    for item in IFACE["blocked_reasons"]:
        pattern = re.escape(item["reason"])
        pattern = re.sub(r"<[a-z]+>", ".+", pattern.replace("\\<", "<").replace("\\>", ">"))
        if item["class"] == klass and isinstance(reason, str) and re.fullmatch(pattern, reason):
            return True
    return False


def under(out, rel):
    path = os.path.realpath(os.path.join(out, rel))
    return path if not os.path.isabs(rel) and path.startswith(os.path.realpath(out) + os.sep) else None


def exercised_problem(row, out):
    """None when `row` may claim EXERCISED, else why not: fixture generation, unsupported claim or sha mismatch.
    One function, used by `receipts` when it builds the table and by verify when it reads one back."""
    renderer, tools = row.get("renderer") or {}, row.get("tools") or []
    parts = [renderer, *tools]
    if any(p.get("backend") == "fixture" for p in parts):
        return "fixture generation"
    inspection = row.get("inspection") or {}
    checks = inspection.get("checks")
    if (not row.get("artifact") or not isinstance(checks, list) or not checks or any(c.get("result") != "pass" for c in checks)
            or not renderer.get("product") or not all(p.get("exe_sha256") and p.get("backend") == "real" for p in parts)
            or not all(p.get("version") or p.get("product") for p in parts)):
        return "unsupported claim"
    for art in [*row["artifact"], {"path": inspection.get("path"), "sha256": inspection.get("sha256")}]:
        path = under(out, art.get("path") or "")
        if not path or not os.path.isfile(path) or common.sha(Path(path).read_bytes()) != art.get("sha256"):
            return "sha mismatch"
    return None


def registry_claims():
    doc = common.parse_json(common.read_capped(common.ROOT / "capabilities" / "registry.json", 4 << 20), "registry")
    return {str(c["id"]): {"id": str(c["id"]), "name": c["name"], "support": c["support"]} for c in doc["capabilities"]}


def finish_row(row, out, claims):
    ob = OB[row["id"]]
    row["upstream"] = list(ob["upstream"])
    claim = claims.get(ob["registry"], {"id": ob["registry"], "name": "unknown", "support": "unknown"})
    row["registry_claim"] = claim
    row["conflict"] = (row["status"] == "EXERCISED") != (claim["support"] != "planned")
    return row


def blocked_row(ident, klass, reason, **extra):
    return {"id": ident, "status": "BLOCKED", "blocker_class": klass, "reason": reason, "renderer": None, "tools": [], "artifact": [],
            "inspection": None, **extra}


def part(name, obs, product=None):
    version = obs.get("version", "")
    return {"name": name, "product": product or f"{name} {version}".strip(), "version": version,
            "exe_sha256": obs.get("exe_sha256", ""), "backend": obs.get("backend", "real")}


def renderer_record(kind, product):
    """(primary renderer, other tools) of one kind: the encoder is the video renderer, Chrome renders everything else."""
    seen = common.observe_chrome()
    chrome = part("chrome", seen, product or "Chrome/" + seen.get("version", ""))
    tools = [part(name, common.observe(name)) for name in TOOLS.get(kind, ())]
    return (tools[0], [chrome, *tools[1:]]) if kind == "video" else (chrome, tools)


# ---- build / inspect ----------------------------------------------------------------------------------------------

def load_kind(kind, spec_path, adapters_dir):
    adapters = common.load_adapters(adapters_dir)
    adapter = adapters[OB[kind]["adapter"]]
    fonts = common.declared_fonts(adapter)
    return adapter, fonts, common.load_spec(kind, spec_path, fonts)


def recap_context(kind, args):
    if kind != "visual-recap":
        return {}
    repo = getattr(args, "repo", None)
    if not (repo and getattr(args, "base", None) and getattr(args, "head", None)):
        raise Exit(2, "invalid spec: recap needs --repo, --base and --head")
    return {"repo": os.path.abspath(repo), "base": args.base, "head": args.head}


def build_kind(kind, spec, adapter, fonts, out, ctx):
    """Build and inspect one artifact into out/<kind>. Returns the build result; a failed inspection is Exit(1)."""
    common.check_deps(adapter, kind)
    kdir = common.fresh_dir(os.path.join(out, kind))
    recap = None
    if kind == "visual-recap":
        recap = common.recap_data(ctx["repo"], ctx["base"], ctx["head"])
        ctx = dict(ctx, base=recap["base"], head=recap["head"])
    made = video.build(spec, kdir, fonts[0]) if kind == "video" else visuals.produce(kind, spec, kdir, fonts[0], recap)
    report = inspectors.inspect_kind(kind, spec, kdir, dict(ctx, fonts=fonts))
    renderer, tools = renderer_record(kind, report.get("product") or made.get("product"))
    inspection = {"version": 1, "kind": kind, "checks": report["checks"], "renderer": renderer, "tools": tools}
    if "ffmpeg_argv" in made:
        inspection["ffmpeg_argv"] = made["ffmpeg_argv"]
    Path(kdir, "inspection.json").write_text(json.dumps(inspection, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    entry = common.describe_file(out, f"{kind}/inspection.json")
    summary = {"path": entry["path"], "sha256": entry["sha256"], "checks": report["checks"]}
    if report["failures"]:
        failed = next(c["check"] for c in report["checks"] if c["result"] == "fail")
        raise Exit(1, report["reason"], kind=kind, check=failed, failures=report["failures"], inspection=summary)
    artifact = [common.describe_file(out, f"{kind}/{name}", volatile=name == "deck.pdf") for name in made["files"]]
    backend = "fixture" if any(p["backend"] == "fixture" for p in [renderer, *tools]) else "real"
    return {"status": "ok", "reason": "ok", "kind": kind, "artifact": artifact, "inspection": summary, "renderer": renderer,
            "tools": tools, "backend": backend, "recap": ({"base": recap["base"], "head": recap["head"]} if recap else None)}


def cmd_build(args):
    adapter, fonts, spec = load_kind(args.kind, args.spec, args.adapters)
    out = common.prepare_out(args.out)
    return 0, build_kind(args.kind, spec, adapter, fonts, out, recap_context(args.kind, args))


def cmd_inspect(args):
    adapter, fonts, spec = load_kind(args.kind, args.spec, args.adapters)
    art = os.path.realpath(args.artifact)
    if not os.path.isdir(args.artifact):
        raise Exit(2, "outside input")
    ctx = dict(recap_context(args.kind, args), fonts=fonts)
    if args.kind == "visual-recap":
        ctx["base"], ctx["head"] = common.resolve_rev(ctx["repo"], ctx["base"]), common.resolve_rev(ctx["repo"], ctx["head"])
    report = inspectors.inspect_kind(args.kind, spec, art, ctx)
    if report["failures"]:
        failed = next(c["check"] for c in report["checks"] if c["result"] == "fail")
        raise Exit(1, report["reason"], kind=args.kind, check=failed, failures=report["failures"], checks=report["checks"])
    return 0, {"status": "ok", "reason": "ok", "kind": args.kind, "checks": report["checks"]}


# ---- illustration -------------------------------------------------------------------------------------------------

def do_illustrate(entry, prompt, out, allow_provider, allow_network, allow_spend):
    """Every refusal comes before the generate argv is spawned."""
    if not allow_provider:
        raise Exit(2, "provider not allowed")
    prompt = illustration.read_prompt(prompt)
    desc = illustration.describe(entry)
    illustration.refuse_by_policy(desc, allow_network, allow_spend)
    idir = common.fresh_dir(os.path.join(out, "illustration"))
    data = illustration.generate(entry, desc, prompt, idir)
    backend = illustration.backend_of(entry, desc)
    checks = [{"check": "png", "result": "pass", "measured": list(common.checks.png_header(data)[:2])}]
    Path(idir, "inspection.json").write_text(json.dumps({"version": 1, "kind": "illustration", "checks": checks, "provider": entry["name"],
                                                         "describe": desc}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "ok", "reason": "ok", "provider": entry["name"], "backend": backend,
            "backend_reason": "fixture generation" if backend == "fixture" else None, "describe": desc,
            "artifact": [common.describe_file(out, "illustration/image.png")],
            "inspection": {**common.describe_file(out, "illustration/inspection.json"), "checks": checks}}


def cmd_illustrate(args):
    providers = illustration.load_providers(args.providers)
    if args.provider not in providers:
        raise Exit(2, "unknown provider")
    if not args.allow_provider:
        raise Exit(2, "provider not allowed")
    out = common.prepare_out(args.out)
    return 0, do_illustrate(providers[args.provider], args.prompt_file, out, args.allow_provider, args.allow_network, args.allow_spend)


# ---- renderers ----------------------------------------------------------------------------------------------------

def observed_row(ident, obs, tools=None):
    if obs["status"] != "available":
        return {"id": ident, "status": "unavailable", "reason": obs.get("reason", ""), "setup": SETUP[ident]}
    return {"id": ident, "status": "available", "version": obs["version"], "exe_sha256": obs["exe_sha256"], "backend": obs["backend"]}


def renderer_rows(providers_path=None, allow_provider=False):
    chrome_obs = common.observe_chrome()
    rows = [observed_row("chrome-html", chrome_obs), observed_row("chrome-pdf", chrome_obs)]
    pdf = [common.observe(t) for t in TOOLS["slides"]]
    first_bad = next((o for o in pdf if o["status"] != "available"), None)
    rows.append(observed_row("pdf-inspect", first_bad or pdf[1]))
    rows += [observed_row("ffmpeg-encode", common.observe("ffmpeg")), observed_row("ffprobe-inspect", common.observe("ffprobe"))]
    for ident, item in IFACE["blocked_renderers"].items():
        rows.append({"id": ident, "status": "blocked", "blocker_class": item["class"], "setup": item["setup"]})
    if providers_path:
        for name, entry in illustration.load_providers(providers_path).items():
            if not allow_provider:
                rows.append({"id": name, "status": "unavailable", "reason": "not observed (no --allow-provider)", "kind": "provider"})
                continue
            try:
                desc = illustration.describe(entry)
            except Exit as error:
                rows.append({"id": name, "status": "unavailable", "reason": error.reason, "kind": "provider"})
                continue
            blocked = desc["network"] or desc["cost"] != "none"
            rows.append({"id": name, "status": "blocked" if blocked else "available", "kind": "provider",
                         "backend": illustration.backend_of(entry, desc), "network": desc["network"], "cost": desc["cost"]})
    return rows


def cmd_renderers(args):
    return 0, {"status": "ok", "reason": "ok", "renderers": renderer_rows(args.providers, args.allow_provider),
               "illustration_licence": illustration.licence_record()}


# ---- receipts -----------------------------------------------------------------------------------------------------

DEMO_ENV = {"GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid", "GIT_COMMITTER_NAME": "demo",
            "GIT_COMMITTER_EMAIL": "demo@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def demo_repo(out):
    """A disposable repository of synthetic files (three commits, three files touched in the range)."""
    repo = os.path.join(out, "recap-repo")
    if os.path.lexists(repo):
        shutil.rmtree(repo)
    os.makedirs(repo)

    def commit(message):
        common.git_run(repo, "add", "-A")
        common.git_run(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, extra=DEMO_ENV)
    common.git_run(repo, "init", "-q", "-b", "main")
    Path(repo, "README.md").write_text("# Demo\nA synthetic project.\n")
    Path(repo, "app.py").write_text("print('one')\n")
    Path(repo, "notes.txt").write_text("keep\n")
    commit("start the demo")
    base = common.git_run(repo, "rev-parse", "HEAD")[1].decode().strip()
    Path(repo, "app.py").write_text("print('one')\nprint('two')\nprint('three')\n")
    Path(repo, "docs.md").write_text("Usage\n=====\nRun app.py.\n")
    commit("grow app and add docs")
    Path(repo, "README.md").write_text("# Demo\nA synthetic project with docs.\n")
    commit("mention the docs")
    return repo, base, common.git_run(repo, "rev-parse", "HEAD")[1].decode().strip()


def illustration_row(args, out, claims, enabled):
    row = blocked_row("illustration", "provider", "no provider without network or cost configured",
                      licence=illustration.licence_record(), interface=None)
    if args.providers and args.allow_provider:
        prompt = os.path.join(out, "illustration-prompt.txt")
        Path(prompt).write_text("A plain synthetic gradient, no subject.\n", encoding="utf-8")
        for name, entry in illustration.load_providers(args.providers).items():
            try:
                res = do_illustrate(entry, prompt, out, True, False, False)
                row["interface"] = {"provider": name, "backend": res["backend"], "backend_reason": res["backend_reason"],
                                    "artifact": res["artifact"], "inspection": res["inspection"]}
            except Exit as error:
                row["interface"] = {"provider": name, "status": "refused", "reason": error.reason}
            break
        if os.path.exists(prompt):
            os.remove(prompt)
    return row


def cmd_receipts(args):
    adapters = common.load_adapters(args.adapters)
    enabled = ORDER if not args.enable else [k.strip() for k in args.enable.split(",") if k.strip()]
    for kind in enabled:
        if kind not in OB:
            raise Exit(2, "unknown kind")
    out = common.prepare_out(args.out)
    specs_dir = Path(args.specs) if args.specs else EXAMPLE_SPECS
    claims = registry_claims()
    started, rows, failed, recap = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), [], False, None
    os.makedirs(os.path.join(out, "specs"), exist_ok=True)
    for ident in ORDER:
        if ident not in enabled:
            rows.append(finish_row(blocked_row(ident, "not-enabled", "not enabled"), out, claims))
            continue
        if ident == "illustration":
            rows.append(finish_row(illustration_row(args, out, claims, enabled), out, claims))
            continue
        spec_path = specs_dir / f"{ident}.json"
        adapter = adapters[OB[ident]["adapter"]]
        try:
            fonts = common.declared_fonts(adapter)
            spec = common.load_spec(ident, spec_path, fonts)
            ctx = {}
            if ident == "visual-recap":
                common.check_deps(adapter, ident)
                repo, base, head = demo_repo(out)
                ctx = {"repo": repo, "base": base, "head": head}
            result = build_kind(ident, spec, adapter, fonts, out, ctx)
            shutil.copyfile(spec_path, os.path.join(out, "specs", f"{ident}.json"))
            row = {"id": ident, "status": "EXERCISED", "blocker_class": None, "reason": None, "renderer": result["renderer"],
                   "tools": result["tools"], "artifact": result["artifact"], "inspection": result["inspection"],
                   "spec": common.describe_file(out, f"specs/{ident}.json")}
            problem = exercised_problem(row, out)
            if problem:
                row.update(status="BLOCKED", blocker_class="host", reason="fixture generation" if problem == "fixture generation"
                           else f"inspection failed: {problem}")
            if result["recap"]:
                recap = {"repo": "recap-repo", **result["recap"]}
        except Exit as error:
            if error.code == 3:
                row = blocked_row(ident, "host", error.reason)
            elif error.code == 1:
                failed = True
                row = blocked_row(ident, "host", f"inspection failed: {error.reason}")
            else:
                raise
        rows.append(finish_row(row, out, claims))
    doc = {"version": 1, "run_id": uuid.uuid4().hex[:12], "started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "rows": rows, "renderers": renderer_rows(args.providers, args.allow_provider), "recap": recap}
    doc["stable_sha256"] = stable_sha256(doc)
    Path(out, "receipts.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    blockers = [r["id"] for r in rows if r["status"] == "BLOCKED" and r["blocker_class"] in ("host", "not-enabled")]
    if failed:
        return 1, {"status": "failed", "reason": "inspection failed", **doc}
    if os.environ.get(common.REQUIRE_ENV) == "1" and blockers:
        return 3, {"status": "unavailable", "reason": f"required renderer blocked: {blockers[0]}", **doc}
    return 0, {"status": "ok", "reason": "ok", **doc}


def stable_sha256(doc):
    """Hash of the receipts without the closed set of per-run fields listed in interface.json (run id, times, pdf bytes)."""
    drop, volatile = set(IFACE["stable_exclude"]["keys"]) | {"stable_sha256"}, set(IFACE["stable_exclude"]["volatile_artifact_fields"])

    def strip(node):
        if isinstance(node, dict):
            skip = volatile if node.get("volatile") is True else set()
            return {k: strip(v) for k, v in node.items() if k not in drop and k not in skip}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return common.sha(json.dumps(strip(doc), sort_keys=True, separators=(",", ":")))


# ---- verify -------------------------------------------------------------------------------------------------------

def verify(out, only=None, specs_dir=None):
    """Read a receipts table back and trust none of it: structure first (no browser), then a fresh inspection of every
    EXERCISED row from the artifact bytes, the spec and the recap repo."""
    out = os.path.realpath(out)
    path = Path(out, "receipts.json")
    if not common.regular_file(path):
        raise Exit(2, "invalid receipts: missing receipts.json")
    doc = common.parse_json(common.read_capped(path, 16 << 20).decode("utf-8", "replace"), "receipts")
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
        raise Exit(2, "invalid receipts: rows")
    rows = {r.get("id"): r for r in doc["rows"] if isinstance(r, dict)}
    for ident in ORDER:
        if ident not in rows:
            raise Exit(1, f"missing row: {ident}", id=ident)
    for ident in ORDER:
        row = rows[ident]
        fail = None
        if row.get("status") == "EXERCISED":
            fail = exercised_problem(row, out)
        elif row.get("status") == "BLOCKED":
            if row.get("blocker_class") not in IFACE["blocker_classes"] or not reason_ok(row.get("reason"), row.get("blocker_class")):
                fail = "invalid reason"
            elif (row.get("interface") or {}).get("artifact"):
                for art in row["interface"]["artifact"]:
                    path_ = under(out, art.get("path") or "")
                    if not path_ or not os.path.isfile(path_) or common.sha(Path(path_).read_bytes()) != art.get("sha256"):
                        fail = "sha mismatch"
        else:
            fail = "invalid reason"
        if fail:
            raise Exit(1, f"{fail}: {ident}", id=ident)
    adapters = common.load_adapters(None)
    checked = []
    for ident in KINDS:
        row = rows[ident]
        if row["status"] != "EXERCISED" or (only and ident not in only):
            continue
        spec_file = Path(specs_dir or os.path.join(out, "specs")) / f"{ident}.json"
        adapter = adapters[OB[ident]["adapter"]]
        fonts = common.declared_fonts(adapter)
        spec = common.load_spec(ident, spec_file, fonts)
        ctx = {"fonts": fonts}
        if ident == "visual-recap":
            recap = doc.get("recap") or {}
            ctx.update(repo=os.path.join(out, recap.get("repo", "")), base=recap.get("base"), head=recap.get("head"))
        report = inspectors.inspect_kind(ident, spec, os.path.join(out, ident), ctx)
        if report["failures"]:
            raise Exit(1, report["reason"], id=ident, failures=report["failures"])
        checked.append(ident)
    summary = [{"id": i, "status": rows[i]["status"], "blocker_class": rows[i].get("blocker_class"), "reason": rows[i].get("reason")}
               for i in ORDER]
    return {"status": "ok", "reason": "ok", "rows": summary, "renderers": renderer_rows(), "reinspected": checked}


# ---- command line -------------------------------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Exit(2, "usage: " + message.splitlines()[0])


def build_parser():
    parser = Parser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", parser_class=Parser)

    def command(name, func, *flags):
        p = sub.add_parser(name)
        p.add_argument("--format", choices=("text", "json"), default="text")
        p.add_argument("--json", action="store_true")
        p.add_argument("--adapters", default=None)
        for flag, kw in flags:
            p.add_argument(flag, **kw)
        p.set_defaults(func=func)
        return p
    repo = [("--repo", {}), ("--base", {}), ("--head", {})]
    command("renderers", cmd_renderers, ("--providers", {}), ("--allow-provider", {"action": "store_true"}))
    command("build", cmd_build, ("--kind", {"required": True}), ("--spec", {"required": True}), ("--out", {}), *repo)
    command("inspect", cmd_inspect, ("--kind", {"required": True}), ("--spec", {"required": True}), ("--artifact", {"required": True}), *repo)
    command("illustrate", cmd_illustrate, ("--provider", {"required": True}), ("--providers", {"required": True}),
            ("--prompt-file", {"required": True}), ("--out", {}), ("--allow-provider", {"action": "store_true"}),
            ("--allow-network", {"action": "store_true"}), ("--allow-spend", {"action": "store_true"}))
    command("receipts", cmd_receipts, ("--out", {}), ("--enable", {}), ("--specs", {}), ("--providers", {}),
            ("--allow-provider", {"action": "store_true"}))
    return parser


def lines_of(doc):
    lines = []
    if "rows" in doc:
        for r in doc["rows"]:
            tail = f" {r['blocker_class']}: {r['reason']}" if r.get("status") == "BLOCKED" else ""
            lines.append(f"{r['id']} {r['status']}{tail}")
    for r in doc.get("renderers", []):
        lines.append(f"{r['id']} {r['status']}" + (f" {r.get('version', '')}".rstrip() if r["status"] == "available" else
                                                    f" {r.get('blocker_class') or r.get('reason', '')}"))
    if not lines:
        lines.append(f"{doc.get('status')}: {doc.get('reason')}")
    return lines


def emit(doc, as_json):
    if as_json:
        print(json.dumps(doc, sort_keys=True))
    else:
        print("\n".join(lines_of(doc)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv or (len(argv) > 1 and "--format" in argv and argv[argv.index("--format") + 1:argv.index("--format") + 2] == ["json"])
    try:
        args = build_parser().parse_args(argv)
        if not getattr(args, "func", None):
            raise Exit(2, "usage: a subcommand is required")
        as_json = args.json or args.format == "json"
        code, doc = args.func(args)
    except Exit as error:
        code, doc = error.code, {"status": common.STATUS[error.code], "reason": error.reason, **error.extra}
    emit(doc, as_json)
    return code


if __name__ == "__main__":
    sys.exit(main())
