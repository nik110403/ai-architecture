"""research-adapter acceptance tests (A1-A11 plus verifier amendments F1-F12).

The adapter is exercised against a contract stub of the companion built in a temp dir.
A single test (A8) runs the same flows against a real companion when the environment
variable RESEARCH_COMPANION_DIR names one; otherwise it skips.
"""

import contextlib
import getpass
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "modules" / "research-adapter" / "research_adapter.py"
RUN = ROOT / "examples" / "research-companion" / "run.py"
NEW_DIRS = (ROOT / "modules" / "research-adapter", ROOT / "examples" / "research-companion")

STUB_LIT_SEARCH = r'''
import csv, json, os, re, sys, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
(HERE / "imported.sentinel").write_text("imported\n")
LOG = HERE / "calls.log"


def http_get(url, accept="application/json", **kw):
    raise RuntimeError("stub has no network")


def log(entry):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def norm_doi(d):
    d = (d or "").strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)


def parse_oa(data):
    out = []
    for w in data.get("results", []):
        out.append({"title": w.get("display_name") or "", "doi": norm_doi(w.get("doi")),
                    "year": w.get("publication_year"), "retracted": bool(w.get("is_retracted")),
                    "oa_id": (w.get("id") or "").rsplit("/", 1)[-1],
                    "refs": [r.rsplit("/", 1)[-1] for r in w.get("referenced_works", [])],
                    "indexes": ["openalex"]})
    return out


def parse_s2(data):
    return [{"title": p.get("title") or "", "doi": norm_doi((p.get("externalIds") or {}).get("DOI")),
             "year": p.get("year"), "retracted": False, "oa_id": "", "refs": [],
             "indexes": ["semanticscholar"]} for p in data.get("data") or []]


def merge(items):
    out, by = [], {}
    for it in items:
        hit = by.get(it["doi"]) if it["doi"] else None
        if hit is None:
            out.append(it)
            if it["doi"]:
                by[it["doi"]] = it
        else:
            for i in it["indexes"]:
                if i not in hit["indexes"]:
                    hit["indexes"].append(i)
    return out


def search(a, fetch):
    status, items = {}, []
    q = urllib.parse.urlencode
    oa = {"search": a.query, "per-page": a.n}
    s2 = {"query": a.query, "limit": a.n}
    if a.since:
        oa["filter"] = "from_publication_date:%d-01-01" % a.since
        s2["year"] = "%d-" % a.since
    for name, base, params, parse in (
            ("openalex", "https://api.openalex.org/works", oa, parse_oa),
            ("semanticscholar", "https://api.semanticscholar.org/graph/v1/paper/search", s2, parse_s2)):
        try:
            items += parse(json.loads(fetch(base + "?" + q(params))))
            status[name] = "READY"
        except (RuntimeError, ValueError, KeyError, TypeError):
            status[name] = "UNAVAILABLE"
    ready = [k for k, v in status.items() if v == "READY"]
    st = "ok" if len(ready) == 2 else ("partial" if ready else "failed")
    items = merge(items)
    if a.snowball and ready:
        extra = []
        for s in items[:a.snowball]:
            if not s["oa_id"]:
                continue
            flts = []
            if s["refs"]:
                flts.append("openalex:" + "|".join(s["refs"]))
            flts.append("cites:" + s["oa_id"])
            for flt in flts:
                try:
                    extra += parse_oa(json.loads(fetch(
                        "https://api.openalex.org/works?" + q({"filter": flt}))))
                except (RuntimeError, ValueError, KeyError, TypeError):
                    status["openalex"] = "DEGRADED"
        items = merge(items + extra)
        if status["openalex"] == "DEGRADED" and st == "ok":
            st = "partial"
    return st, status, items


def main(argv=None, fetch=http_get):
    import argparse
    ap = argparse.ArgumentParser(prog="lit-search")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--since", type=int)
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--snowball", nargs="?", const=5, type=int)
    ap.add_argument("--mailto", default="")
    ap.add_argument("--out", default="01_notes/search")
    ap.add_argument("--export-bib", nargs="+")
    ap.add_argument("--bib", default="07_references/references.bib")
    a = ap.parse_args(argv)
    log({"main": list(argv or []), "fetch_default": fetch is http_get})
    if a.export_bib:
        return export(a, fetch)
    if not a.query:
        return 2
    st, status, items = search(a, fetch)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = "2026-01-01-query"
    with open(out / (stem + ".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "status", "year", "title", "doi", "indexes"])
        for i, it in enumerate(items, 1):
            w.writerow([i, "RETRACTED" if it["retracted"] else "", it["year"] or "", it["title"],
                        it["doi"], "+".join(sorted(it["indexes"]))])
    receipt = {"query": a.query, "since": a.since, "n": a.n, "provider_status": status,
               "counts": {"after_dedupe": len(items)}, "status": st,
               "mailto": "set" if a.mailto else "unset"}
    (out / (stem + ".receipt.json")).write_text(json.dumps(receipt, indent=2) + "\n")
    (out / (stem + ".md")).write_text("# Search: %s\n" % a.query)
    print("status: %s  providers: %s" % (st, status))
    return 1 if st == "failed" else 0


def export(a, fetch):
    bib = Path(a.bib)
    existing = bib.read_text(encoding="utf-8") if bib.exists() else ""
    keys = set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", existing))
    have = {norm_doi(d) for d in re.findall(r"\bdoi\s*=\s*[{\"]\s*([^}\"]+?)\s*[}\"]", existing, re.I)}
    added = skipped = failed = 0
    new = []
    for raw in a.export_bib:
        doi = norm_doi(raw)
        if doi in have:
            skipped += 1
            continue
        try:
            entry = fetch("https://doi.org/" + urllib.parse.quote(doi, safe="/()"),
                          accept="application/x-bibtex").strip()
        except RuntimeError as e:
            failed += 1
            print("  NOT RESOLVED %s: %s" % (doi, str(e)[:100]))
            continue
        m = re.match(r"@\w+\s*\{\s*([^,\s]+)\s*,", entry)
        if not m:
            failed += 1
            print("  NOT RESOLVED %s: response is not BibTeX" % doi)
            continue
        key = m.group(1)
        k, n = key, 0
        while k in keys:
            k = key + chr(97 + n)
            n += 1
        entry = entry[:m.start(1)] + k + entry[m.end(1):]
        keys.add(k)
        have.add(doi)
        new.append(entry)
        added += 1
    if new:
        bib.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        bib.write_text(existing + sep + "\n\n".join(new) + "\n", encoding="utf-8")
    print("added %d, skipped %d (DOI already present), failed %d" % (added, skipped, failed))
    return 0 if added or skipped else 1
'''

