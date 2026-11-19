#!/usr/bin/env python3
"""Drive the public provenote companion for research discovery and citation checks.

Subcommands: handshake, search, cite. The adapter never touches the network on its own:
`search` and `cite` need either --fixture FILE (offline, canned responses) or --allow-network
(the companion's own default fetch). It makes no model call and copies nothing from the
companion; it imports `bin/lit-search` only after both checks pass and runs `bin/check-bib`
as a subprocess. Fetched metadata is untrusted data.
Exit codes: 0 ok, 1 companion reported failure / citations not verified, 2 usage or refused
input, 3 companion unavailable or contract mismatch.
"""

import argparse
import contextlib
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

NAME = "provenote"
TESTED = re.compile(r"0\.2\.[0-9]+")
SUMMARY = re.compile(r"[0-9]+ entries, [0-9]+ errors, [0-9]+ notes")
NO_COMPANION = "unavailable: no companion given (--companion DIR)"
RECEIPT_SUFFIX = ".receipt.json"


class Exit(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code, self.message = code, message


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)


def load_input(path, what):
    try:
        return load_json(path)
    except (OSError, ValueError) as error:
        raise Exit(2, f"invalid {what}: {error}")


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def regular(path):
    return path.is_file() and not path.is_symlink()


def handshake(companion):
    """Return companion identity and per-capability status; never imports the companion."""
    if companion is None:
        return {"companion": None, "integrated": False,
                "capabilities": {"34": NO_COMPANION, "35": NO_COMPANION}}

    def down(reason, info=None):
        text = "unavailable: " + reason
        return {"companion": info, "integrated": False, "capabilities": {"34": text, "35": text}}

    root = Path(companion)
    if root.is_symlink():
        raise Exit(2, "companion directory is a symlink")
    if not root.is_dir():
        return down("companion directory not found")
    meta_dir = root / ".claude-plugin"
    plugin = meta_dir / "plugin.json"
    if meta_dir.is_symlink() or plugin.is_symlink():
        raise Exit(2, "companion plugin.json is behind a symlink")
    if not plugin.is_file():
        return down("plugin.json missing")
    try:
        meta = load_json(plugin)
    except (OSError, ValueError) as error:
        return down(f"plugin.json invalid ({str(error)[:80]})")
    if not isinstance(meta, dict) or meta.get("name") != NAME:
        return down(f"plugin name is not {NAME}")
    version = meta.get("version")
    shown = version if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", version) else "?"
    info = {"name": NAME, "version": shown}
    if shown == "?" or not TESTED.fullmatch(shown):
        text = f"unsupported: version {shown} (tested 0.2.x)"
        return {"companion": info, "integrated": False, "capabilities": {"34": text, "35": text}}
    bin_dir = root / "bin"
    have_search = not bin_dir.is_symlink() and regular(bin_dir / "lit-search")
    have_check = not bin_dir.is_symlink() and regular(bin_dir / "check-bib")
    missing = [f"bin/{n}" for n, ok in (("lit-search", have_search), ("check-bib", have_check)) if not ok]
    cap34 = "available" if have_search else "unavailable: requires bin/lit-search (a regular file)"
    cap35 = "available" if not missing else "unavailable: requires " + " and ".join(missing)
    return {"companion": info, "integrated": not missing,
            "capabilities": {"34": cap34, "35": cap35}}


def require(companion, capability):
    status = handshake(companion)
    state = status["capabilities"][capability]
    if state != "available":
        raise Exit(3, f"capability {capability}: {state}")


def load_main(companion):
    """Load the companion's lit-search `main` by path; refuse anything off-contract."""
    path = Path(companion) / "bin" / "lit-search"
    if path.is_symlink() or not path.is_file():
        raise Exit(3, "companion contract mismatch: bin/lit-search is not a regular file")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader = SourceFileLoader("provenote_lit_search", str(path))
        module = module_from_spec(spec_from_loader(loader.name, loader))
        loader.exec_module(module)
    except (Exception, SystemExit):
        raise Exit(3, "companion contract mismatch: bin/lit-search cannot be loaded")
    finally:
        sys.dont_write_bytecode = previous
    entry = getattr(module, "main", None)
    if not callable(entry):
        raise Exit(3, "companion contract mismatch: bin/lit-search has no main")
    return entry


def run_main(entry, argv, fetch):
    """Call the companion main with a captured stdout; return (exit code, captured text)."""
    buffer = io.StringIO()
    kwargs = {"fetch": fetch} if fetch is not None else {}
    try:
        with contextlib.redirect_stdout(buffer):
            code = entry(argv, **kwargs)
    except SystemExit as error:
        code = error.code
    except Exception:
        raise Exit(3, "companion contract mismatch: main raised")
    if not is_int(code):
        raise Exit(3, "companion contract mismatch: main returned no exit code")
    return code, buffer.getvalue()


def load_fixture(path):
    data = load_input(path, "fixture")
    if not isinstance(data, dict) or set(data) != {"routes"} or not isinstance(data["routes"], list):
        raise Exit(2, "invalid fixture: expected {\"routes\": [...]}")
    base = Path(path).resolve().parent
    routes = []
    for route in data["routes"]:
        if not isinstance(route, dict) or not isinstance(route.get("match"), str) or not route["match"]:
            raise Exit(2, "invalid fixture route")
        kind = set(route) - {"match"}
        if kind == {"body"}:
            body = route["body"] if isinstance(route["body"], str) else json.dumps(route["body"])
        elif kind == {"body_file"} and isinstance(route["body_file"], str):
            target = (base / route["body_file"]).resolve()
            try:
                target.relative_to(base)
                body = target.read_text(encoding="utf-8")
            except (ValueError, OSError):
                raise Exit(2, "fixture body_file escapes the fixture directory or is unreadable")
        else:
            raise Exit(2, "invalid fixture route: need exactly one of body, body_file")
        routes.append((route["match"], body))

    def fetch(url, accept="application/json", **_ignored):
        for match, body in routes:
            if match in url:
                return body
        raise RuntimeError("no fixture route")

    return fetch


def choose_fetch(args):
    """Network gate: exactly one of --fixture / --allow-network; returns the fetch or None."""
    if bool(args.fixture) == bool(args.allow_network):
        raise Exit(2, "pass exactly one of --fixture FILE or --allow-network")
    return load_fixture(args.fixture) if args.fixture else None


def load_brief(path):
    data = load_input(path, "brief")
    if not isinstance(data, dict) or not {"question"} <= set(data) <= {"question", "since", "n", "snowball"}:
        raise Exit(2, "invalid brief: keys are question, since, n, snowball")
    question, since = data["question"], data.get("since")
    n, snowball = data.get("n", 25), data.get("snowball", False)
    if not isinstance(question, str) or not question.strip() or question.startswith("-"):
        raise Exit(2, "invalid brief question")
    if since is not None and not is_int(since):
        raise Exit(2, "invalid brief since")
    if not is_int(n) or n < 1:
        raise Exit(2, "invalid brief n")
    if not (isinstance(snowball, bool) or (is_int(snowball) and snowball >= 1)):
        raise Exit(2, "invalid brief snowball")
    return {"question": question.strip(), "since": since, "n": n, "snowball": snowball}


def slug(text, cap=60):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:cap].strip("-")


def one_line(text):
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", text or "").strip()


def provider_text(provider_status):
    return ", ".join(f"{k}: {v}" for k, v in provider_status.items())