STUB_CHECK_BIB = r'''#!/usr/bin/env python3
import re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
mode = (HERE / "checkbib.mode").read_text().strip() if (HERE / "checkbib.mode").exists() else "ok"
if mode == "exit2":
    print("usage problem", file=sys.stderr)
    sys.exit(2)
if mode == "garbage":
    print("all fine, trust me")
    sys.exit(0)
bib = Path(sys.argv[1])
if not bib.is_file():
    sys.exit(2)
text = bib.read_text(encoding="utf-8")
keys = re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", text)
paper = Path(sys.argv[2]) if len(sys.argv) > 2 else None
cited = set(re.findall(r"@([A-Za-z0-9_]+)", paper.read_text())) if paper and paper.is_file() else set()
errors = ["ERROR cited but not in bib: %s" % k for k in sorted(cited - set(keys))]
notes = ["NOTE  in bib but never cited: %s" % k for k in sorted(set(keys) - cited)]
for line in errors + notes:
    print(line)
print("%d entries, %d errors, %d notes" % (len(keys), len(errors), len(notes)))
sys.exit(1 if errors else 0)
'''


def load_adapter():
    spec = importlib.util.spec_from_file_location("research_adapter_under_test", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def oa_body(items, seed_id=None, refs=()):
    results = []
    for n, (title, doi, year) in enumerate(items):
        w = {"display_name": title, "doi": ("https://doi.org/" + doi) if doi else None,
             "publication_year": year}
        if n == 0 and seed_id:
            w["id"] = "https://openalex.org/" + seed_id
            w["referenced_works"] = ["https://openalex.org/" + r for r in refs]
        results.append(w)
    return {"results": results}


def s2_body(items):
    return {"data": [{"title": t, "year": y, "externalIds": {"DOI": d} if d else {}} for t, d, y in items]}


def bibtex(key, doi, title="Synthetic Title"):
    return "@article{%s,\n  author = {Doe, Jane},\n  title = {%s},\n  year = {2021},\n  doi = {%s}\n}" % (
        key, title, doi)


OA_ITEMS = [("Alpha study", "10.5555/alpha", 2021), ("Beta study", "10.5555/beta", 2022),
            ("Gamma study", "10.5555/gamma", 2020)]
S2_ITEMS = [("Alpha study", "10.5555/alpha", 2021), ("Delta study", "10.5555/delta", 2023)]


def search_routes(oa=True, s2=True):
    routes = []
    if oa:
        routes.append({"match": "api.openalex.org", "body": oa_body(OA_ITEMS)})
    if s2:
        routes.append({"match": "api.semanticscholar.org", "body": s2_body(S2_ITEMS)})
    return routes


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.comp = self.make_stub(self.tmp / "companion")

    def make_stub(self, d, version="0.2.1", name="provenote", lit=True, check=True):
        (d / ".claude-plugin").mkdir(parents=True)
        (d / "bin").mkdir()
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
        if lit:
            (d / "bin" / "lit-search").write_text(STUB_LIT_SEARCH)
        if check:
            (d / "bin" / "check-bib").write_text(STUB_CHECK_BIB)
        return d

    def write(self, name, data):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def brief(self, **over):
        data = {"question": "graph learning", "since": 2020, "n": 5, "snowball": False}
        data.update(over)
        return self.write("brief.json", data)

    def fixture(self, routes, name="fixture.json"):
        return self.write(name, {"routes": routes})

    def cli(self, *args, **kw):
        return subprocess.run([sys.executable, "-B", str(ADAPTER), *map(str, args)],
                              capture_output=True, text=True, cwd=self.tmp, timeout=60, **kw)

    def run_example(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", str(RUN), *map(str, args)],
                              capture_output=True, text=True, cwd=self.tmp, timeout=60, env=env)

    def calls(self, comp=None):
        log = Path(comp or self.comp) / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def search(self, routes=None, brief=None, out="out", comp=None, extra=()):
        routes = search_routes() if routes is None else routes
        return self.cli("search", "--companion", comp or self.comp, "--brief", brief or self.brief(),
                        "--out", self.tmp / out, "--fixture", self.fixture(routes), *extra)

    def cite(self, dois, routes, paper_text="", out="cout", bib="refs.bib", comp=None, extra=()):
        paper = self.write("paper.md", paper_text)
        return self.cli("cite", "--companion", comp or self.comp, "--dois", *dois,
                        "--bib", self.tmp / bib, "--paper", paper, "--out", self.tmp / out,
                        "--fixture", self.fixture(routes, "cite-fixture.json"), *extra)

    def index(self, out="out"):
        return json.loads((self.tmp / out / "notes" / "index.json").read_text())

    def citations(self, out="cout"):
        return json.loads((self.tmp / out / "citations.json").read_text())


def doi_route(doi, key, entry_doi=None, title="Synthetic Title"):
    return {"match": "doi.org/" + doi, "body": bibtex(key, entry_doi or doi, title)}


class A1NoCompanion(Base):
    def test_handshake_without_companion(self):
        r = self.cli("handshake", "--format", "json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertFalse(data["integrated"])
        self.assertIsNone(data["companion"])
        for cap in ("34", "35"):
            self.assertTrue(data["capabilities"][cap].startswith("unavailable: no companion given"))
        text = self.cli("handshake")
        self.assertIn("unavailable: no companion given", text.stdout)

    def test_missing_dir_is_unavailable(self):
        r = self.cli("handshake", "--companion", self.tmp / "nowhere", "--format", "json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertFalse(data["integrated"])
        self.assertTrue(data["capabilities"]["34"].startswith("unavailable"))

    def test_example_offline_without_companion(self):
        r = self.run_example("--offline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("research companion: unavailable", r.stdout)
        self.assertIn("NOT integrated", r.stdout)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p != self.comp], [])

    def test_example_requires_offline_flag(self):
        self.assertEqual(self.run_example().returncode, 2)

    def test_example_ignores_env_companion(self):
        env = dict(os.environ, RESEARCH_COMPANION_DIR=str(self.comp))
        r = self.run_example("--offline", env=env)
        self.assertIn("unavailable", r.stdout)
        self.assertFalse((self.comp / "imported.sentinel").exists())


class A2Handshake(Base):
    def hs(self, comp):
        r = self.cli("handshake", "--companion", comp, "--format", "json")
        return r, (json.loads(r.stdout) if r.returncode == 0 else None)

    def test_good_companion(self):
        r, d = self.hs(self.comp)
        self.assertEqual(d["capabilities"], {"34": "available", "35": "available"})
        self.assertTrue(d["integrated"])
        self.assertEqual(d["companion"], {"name": "provenote", "version": "0.2.1"})
        self.assertFalse((self.comp / "imported.sentinel").exists())

    def test_patch_version_ok_but_two_part_not(self):
        r, d = self.hs(self.make_stub(self.tmp / "c2", version="0.2.17"))
        self.assertTrue(d["integrated"])
        r, d = self.hs(self.make_stub(self.tmp / "c3", version="0.2"))
        self.assertFalse(d["integrated"])

    def test_refusals_are_never_integrated(self):
        cases = {}
        c = self.make_stub(self.tmp / "nopj")
        (c / ".claude-plugin" / "plugin.json").unlink()
        cases["plugin.json"] = c
        c = self.make_stub(self.tmp / "dup")
        (c / ".claude-plugin" / "plugin.json").write_text('{"name": "provenote", "name": "x", "version": "0.2.1"}')
        cases["duplicate"] = c
        cases["name"] = self.make_stub(self.tmp / "nm", name="other")
        for needle, c in cases.items():
            r, d = self.hs(c)
            self.assertEqual(r.returncode, 0, needle)
            self.assertFalse(d["integrated"], needle)
            for cap in ("34", "35"):
                self.assertTrue(d["capabilities"][cap].startswith("unavailable"), (needle, d))
                self.assertIn(needle, d["capabilities"][cap])

    def test_unsupported_versions(self):
        for v in ("0.3.0", "1.0.0"):
            r, d = self.hs(self.make_stub(self.tmp / ("v" + v), version=v))
            self.assertFalse(d["integrated"])
            for cap in ("34", "35"):
                self.assertEqual(d["capabilities"][cap], "unsupported: version %s (tested 0.2.x)" % v)

    def test_symlinks_exit_2(self):
        c = self.make_stub(self.tmp / "sl")
        real = c / ".claude-plugin" / "plugin.json"
        moved = self.tmp / "pj.json"
        real.rename(moved)
        real.symlink_to(moved)
        self.assertEqual(self.cli("handshake", "--companion", c).returncode, 2)
        link = self.tmp / "linkdir"
        link.symlink_to(self.comp)
        self.assertEqual(self.cli("handshake", "--companion", link).returncode, 2)

    def test_check_bib_absent_only_35(self):
        r, d = self.hs(self.make_stub(self.tmp / "nocb", check=False))
        self.assertEqual(d["capabilities"]["34"], "available")
        self.assertTrue(d["capabilities"]["35"].startswith("unavailable"))
        self.assertIn("bin/check-bib", d["capabilities"]["35"])
        self.assertFalse(d["integrated"])

    def test_lit_search_absent_blocks_both(self):
        r, d = self.hs(self.make_stub(self.tmp / "nols", lit=False))
        self.assertIn("bin/lit-search", d["capabilities"]["34"])
        self.assertTrue(d["capabilities"]["35"].startswith("unavailable"))
        self.assertFalse(d["integrated"])

    def test_symlinked_bin_refused(self):
        c = self.make_stub(self.tmp / "slbin")
        real = c / "bin" / "lit-search"
        real.rename(self.tmp / "ls-real")
        real.symlink_to(self.tmp / "ls-real")
        r, d = self.hs(c)
        self.assertTrue(d["capabilities"]["34"].startswith("unavailable"))
        self.assertFalse(d["integrated"])
        r = self.search(comp=c)
        self.assertEqual(r.returncode, 3)
        self.assertFalse((c / "imported.sentinel").exists())

    def test_bad_arguments_exit_2(self):
        self.assertEqual(self.cli("handshake", "--bogus").returncode, 2)
        self.assertEqual(self.cli("handshake", "--format", "xml").returncode, 2)


class A3NoSilentNetwork(Base):
    def test_search_and_cite_need_a_flag(self):
        r = self.cli("search", "--companion", self.comp, "--brief", self.brief(), "--out", self.tmp / "o")
        self.assertEqual(r.returncode, 2)
        r2 = self.cli("cite", "--companion", self.comp, "--dois", "10.5555/a", "--bib", self.tmp / "b.bib",
                      "--paper", self.tmp / "p.md", "--out", self.tmp / "o2")
        self.assertEqual(r2.returncode, 2)
        self.assertFalse((self.comp / "imported.sentinel").exists())

    def test_both_flags_exit_2(self):
        r = self.search(extra=("--allow-network",))
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.comp / "imported.sentinel").exists())

    def test_handshake_never_imports(self):
        self.cli("handshake", "--companion", self.comp)
        self.assertFalse((self.comp / "imported.sentinel").exists())

    def test_fixture_run_does_import(self):
        self.assertEqual(self.search().returncode, 0)
        self.assertTrue((self.comp / "imported.sentinel").exists())

    def test_allow_network_passes_default_fetch(self):
        r = self.cli("search", "--companion", self.comp, "--brief", self.brief(), "--out", self.tmp / "o",
                     "--allow-network")
        self.assertEqual(r.returncode, 1)  # stub providers unavailable: nothing fetched, no socket
        self.assertTrue(self.calls()[0]["fetch_default"])

    def test_fixture_run_never_connects(self):
        adapter = load_adapter()
        fixture = self.fixture(search_routes())
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")) as m:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = adapter.main(["search", "--companion", str(self.comp), "--brief", str(self.brief()),
                                   "--out", str(self.tmp / "o"), "--fixture", str(fixture)])
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_count, 0)