def cmd_search(args):
    fetch = choose_fetch(args)
    brief = load_brief(args.brief)
    require(args.companion, "34")
    out = Path(args.out)
    work = out / "companion"
    if work.exists() and (not work.is_dir() or any(work.iterdir())):
        raise Exit(2, "companion output directory is not empty")
    entry = load_main(args.companion)
    argv = [brief["question"]]
    if brief["since"] is not None:
        argv += ["--since", str(brief["since"])]
    argv += ["--n", str(brief["n"])]
    if brief["snowball"] is True:
        argv += ["--snowball"]
    elif brief["snowball"] is not False:
        argv += ["--snowball", str(brief["snowball"])]
    if args.mailto:
        argv += ["--mailto", args.mailto]
    argv += ["--out", str(work)]
    code, _ = run_main(entry, argv, fetch)
    if code == 1:
        print("research search: failed - no provider reachable; no notes written")
        return 1
    if code != 0:
        raise Exit(2 if code == 2 else 3, f"companion exit {code}")
    receipts = sorted(work.glob("*" + RECEIPT_SUFFIX))
    if len(receipts) != 1:
        raise Exit(3, "companion contract mismatch: expected exactly one receipt")
    receipt_path = receipts[0]
    receipt = load_input(receipt_path, "companion receipt")
    table = receipt_path.with_name(receipt_path.name[: -len(RECEIPT_SUFFIX)] + ".csv")
    if (not isinstance(receipt, dict) or not isinstance(receipt.get("status"), str)
            or not isinstance(receipt.get("provider_status"), dict) or not table.is_file()):
        raise Exit(3, "companion contract mismatch: receipt or table malformed")
    with open(table, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    notes_dir = out / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    listing = []
    for number, row in enumerate(rows, 1):
        doi, title = one_line(row.get("doi")), one_line(row.get("title"))
        name = f"{number}-{slug(doi) or slug(title) or 'untitled'}.md"
        flag = one_line(row.get("status")) or "none"
        text = "\n".join([
            f"# {title or '(untitled)'}", "",
            f"- doi: {doi or 'none'}",
            f"- year: {one_line(row.get('year')) or 'unknown'}",
            f"- index: {one_line(row.get('indexes'))}",
            f"- status: {flag}",
            "- trust: untrusted fetched metadata", ""])
        (notes_dir / name).write_text(text, encoding="utf-8")
        listing.append({"file": name, "doi": doi, "indexes": one_line(row.get("indexes")), "status": flag})
    index = {
        "companion": handshake(args.companion)["companion"],
        "companion_receipt": {"path": receipt_path.relative_to(out).as_posix(),
                              "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()},
        "status": receipt["status"],
        "provider_status": receipt["provider_status"],
        "notes": listing,
    }
    (notes_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"research search: status {index['status']}  providers: "
          f"{provider_text(index['provider_status'])}  notes: {len(listing)}")
    return 0


def norm_doi(value):
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", (value or "").strip().lower())


def parse_bib(text):
    """Return [(citekey, normalised doi or '')] in file order."""
    entries, pos = [], 0
    opener = re.compile(r"@(\w+)\s*[{(]")
    while True:
        match = opener.search(text, pos)
        if not match:
            return entries
        end, depth = match.end(), 1
        while end < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        pos = end
        if match.group(1).lower() in {"comment", "string", "preamble"}:
            continue
        body = text[match.end():end - 1]
        doi = re.search(r"\bdoi\s*=\s*[{\"]\s*([^}\"]+?)\s*[}\"]", body, re.I)
        entries.append((body.partition(",")[0].strip(), norm_doi(doi.group(1)) if doi else ""))


def read_bib(path):
    return parse_bib(path.read_text(encoding="utf-8")) if path.is_file() else []


def run_check_bib(companion, bib, paper):
    script = Path(companion) / "bin" / "check-bib"
    try:
        done = subprocess.run([sys.executable, "-B", str(script), str(bib), str(paper)],
                              capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {"exit": -1, "summary_line": ""}
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    return {"exit": done.returncode, "summary_line": last if SUMMARY.fullmatch(last) else ""}


def cmd_cite(args):
    fetch = choose_fetch(args)
    requested = []
    for raw in args.dois:
        doi = norm_doi(raw)
        if not doi or doi.startswith("-") or re.search(r"\s", doi):
            raise Exit(2, "invalid DOI argument")
        if doi not in requested:
            requested.append(doi)
    require(args.companion, "35")
    bib, paper, out = Path(args.bib), Path(args.paper), Path(args.out)
    entry = load_main(args.companion)
    before = read_bib(bib)
    code, text = run_main(entry, ["--export-bib", *requested, "--bib", str(bib)], fetch)
    if code not in (0, 1):
        raise Exit(3, f"companion exit {code}")
    after = read_bib(bib)
    fresh = iter(after[len(before):])
    results = []
    for doi in requested:
        known = next((key for key, found in before if found == doi), None)
        if f"NOT RESOLVED {doi}:" in text:
            results.append({"doi": doi, "citekey": None, "status": "NOT RESOLVED"})
        elif known is not None:
            results.append({"doi": doi, "citekey": known, "status": "resolved"})
        else:
            added = next(fresh, None)
            if added is None:
                results.append({"doi": doi, "citekey": None, "status": "NOT RESOLVED"})
            else:
                same = added[1] == doi
                results.append({"doi": doi, "citekey": added[0],
                                "status": "resolved" if same else "identity-mismatch"})
    check = run_check_bib(args.companion, bib, paper)
    verified = (all(r["status"] == "resolved" for r in results) and check["exit"] == 0
                and check["summary_line"] != "")
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(
        json.dumps({"entries": results, "check_bib": check, "verified": verified}, indent=2) + "\n",
        encoding="utf-8")
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("resolved", "NOT RESOLVED", "identity-mismatch")}
    print(f"research cite: verified {str(verified).lower()}  " +
          ", ".join(f"{v} {k}" for k, v in counts.items()) + f"  check-bib exit {check['exit']}")
    return 0 if verified else 1


def cmd_handshake(args):
    status = handshake(args.companion)
    if args.format == "json":
        print(json.dumps(status, indent=2))
        return 0
    info = status["companion"]
    print("companion: " + (f"{info['name']} {info['version']}" if info else "none"))
    print(f"integrated: {str(status['integrated']).lower()}")
    for capability, text in status["capabilities"].items():
        print(f"capability {capability}: {text}")
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Exit(2, message)


def parser():
    top = Parser(description=__doc__.split("\n\n")[0])
    sub = top.add_subparsers(dest="command", required=True, parser_class=Parser)
    hs = sub.add_parser("handshake", help="report companion identity and capability status")
    hs.add_argument("--companion", metavar="DIR")
    hs.add_argument("--format", choices=("text", "json"), default="text")
    for name in ("search", "cite"):
        p = sub.add_parser(name)
        p.add_argument("--companion", required=True, metavar="DIR")
        p.add_argument("--out", required=True)
        p.add_argument("--fixture", metavar="FILE", help="offline canned responses")
        p.add_argument("--allow-network", action="store_true", help="use the companion's own fetch")
        if name == "search":
            p.add_argument("--brief", required=True, metavar="FILE")
            p.add_argument("--mailto", default="", help="forwarded to the companion only when given")
        else:
            p.add_argument("--dois", nargs="+", required=True, metavar="DOI")
            p.add_argument("--bib", required=True)
            p.add_argument("--paper", required=True)
    return top


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        return {"handshake": cmd_handshake, "search": cmd_search, "cite": cmd_cite}[args.command](args)
    except Exit as stop:
        if stop.message:
            print(f"research-adapter: {stop.message}", file=sys.stderr)
        return stop.code


class _Adapter:
    """Generic describe/handle adapter (see modules/contracts); handle reports the no-companion handshake."""

    def describe(self):
        return {"module": "research-adapter", "companion": NAME,
                "operations": ["handshake", "search", "cite"]}

    def handle(self):
        return handshake(None)


ADAPTER = _Adapter()

if __name__ == "__main__":
    sys.exit(main())