class A4Discovery(Base):
    def test_discovery_through_companion(self):
        r = self.search()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = [c for c in self.calls() if "main" in c]
        self.assertEqual(len(calls), 1)
        argv = calls[0]["main"]
        self.assertIn("graph learning", argv)
        self.assertEqual(argv[argv.index("--since") + 1], "2020")
        self.assertEqual(argv[argv.index("--n") + 1], "5")
        self.assertFalse(calls[0]["fetch_default"])
        idx = self.index()
        self.assertEqual(idx["status"], "ok")
        self.assertEqual(idx["companion"], {"name": "provenote", "version": "0.2.1"})
        notes = sorted((self.tmp / "out" / "notes").glob("*.md"))
        self.assertEqual(len(notes), 4)
        self.assertEqual(len(idx["notes"]), 4)
        alpha = next(n for n in notes if "alpha" in n.name).read_text()
        self.assertIn("10.5555/alpha", alpha)
        self.assertIn("openalex+semanticscholar", alpha)
        self.assertIn("trust: untrusted fetched metadata", alpha)
        rec = idx["companion_receipt"]
        self.assertFalse(Path(rec["path"]).is_absolute())
        data = (self.tmp / "out" / rec["path"]).read_bytes()
        self.assertEqual(rec["sha256"], hashlib.sha256(data).hexdigest())

    def test_existing_companion_output_refused(self):
        (self.tmp / "out" / "companion").mkdir(parents=True)
        (self.tmp / "out" / "companion" / "x").write_text("x")
        self.assertEqual(self.search().returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_mailto_forwarded_only_when_given(self):
        self.search()
        self.assertNotIn("--mailto", self.calls()[0]["main"])
        self.search(out="out2", extra=("--mailto", "someone@example.test"))
        argv = self.calls()[1]["main"]
        self.assertEqual(argv[argv.index("--mailto") + 1], "someone@example.test")

    def test_unsafe_dois_stay_inside_notes(self):
        oa = oa_body([("Evil ../../title\nwith newline", "10.5555/../../escape", 2020),
                      ("Nodoi Title", None, 2021)])
        r = self.search(routes=[{"match": "api.openalex.org", "body": oa}])
        self.assertEqual(r.returncode, 0, r.stderr)
        notes = self.tmp / "out" / "notes"
        for p in (self.tmp).rglob("*"):
            if p.is_file() and p.suffix == ".md" and "notes" in p.parts and "companion" not in p.parts:
                self.assertTrue(p.resolve().parent == notes.resolve(), p)
        names = sorted(p.name for p in notes.glob("*.md"))
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertRegex(n, r"^[0-9]+-[a-z0-9-]+\.md$")
            self.assertLessEqual(len(n), 80)
        nodoi = next(p for p in notes.glob("*.md") if "nodoi" in p.name).read_text()
        self.assertIn("doi: none", nodoi)
        self.assertFalse((self.tmp / "out" / "escape").exists())
        evil = next(p for p in notes.glob("*.md") if "nodoi" not in p.name).read_text()
        self.assertEqual(len(evil.splitlines()[0].splitlines()), 1)
        self.assertIn("trust: untrusted fetched metadata", evil)

    def test_dup_keys_and_bad_brief_rejected(self):
        bad = [self.write("b1.json", '{"question": "q", "question": "r"}')]
        for over in ({"n": 0}, {"n": "5"}, {"n": True}, {"since": "2020"}, {"snowball": "x"},
                     {"question": ""}, {"question": "-x"}, {"bogus": 1}):
            bad.append(self.brief(**over))
        for b in bad:
            r = self.search(brief=b)
            self.assertEqual(r.returncode, 2, (b.read_text(), r.stderr))
        self.assertFalse((self.comp / "imported.sentinel").exists())

    def test_fixture_validation(self):
        dup = self.write("fx-dup.json", '{"routes": [], "routes": []}')
        r = self.cli("search", "--companion", self.comp, "--brief", self.brief(), "--out", self.tmp / "o",
                     "--fixture", dup)
        self.assertEqual(r.returncode, 2)
        self.write("outside.txt", json.dumps(oa_body(OA_ITEMS)))
        esc = self.write("sub/fx.json", {"routes": [{"match": "openalex", "body_file": "../outside.txt"}]})
        r = self.cli("search", "--companion", self.comp, "--brief", self.brief(), "--out", self.tmp / "o2",
                     "--fixture", esc)
        self.assertEqual(r.returncode, 2)
        self.write("sub/ok.txt", json.dumps(oa_body(OA_ITEMS)))
        ok = self.write("sub/fx2.json", {"routes": [{"match": "openalex", "body_file": "ok.txt"}]})
        r = self.cli("search", "--companion", self.comp, "--brief", self.brief(), "--out", self.tmp / "o3",
                     "--fixture", ok)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.index("o3")["provider_status"]["semanticscholar"], "UNAVAILABLE")

    def test_contract_mismatch(self):
        c = self.make_stub(self.tmp / "nomain")
        (c / "bin" / "lit-search").write_text("x = 1\n")
        r = self.search(comp=c)
        self.assertEqual(r.returncode, 3)
        self.assertIn("companion contract mismatch", r.stderr + r.stdout)
        c2 = self.make_stub(self.tmp / "badsyntax")
        (c2 / "bin" / "lit-search").write_text("def (\n")
        r = self.search(comp=c2, out="o2")
        self.assertEqual(r.returncode, 3)
        self.assertIn("companion contract mismatch", r.stderr + r.stdout)


class A5Degraded(Base):
    def test_partial_visible(self):
        r = self.search(routes=search_routes(s2=False))
        self.assertEqual(r.returncode, 0)
        idx = self.index()
        self.assertEqual(idx["status"], "partial")
        self.assertEqual(idx["provider_status"]["semanticscholar"], "UNAVAILABLE")
        self.assertEqual(idx["provider_status"]["openalex"], "READY")
        self.assertIn("semanticscholar: UNAVAILABLE", r.stdout)
        receipt = json.loads(next((self.tmp / "out" / "companion").glob("*.receipt.json")).read_text())
        self.assertEqual(idx["status"], receipt["status"])
        self.assertEqual(idx["provider_status"], receipt["provider_status"])

    def test_status_passed_verbatim(self):
        # a companion that reports an unexpected status must be mirrored, not rewritten
        c = self.make_stub(self.tmp / "odd")
        src = (c / "bin" / "lit-search").read_text().replace('"status": st,', '"status": "weird",')
        (c / "bin" / "lit-search").write_text(src)
        r = self.search(comp=c)
        self.assertEqual(self.index()["status"], "weird")

    def test_no_route_fails_without_notes(self):
        r = self.search(routes=[])
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.tmp / "out" / "notes").exists())

    def test_snowball_degraded(self):
        oa = oa_body(OA_ITEMS, seed_id="W1", refs=("W9",))
        routes = [{"match": "search=", "body": oa},
                  {"match": "api.semanticscholar.org", "body": s2_body(S2_ITEMS)}]
        r = self.search(routes=routes, brief=self.brief(snowball=2))
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.calls()[0]["main"]
        self.assertEqual(argv[argv.index("--snowball") + 1], "2")
        idx = self.index()
        self.assertEqual(idx["provider_status"]["openalex"], "DEGRADED")
        self.assertEqual(idx["status"], "partial")
        self.assertIn("openalex: DEGRADED", r.stdout)

    def test_snowball_ok_when_routes_exist(self):
        oa = oa_body(OA_ITEMS, seed_id="W1", refs=("W9",))
        extra = oa_body([("Cited work", "10.5555/cited", 2019)])
        routes = [{"match": "cites%3A", "body": extra}, {"match": "openalex%3A", "body": extra},
                  {"match": "api.openalex.org", "body": oa},
                  {"match": "api.semanticscholar.org", "body": s2_body(S2_ITEMS)}]
        r = self.search(routes=routes, brief=self.brief(snowball=True))
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.calls()[0]["main"]
        self.assertIn("--snowball", argv)
        self.assertEqual(self.index()["status"], "ok")
        self.assertEqual(len(self.index()["notes"]), 5)

    def test_example_shows_degraded(self):
        brief = self.brief()
        fx = self.fixture(search_routes(s2=False))
        r = self.run_example("--offline", "--companion", self.comp, "--out", self.tmp / "ex", "--brief", brief,
                             "--fixture", fx)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("semanticscholar: UNAVAILABLE", r.stdout)


class A6Citations(Base):
    A = "10.5555/a"
    B = "10.5555/b"

    def test_one_unresolved(self):
        r = self.cite([self.A, self.B], [doi_route(self.A, "keya")])
        self.assertEqual(r.returncode, 1)
        c = self.citations()
        self.assertEqual({e["doi"]: e["status"] for e in c["entries"]},
                         {self.A: "resolved", self.B: "NOT RESOLVED"})
        self.assertFalse(c["verified"])
        self.assertEqual(len(re.findall(r"@article", (self.tmp / "refs.bib").read_text())), 1)

    def test_verified_when_all_good(self):
        routes = [doi_route(self.A, "keya"), doi_route(self.B, "keyb")]
        r = self.cite([self.A, self.B], routes, paper_text="See @keya and @keyb.\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.citations()
        self.assertTrue(c["verified"])
        self.assertEqual(c["check_bib"], {"exit": 0, "summary_line": "2 entries, 0 errors, 0 notes"})
        self.assertEqual({e["citekey"] for e in c["entries"]}, {"keya", "keyb"})

    def test_check_bib_exit_decides(self):
        routes = [doi_route(self.A, "keya"), doi_route(self.B, "keyb")]
        self.cite([self.A, self.B], routes, paper_text="See @keya, @keyb and @ghost.\n")
        c = self.citations()
        self.assertEqual(c["check_bib"]["exit"], 1)
        self.assertFalse(c["verified"])
        self.assertTrue(all(e["status"] == "resolved" for e in c["entries"]))

    def test_cite_requires_35(self):
        c = self.make_stub(self.tmp / "nocb", check=False)
        r = self.cite([self.A], [doi_route(self.A, "keya")], comp=c)
        self.assertEqual(r.returncode, 3)
        self.assertFalse((self.tmp / "cout" / "citations.json").exists())
        self.assertIn("bin/check-bib", r.stdout + r.stderr)

    def test_check_bib_exit_2_or_garbage(self):
        routes = [doi_route(self.A, "keya")]
        (self.comp / "checkbib.mode").write_text("exit2")
        self.cite([self.A], routes, paper_text="@keya")
        c = self.citations()
        self.assertEqual(c["check_bib"]["exit"], 2)
        self.assertFalse(c["verified"])
        (self.comp / "checkbib.mode").write_text("garbage")
        self.cite([self.A], routes, paper_text="@keya", out="cout2", bib="refs2.bib")
        c = self.citations("cout2")
        self.assertEqual(c["check_bib"]["exit"], 0)
        self.assertFalse(c["verified"])

    def test_bad_doi_argument(self):
        r = self.cite(["-x"], [])
        self.assertEqual(r.returncode, 2)


class A7Identity(Base):
    A = "10.5555/a"

    def test_mismatch(self):
        r = self.cite([self.A], [doi_route(self.A, "keya", entry_doi="10.5555/other")], paper_text="@keya")
        self.assertEqual(r.returncode, 1)
        c = self.citations()
        self.assertEqual(c["entries"][0]["status"], "identity-mismatch")
        self.assertFalse(c["verified"])
        self.assertEqual(c["entries"][0]["citekey"], "keya")

    def test_suffixed_key_matches_bib(self):
        (self.tmp / "refs.bib").write_text(bibtex("keya", "10.5555/zzz") + "\n")
        r = self.cite([self.A], [doi_route(self.A, "keya")], paper_text="@keya @keyaa")
        c = self.citations()
        self.assertIn("@article{%s," % c["entries"][0]["citekey"], (self.tmp / "refs.bib").read_text())
        self.assertEqual(c["entries"][0]["status"], "resolved")
        self.assertEqual(c["entries"][0]["citekey"], "keyaa")

    def test_preexisting_entry_resolved(self):
        original = bibtex("keyx", "10.5555/zzz") + "\n\n" + bibtex("keya", "https://doi.org/10.5555/A") + "\n"
        (self.tmp / "refs.bib").write_text(original)
        r = self.cite([self.A], [], paper_text="@keya @keyx")
        c = self.citations()
        self.assertEqual(c["entries"][0], {"doi": self.A, "citekey": "keya", "status": "resolved"})
        self.assertEqual((self.tmp / "refs.bib").read_text(), original)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_matching_entry_among_new(self):
        (self.tmp / "refs.bib").write_text(bibtex("keyx", "10.5555/zzz") + "\n")
        self.cite([self.A, "10.5555/b"], [doi_route(self.A, "keya"), doi_route("10.5555/b", "keyb")],
                  paper_text="@keya @keyb @keyx")
        c = self.citations()
        self.assertEqual([e["citekey"] for e in c["entries"]], ["keya", "keyb"])
        self.assertTrue(c["verified"])


class A8RealCompanion(Base):
    def test_real_companion(self):
        directory = os.environ.get("RESEARCH_COMPANION_DIR")
        if not directory:
            self.skipTest("set RESEARCH_COMPANION_DIR to a provenote checkout to run against the real companion")
        comp = Path(directory)
        r = self.cli("handshake", "--companion", comp, "--format", "json")
        hs = json.loads(r.stdout)
        self.assertTrue(hs["integrated"], hs)
        r = self.search(comp=comp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        idx = self.index()
        self.assertEqual(idx["status"], "ok")
        self.assertEqual(len(idx["notes"]), 4)
        rec = idx["companion_receipt"]
        self.assertEqual(rec["sha256"], hashlib.sha256((self.tmp / "out" / rec["path"]).read_bytes()).hexdigest())
        routes = [doi_route("10.5555/a", "keya"), doi_route("10.5555/b", "keyb")]
        r = self.cite(["10.5555/a", "10.5555/b"], routes, paper_text="See @keya and @keyb.\n", comp=comp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = self.citations()
        self.assertTrue(c["verified"], c)
        r = self.cite(["10.5555/a", "10.5555/b"], routes[:1], paper_text="@keya", out="cout2", bib="refs2.bib",
                      comp=comp)
        c = self.citations("cout2")
        self.assertFalse(c["verified"])
        self.assertEqual([e["status"] for e in c["entries"]], ["resolved", "NOT RESOLVED"])


class A9Portable(Base):
    MAILTO = "someone@example.test"

    def all_outputs(self, *roots):
        for root in roots:
            for p in Path(root).rglob("*"):
                if p.is_file():
                    yield p, p.read_text(encoding="utf-8", errors="replace")

    def test_no_machine_identity_and_no_mailto(self):
        self.search(extra=("--mailto", self.MAILTO))
        self.cite(["10.5555/a"], [doi_route("10.5555/a", "keya")], paper_text="@keya")
        banned = ["/" + "home/", socket.gethostname()]
        user = getpass.getuser()
        files = list(self.all_outputs(self.tmp / "out", self.tmp / "cout"))
        self.assertGreater(len(files), 5)
        for p, text in files:
            for b in banned:
                self.assertNotIn(b, text, p)
            if len(user) >= 3:
                self.assertIsNone(re.search(r"\b%s\b" % re.escape(user), text), p)
            self.assertNotIn(self.MAILTO, text, p)
            self.assertNotIn(str(self.tmp), text, p)

    def test_example_with_companion_and_shipped_files(self):
        r = self.run_example("--offline", "--companion", self.comp, "--out", self.tmp / "ex")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("research companion: provenote 0.2.1", r.stdout)
        idx = json.loads((self.tmp / "ex" / "notes" / "index.json").read_text())
        self.assertEqual(idx["status"], "ok")
        cit = json.loads((self.tmp / "ex" / "citations.json").read_text())
        self.assertTrue(cit["verified"], cit)
        self.assertNotIn(str(self.tmp), r.stdout)


class A10Descriptor(unittest.TestCase):
    def test_descriptor_and_adapter(self):
        spec = importlib.util.spec_from_file_location("contracts_u17", ROOT / "modules/contracts/contracts.py")
        contracts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contracts)
        d = ROOT / "modules" / "research-adapter"
        descriptor = contracts.check_module_descriptor(d, module="research-adapter")
        self.assertEqual(descriptor["claimed_support"], "planned")
        self.assertEqual(descriptor["evidence"], [])
        self.assertEqual(descriptor["dependencies"],
                         [{"identifier": "provenote", "version": "0.2.0", "status": "declared-untested"}])
        adapter = load_adapter().ADAPTER
        out = contracts.execute_adapter(adapter, "describe", 1, descriptor["dependencies"])
        self.assertEqual(out["module"], "research-adapter")
        out = contracts.execute_adapter(adapter, "handle", 1, descriptor["dependencies"], "provenote")
        self.assertFalse(out["integrated"])
        readme = (d / "README.md").read_text()
        for term in ("openalex", "semanticscholar", "doi.org", "Zotero"):
            self.assertIn(term.lower(), readme.lower())


class A11Privacy(unittest.TestCase):
    def test_privacy_scan_and_extra_terms(self):
        spec = importlib.util.spec_from_file_location("privacy_scan_u17", ROOT / "scripts/privacy_scan.py")
        scan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scan)
        for d in NEW_DIRS:
            self.assertEqual(scan.scan(d), [], d)
        terms = ["Pro" + "jects/", "research-kit" + "-src", "claude" + "_improvement", "~" + "/",
                 "/" + "home/", socket.gethostname()]
        user = getpass.getuser()
        for d in NEW_DIRS:
            for p in d.rglob("*"):
                if not p.is_file() or "__pycache__" in p.parts:
                    continue
                text = p.read_text(encoding="utf-8")
                if p.name == "test_research_adapter.py":
                    continue  # the test file builds its needles from fragments; user name check below
                for t in terms:
                    self.assertNotIn(t, text, (p, t))
                if len(user) >= 3:
                    self.assertIsNone(re.search(r"\b%s\b" % re.escape(user), text), p)
        # the test file itself: only fragment-built terms are allowed
        own = Path(__file__).read_text(encoding="utf-8")
        for t in ("/" + "home/", "research-kit" + "-src", "claude" + "_improvement"):
            self.assertNotIn(t, own)

    def test_env_var_is_the_only_path_hint(self):
        text = (ROOT / "modules/research-adapter/research_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("RESEARCH_COMPANION_DIR", text)


if __name__ == "__main__":
    unittest.main()
