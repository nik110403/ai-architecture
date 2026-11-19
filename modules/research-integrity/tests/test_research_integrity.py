"""research-integrity acceptance tests (A1-A13 plus tribune amendments F1-F14).

A logging contract stub companion (strict about the argv grammar, cwd and note paths) stands in for provenote.
Tests marked "real" run against the companion named by RESEARCH_COMPANION_DIR and skip otherwise.
Refusal tests start from the stub PASS baseline (init, query, check, finalize), plant one defect and
assert that the refusal names it. Mutations are applied to a copy of the module and must be caught.
"""

import contextlib
import getpass
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MOD = ROOT / "modules" / "research-integrity"
CLI = MOD / "integrity.py"
MINI = ROOT / "examples" / "research-companion" / "mini_study"
VERIFY = ROOT / "examples" / "research-companion" / "verify_receipt.py"
REAL = os.environ.get("RESEARCH_COMPANION_DIR")
SKIP_REAL = "set RESEARCH_COMPANION_DIR to a provenote plugin directory to run this against the real companion"
HOMEFRAG = "/" + "home/"
NOTE_A, NOTE_B = "rivera2020synthetic", "okafor2021invented"

STUB_COMMON = r'''
import json, os, signal, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
NAME = Path(__file__).name
with open(HERE / "calls.log", "a", encoding="utf-8") as _f:
    _f.write(json.dumps({"bin": NAME, "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")


def mode():
    path = HERE / ("mode-" + NAME)
    return path.read_text().strip() if path.exists() else "ok"


def usage():
    sys.exit(2)


def settle(default=0):
    m = mode()
    if m.startswith("exit:"):
        sys.exit(int(m[5:]))
    if m == "signal":
        os.kill(os.getpid(), signal.SIGKILL)
    if m == "sleep":
        time.sleep(120)
    sys.exit(default)
'''

STUB_VERIFY = STUB_COMMON + r'''
import re
args = sys.argv[1:]
if len(args) < 2 or args[0] != "--strict":
    usage()
failing = set((HERE / "fail-notes").read_text().split()) if (HERE / "fail-notes").exists() else set()
bad = False
for a in args[1:]:
    if a.startswith("-") or not Path(a).is_file():
        usage()
    if "01_notes" not in Path(os.path.abspath(a)).parent.parts:
        usage()
    text = Path(a).read_text(encoding="utf-8")
    ref = re.search(r"^source_md:\s*(\S+)", text, re.M)
    root = Path(os.path.abspath(a)).parent
    while root.name != "01_notes":
        root = root.parent
    source = (root.parent / ref.group(1)).read_text(encoding="utf-8") if ref and (root.parent / ref.group(1)).is_file() else ""
    for q in re.findall(r'^\s+text:\s*"(.*)"\s*$', text, re.M):
        if q not in source:
            bad = True
    if Path(a).stem in failing:
        bad = True
settle(1 if bad else 0)
'''

STUB_CLAIMS = STUB_COMMON + r'''
args = sys.argv[1:]
if len(args) != 4 or args[0] != "--strict-quotes" or args[1] != "--root":
    usage()
if not Path(args[2]).is_dir() or not Path(args[3]).is_file():
    usage()
settle(1 if (HERE / "fail-claims").exists() else 0)
'''

STUB_NEW_PROJECT = STUB_COMMON + r'''
args = sys.argv[1:]
if len(args) != 1 or args[0].startswith("-"):
    usage()
target = Path(args[0])
if target.exists() and (not target.is_dir() or any(target.iterdir())):
    sys.exit(1)
if mode() == "ok":
    for d in ("00_admin", "01_notes/lit", "02_sources/pdf", "03_code", "10_paper"):
        (target / d).mkdir(parents=True, exist_ok=True)
    (target / "03_code" / ".gitkeep").write_text("")
settle(0)
'''

STUB_NEW_PIPELINE = STUB_COMMON + r'''
args = sys.argv[1:]
if len(args) != 3 or args[1] != "--lang" or args[2] not in ("python", "r") or args[0].startswith("-"):
    usage()
target = Path(args[0])
if target.exists() and (not target.is_dir() or any(target.iterdir())):
    sys.exit(1)
if mode() == "ok":
    target.mkdir(parents=True, exist_ok=True)
    (target / "run_all.R").write_text("# stub\n")
settle(0)
'''


def make_stub(root, version="0.2.1", name="provenote"):
    root = Path(root)
    (root / ".claude-plugin").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
    for bin_name, source in (("verify-quotes", STUB_VERIFY), ("check-claims", STUB_CLAIMS),
                             ("new-project", STUB_NEW_PROJECT), ("new-pipeline", STUB_NEW_PIPELINE)):
        (root / "bin" / bin_name).write_text(source)
    return root


def calls(companion, name=None):
    path = Path(companion) / "calls.log"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if name is None or r["bin"] == name]


def tree_hash(root, skip_research=False):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        if skip_research and rel.split("/")[0] == ".research":
            continue
        if path.is_file():
            digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


MODULES = {}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ri-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = make_stub(self.tmp / "stub")
        self.counter = 0

    def fresh(self, name="w"):
        self.counter += 1
        path = self.tmp / f"{name}{self.counter}"
        path.mkdir()
        return path

    def run_cli(self, *args, cli=CLI, env=None, expect=None):
        """Run the CLI in process (fast); a mutated copy of the module is loaded from its own path."""
        module = MODULES.get(str(cli)) or MODULES.setdefault(str(cli), load_module(f"integrity_{len(MODULES)}", cli))
        saved = dict(os.environ)
        os.environ.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = module.main([str(a) for a in args])
        finally:
            os.environ.clear()
            os.environ.update(saved)
        done = subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())
        if expect is not None:
            self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def index(self, status="ok"):
        out = self.fresh("idx")
        (out / "companion").mkdir()
        receipt = out / "companion" / "r.receipt.json"
        receipt.write_text(json.dumps({"status": status, "provider_status": {}}))
        (out / "notes").mkdir()
        (out / "notes" / "index.json").write_text(json.dumps({
            "companion": {"name": "provenote", "version": "0.2.1"},
            "companion_receipt": {"path": "companion/r.receipt.json", "sha256": sha(receipt)},
            "status": status, "provider_status": {}, "notes": []}))
        return out / "notes" / "index.json"

    def project(self):
        target = self.fresh("proj") / "p"
        shutil.copytree(MINI, target)
        return target

    def up_to_check(self, companion=None, cli=CLI, plant=None, check_expect=0):
        """Scaffold (new-project into a fresh dir, mini study copied in), init, query, check."""
        companion = companion or self.stub
        target = self.fresh("proj") / "p"
        self.run_cli("init", "--project", target, "--companion", self.stub, cli=cli, expect=0)
        shutil.copytree(MINI, target, dirs_exist_ok=True)
        if plant:
            plant(target)
        self.run_cli("query", "--project", target, "--index", self.index(), cli=cli, expect=0)
        done = self.run_cli("check", "--project", target, "--companion", companion, cli=cli,
                            expect=check_expect)
        return target, done

    def baseline(self, companion=None, cli=CLI, plant=None):
        target, _ = self.up_to_check(companion, cli, plant)
        self.run_cli("finalize", "--project", target, cli=cli, expect=0)
        return target

    def release(self, target, companion=None, cli=CLI, expect=0):
        out = self.fresh("out") / "o"
        done = self.run_cli("release", "--project", target, "--out", out, cli=cli, expect=expect)
        return out, done

    def rebind(self, target):
        """Refresh the reviewed hashes of audit/referee after a planted edit, so only one defect is planted."""
        for name in ("audit", "referee"):
            path = target / "00_admin" / f"{name}.json"
            data = json.loads(path.read_text())
            data["reviewed"] = {rel: sha(target / rel) for rel in data["reviewed"]}
            path.write_text(json.dumps(data))

    def mutant(self, old, new, in_file="integrity.py", also=()):
        root = self.fresh("mut")
        shutil.copytree(MOD, root / "modules" / "research-integrity",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(ROOT / "modules" / "research-adapter", root / "modules" / "research-adapter",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        path = root / "modules" / "research-integrity" / in_file
        text = path.read_text()
        self.assertEqual(text.count(old), 1, f"mutation anchor not unique: {old!r}")
        text = text.replace(old, new)
        for more_old, more_new in also:
            self.assertEqual(text.count(more_old), 1, f"mutation anchor not unique: {more_old!r}")
            text = text.replace(more_old, more_new)
        path.write_text(text)
        return path


# ---------------------------------------------------------------- A1, A2, F5

class TestUnavailableAndDelegation(Base):
    def scenario_a1(self, cli):
        target = self.project()
        self.run_cli("init", "--project", target, cli=cli, expect=0)
        self.run_cli("query", "--project", target, "--index", self.index(), cli=cli, expect=0)
        done = self.run_cli("check", "--project", target, cli=cli, expect=3)
        self.assertIn("quotes: UNAVAILABLE: no companion given (--companion DIR)", done.stdout)
        self.assertIn("claims: UNAVAILABLE: no companion given (--companion DIR)", done.stdout)
        self.assertIn("overall: INCOMPLETE", done.stdout)
        self.run_cli("finalize", "--project", target, cli=cli, expect=3)
        out = self.fresh("out") / "o"
        self.run_cli("release", "--project", target, "--out", out, cli=cli, expect=3)
        self.assertFalse((out / "release-manifest.json").exists())

    def test_a1_unavailable_is_never_pass(self):
        self.scenario_a1(CLI)

    def test_a1_mutation_unavailable_as_pass(self):
        mutant = self.mutant('("INCOMPLETE" if states - {"PASS"} else "PASS")',
                             '("INCOMPLETE" if states - {"PASS", "UNAVAILABLE"} else "PASS")')
        with self.assertRaises(AssertionError):
            self.scenario_a1(mutant)

    def scenario_a2(self, cli):
        target, done = self.up_to_check(cli=cli)
        notes = sorted((target / "01_notes" / "lit").glob("*.md"))
        quote_calls = calls(self.stub, "verify-quotes")
        self.assertEqual(len(quote_calls), len(notes))
        self.assertEqual(sorted(c["argv"] for c in quote_calls),
                         sorted(["--strict", str(n)] for n in notes))
        claim_calls = calls(self.stub, "check-claims")
        self.assertEqual(len(claim_calls), 1)
        self.assertEqual(claim_calls[0]["argv"], ["--strict-quotes", "--root", str(target),
                                                  str(target / "10_paper" / "draft.md")])
        for c in quote_calls + claim_calls:
            self.assertEqual(Path(c["cwd"]).resolve(), target.resolve())

    def test_a2_delegation_is_real(self):
        self.scenario_a2(CLI)

    def test_a2_mutation_skip_subprocess(self):
        mutant = self.mutant('state, why = companion.run("verify-quotes", ["--strict", str(project / rel)], str(project))',
                             'state, why = "PASS", ""')
        with self.assertRaises(AssertionError):
            self.scenario_a2(mutant)

    def test_a2_stub_exit_one_fails_each_check(self):
        (self.stub / "mode-verify-quotes").write_text("exit:1")
        _, done = self.up_to_check(check_expect=1)
        self.assertIn("quotes: FAIL", done.stdout)
        self.assertIn("overall: FAIL", done.stdout)
        (self.stub / "mode-verify-quotes").unlink()
        (self.stub / "fail-claims").write_text("1")
        _, done = self.up_to_check(check_expect=1)
        self.assertIn("claims: FAIL", done.stdout)
        self.assertIn("quotes: PASS", done.stdout)

    def test_a2_missing_bin_makes_only_that_check_unavailable(self):
        (self.stub / "bin" / "check-claims").unlink()
        _, done = self.up_to_check(check_expect=3)
        self.assertIn("claims: UNAVAILABLE: requires bin/check-claims", done.stdout)
        self.assertIn("quotes: PASS", done.stdout)
        self.assertEqual(calls(self.stub, "check-claims"), [])

    def test_f4_failure_taxonomy_is_fail_not_unavailable(self):
        for mode, word in (("exit:2", "usage"), ("exit:7", "crash"), ("signal", "crash")):
            (self.stub / "mode-check-claims").write_text(mode)
            _, done = self.up_to_check(check_expect=1)
            line = next(l for l in done.stdout.splitlines() if l.startswith("claims:"))
            self.assertIn(f"FAIL: 10_paper/draft.md: {word}", line)
        (self.stub / "mode-check-claims").write_text("sleep")
        target = self.fresh("proj") / "p"
        shutil.copytree(MINI, target)
        self.run_cli("init", "--project", target, expect=0)
        self.run_cli("query", "--project", target, "--index", self.index(), expect=0)
        done = self.run_cli("check", "--project", target, "--companion", self.stub, "--bin-timeout", "1", expect=1)
        self.assertIn("claims: FAIL: 10_paper/draft.md: timeout", done.stdout)

    def test_f4_stub_rejects_off_grammar_argv(self):
        target = self.project()
        note = str(target / "01_notes" / "lit" / f"{NOTE_A}.md")
        vq = self.stub / "bin" / "verify-quotes"
        cc = self.stub / "bin" / "check-claims"
        stray = self.fresh("stray") / "n.md"
        stray.write_text("x")
        for argv in ([vq, "--bogus", note], [vq, note, "--strict"], [vq, "--strict"],
                     [vq, "--strict", str(target / "nope.md")], [vq, "--strict", str(stray)],
                     [cc, "--root", str(target), str(target / "10_paper/draft.md")],
                     [cc, "--strict-quotes", "--root", str(stray), str(target / "10_paper/draft.md")]):
            done = subprocess.run([sys.executable, "-B", *map(str, argv)], capture_output=True)
            self.assertEqual(done.returncode, 2, argv)

    def test_f5_handshake_states_run_no_bin(self):
        cases = {}
        cases["unsupported"] = make_stub(self.fresh("c") / "s", version="0.3.0")
        cases["wrong-name"] = make_stub(self.fresh("c") / "s", name="other")
        missing = make_stub(self.fresh("c") / "s")
        (missing / ".claude-plugin" / "plugin.json").unlink()
        cases["no-plugin"] = missing
        for label, companion in cases.items():
            _, done = self.up_to_check(companion=companion, check_expect=3)
            for name in ("quotes", "claims"):
                line = next(l for l in done.stdout.splitlines() if l.startswith(f"{name}:"))
                self.assertIn("UNAVAILABLE:", line, label)
            self.assertEqual(calls(companion), [], label)
        _, done = self.up_to_check(companion=cases["unsupported"], check_expect=3)
        self.assertIn("quotes: UNAVAILABLE: unsupported: version 0.3.0 (tested 0.2.x)", done.stdout)

    def test_f5_symlinked_or_special_bin_is_unavailable_and_not_run(self):
        real = self.stub / "bin" / "verify-quotes"
        moved = self.fresh("m") / "vq"
        real.rename(moved)
        real.symlink_to(moved)
        (self.stub / "bin" / "check-claims").unlink()
        (self.stub / "bin" / "check-claims").mkdir()
        _, done = self.up_to_check(check_expect=3)
        self.assertIn("quotes: UNAVAILABLE: requires bin/verify-quotes", done.stdout)
        self.assertIn("claims: UNAVAILABLE: requires bin/check-claims", done.stdout)
        self.assertEqual([c for c in calls(self.stub) if c["bin"] != "new-project"], [])

    def test_f5_missing_contracts_and_empty_inputs_fail(self):
        for rel, needle in (("00_admin/audit.json", "audit: FAIL: missing: 00_admin/audit.json"),
                            ("00_admin/referee.json", "referee: FAIL: missing: 00_admin/referee.json"),
                            ("replication.json", "replication: FAIL: missing: replication.json"),
                            ("analysis.json", "analysis: FAIL: missing: analysis.json"),
                            ("exhibits.json", "exhibits: FAIL: missing: exhibits.json")):
            _, done = self.up_to_check(plant=lambda p, rel=rel: (p / rel).unlink(), check_expect=1)
            self.assertIn(needle, done.stdout)

        def no_notes(p):
            for n in (p / "01_notes" / "lit").glob("*.md"):
                n.unlink()
        _, done = self.up_to_check(plant=no_notes, check_expect=1)
        self.assertIn("quotes: FAIL: empty: notes", done.stdout)

        def no_cites(p):
            d = p / "10_paper" / "draft.md"
            d.write_text(re.sub(r"\[@[^\]]*\]", "", d.read_text()))
            self.rebind(p)
        _, done = self.up_to_check(plant=no_cites, check_expect=1)
        self.assertIn("claims: FAIL: empty: draft citations", done.stdout)

    def test_f5_skipped_query_and_bad_index_are_unavailable(self):
        target = self.project()
        self.run_cli("init", "--project", target, expect=0)
        done = self.run_cli("check", "--project", target, "--companion", self.stub, expect=3)
        self.assertIn("query: UNAVAILABLE: no query receipt", done.stdout)
        self.run_cli("query", "--project", target, "--index", self.index("partial"), expect=3)
        done = self.run_cli("check", "--project", target, "--companion", self.stub, expect=3)
        self.assertIn("query: UNAVAILABLE: index status is partial", done.stdout)

    def test_f5_fail_beats_incomplete(self):
        def plant(p):
            d = p / "10_paper" / "draft.md"
            d.write_text(d.read_text() + "\nBad: t(28) = 2.10, p = .001\n")
            self.rebind(p)
        target = self.project()
        self.run_cli("init", "--project", target, expect=0)
        shutil.copyfile(MINI / "10_paper" / "draft.md", target / "10_paper" / "draft.md")
        plant(target)
        done = self.run_cli("check", "--project", target, expect=1)
        self.assertIn("overall: FAIL", done.stdout)

    def test_f13_init_pipeline_delegates_once(self):
        target = self.fresh("proj") / "p"
        (target / "03_code").mkdir(parents=True)
        self.run_cli("init", "--project", target, "--companion", self.stub, "--pipeline", "r", expect=0)
        found = calls(self.stub, "new-pipeline")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["argv"], [str(target / "03_code"), "--lang", "r"])
        (self.stub / "mode-new-pipeline").write_text("exit:1")
        other = self.fresh("proj") / "p"
        (other / "03_code").mkdir(parents=True)
        done = self.run_cli("init", "--project", other, "--companion", self.stub, "--pipeline", "r", expect=1)
        self.assertIn("FAIL", done.stdout)
        done = self.run_cli("check", "--project", other, "--companion", self.stub, expect=1)
        self.assertIn("scaffold: FAIL: init: new-pipeline", done.stdout)

    def test_init_without_companion_on_empty_dir_is_unavailable(self):
        target = self.fresh("proj") / "p"
        done = self.run_cli("init", "--project", target, expect=3)
        self.assertIn("scaffold: UNAVAILABLE: no companion given (--companion DIR)", done.stdout)
        self.assertFalse(target.exists())


# ---------------------------------------------------------------- A3, A4

class TestLifecycle(Base):
    def lifecycle(self, companion):
        target = self.baseline(companion)
        out, _ = self.release(target, companion)
        manifest = json.loads((out / "release-manifest.json").read_text())
        self.assertEqual(manifest["status"], "PASS")
        by_file = {s["file"]: s for s in manifest["sources"]}
        for rel, entry in by_file.items():
            self.assertEqual(entry["sha256"], sha(target / rel))
        source = by_file[f"02_sources/md/{NOTE_A}.md"]
        note = source["notes"][0]
        self.assertEqual(note["note"], f"01_notes/lit/{NOTE_A}.md")
        self.assertEqual(len(note["quotes"]), 2)
        self.assertEqual([p["paragraph"] for p in note["draft_paragraphs"]], [2])
        exhibits = {e["id"]: e for e in manifest["exhibits"]}
        self.assertEqual(set(exhibits), {"table1", "fig1"})
        for entry in exhibits.values():
            self.assertEqual(entry["sha256"], sha(target / entry["file"]))
            self.assertEqual(entry["script"], "03_code/run_all.py")
            self.assertEqual(entry["script_sha256"], sha(target / "03_code" / "run_all.py"))
            self.assertEqual(entry["analysis_run"]["output_sha256"], entry["sha256"])
            self.assertEqual(entry["analysis_run"]["exit"], 0)
        folder = target / ".research" / "receipts"
        files = sorted(folder.glob("*.json"))
        self.assertEqual([f.name.split("-", 1)[1] for f in files],
                         ["init.json", "query.json", "check.json", "finalize.json", "release.json"])
        previous = "0" * 64
        for f in files:
            self.assertEqual(json.loads(f.read_text())["prev_sha256"], previous)
            previous = sha(f)
        self.run_cli("verify-receipt", "--project", target, expect=0)
        blob = (out / "release-manifest.json").read_text() + "".join(f.read_text() for f in files)
        self.assertNotIn(HOMEFRAG, blob)
        self.assertNotIn(str(self.tmp), blob)
        return target

    def test_a3s_full_lifecycle_with_stub_reaches_pass(self):
        self.lifecycle(self.stub)

    @unittest.skipUnless(REAL, SKIP_REAL)
    def test_a3_full_lifecycle_real_companion(self):
        self.lifecycle(Path(REAL))

    def test_a4s_stub_fake_quote_fails_check_and_refuses_finalize_and_release(self):
        (self.stub / "fail-notes").write_text(NOTE_A)
        target, done = self.up_to_check(check_expect=1)
        self.assertIn(f"01_notes/lit/{NOTE_A}.md", done.stdout)
        self.assertNotIn(f"{NOTE_B}.md: ", done.stdout.split("quotes:")[1].split("\n")[0])
        self.run_cli("finalize", "--project", target, expect=1)
        out, _ = self.release(target, expect=1)
        self.assertFalse((out / "release-manifest.json").exists())

    def plant_fake_quote(self, target):
        note = target / "01_notes" / "lit" / f"{NOTE_A}.md"
        note.write_text(note.read_text().replace("reach the target cell faster", "always arrive before every other agent"))

    def test_a4_stub_detects_planted_fake_quote(self):
        _, done = self.up_to_check(plant=self.plant_fake_quote, check_expect=1)
        self.assertIn(f"01_notes/lit/{NOTE_A}.md", done.stdout)

    @unittest.skipUnless(REAL, SKIP_REAL)
    def test_a4_real_companion_planted_fake_quote(self):
        target, done = self.up_to_check(Path(REAL), plant=self.plant_fake_quote, check_expect=1)
        self.assertIn(f"01_notes/lit/{NOTE_A}.md", done.stdout)
        self.release(target, Path(REAL), expect=1)

    @unittest.skipUnless(REAL, SKIP_REAL)
    def test_a5_real_strict_fails_edited_source_before_finalize(self):
        def plant(p):
            s = p / "02_sources" / "md" / f"{NOTE_A}.md"
            s.write_text(s.read_text() + "\nAn added sentence.\n")
        _, done = self.up_to_check(Path(REAL), plant=plant, check_expect=1)
        self.assertIn("quotes: FAIL", done.stdout)

    def test_init_new_project_delegation_and_distinct_project_ids(self):
        ids = []
        for _ in range(2):
            target = self.fresh("proj") / "p"
            self.run_cli("init", "--project", target, "--companion", self.stub, expect=0)
            ids.append(json.loads(next((target / ".research" / "receipts").glob("001-init.json")).read_text())["project_id"])
        self.assertEqual(len(calls(self.stub, "new-project")), 2)
        self.assertNotEqual(*ids)
        self.run_cli("init", "--project", target, "--companion", self.stub, expect=2)


# ---------------------------------------------------------------- A5, F6, A7, A10

class TestStaleAndLineage(Base):
    def edit_source(self, p):
        s = p / "02_sources" / "md" / f"{NOTE_A}.md"
        s.write_text(s.read_text() + "\nAn added sentence.\n")

    def scenario_a5(self, cli):
        target = self.baseline(cli=cli)
        self.edit_source(target)
        done = self.run_cli("release", "--project", target, "--out", self.fresh("o") / "x", cli=cli, expect=1)
        self.assertIn(f"stale: 02_sources/md/{NOTE_A}.md", done.stdout)

    def test_a5_changed_source_hash_after_finalize(self):
        self.scenario_a5(CLI)

    def test_a5_mutation_release_trusts_finalize(self):
        mutant = self.mutant('    raise_stale(project, finalize["payload"]["bound"])\n    manifest',
                             '    manifest')
        with self.assertRaises(AssertionError):
            self.scenario_a5(mutant)

    def scenario_f6(self, cli):
        edits = {
            "source": self.edit_source,
            "note": lambda p: (p / "01_notes" / "lit" / f"{NOTE_B}.md").write_text(
                (p / "01_notes" / "lit" / f"{NOTE_B}.md").read_text() + "\nAdded.\n"),
            "draft": lambda p: (p / "10_paper" / "draft.md").write_text(
                (p / "10_paper" / "draft.md").read_text() + "\nAdded.\n"),
        }
        for label, edit in edits.items():
            target, _ = self.up_to_check(cli=cli)
            edit(target)
            done = self.run_cli("finalize", "--project", target, cli=cli, expect=1)
            self.assertIn("stale: ", done.stdout, label)

    def test_f6_finalize_rechecks_bound_files(self):
        self.scenario_f6(CLI)

    def test_f6_mutation_finalize_binds_current_hashes(self):
        mutant = self.mutant('    raise_stale(project, check["payload"]["bound"])\n    append_receipt(project, "finalize"',
                             '    append_receipt(project, "finalize"')
        with self.assertRaises(AssertionError):
            self.scenario_f6(mutant)

    def check_after(self, plant, rebind=True):
        def go(p):
            plant(p)
            if rebind:
                self.rebind(p)
        _, done = self.up_to_check(plant=go, check_expect=1)
        return done.stdout

    def scenario_a7(self, cli):
        out = self.check_after(lambda p: (p / "06_tables" / "table1.csv").write_text("n,slope\n6,9.9999\n"))
        self.assertIn("sha mismatch: table1", out)

    def test_a7_exhibit_lineage_failures_name_the_id(self):
        def ghost(p):
            d = p / "10_paper" / "draft.md"
            d.write_text(d.read_text() + "\nSee {{exhibit:ghost}}.\n")
        self.assertIn("unknown exhibit: ghost", self.check_after(ghost))
        self.scenario_a7(CLI)

        def move_script(rel):
            def plant(p):
                data = json.loads((p / "exhibits.json").read_text())
                data["exhibits"][0]["produced_by"] = rel
                (p / "exhibits.json").write_text(json.dumps(data))
            return plant
        self.assertIn("unproduced: table1", self.check_after(move_script("03_code/nope.py")))
        self.assertIn("unproduced: table1", self.check_after(move_script("data/input.csv")))

    def test_a7_mutation_skip_sha_compare(self):
        mutant_src = self.mutant('        if actual != item["sha256"]:', '        if False:')
        # the mutated module is exercised through the same scenario by pointing the CLI at it
        target, _ = self.up_to_check(cli=mutant_src)
        (target / "06_tables" / "table1.csv").write_text("n,slope\n6,9.9999\n")
        self.rebind(target)
        done = self.run_cli("check", "--project", target, "--companion", self.stub, cli=mutant_src)
        self.assertNotIn("sha mismatch: table1", done.stdout)
        done = self.run_cli("check", "--project", target, "--companion", self.stub)
        self.assertIn("sha mismatch: table1", done.stdout)

    def tamper(self, target, name, change):
        path = next((target / ".research" / "receipts").glob(f"*-{name}.json"))
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return path

    def scenario_a10(self, cli):
        target = self.baseline(cli=cli)
        self.tamper(target, "check", lambda d: d["payload"]["checks"]["quotes"].update(detail="edited"))
        self.run_cli("release", "--project", target, "--out", self.fresh("o") / "x", cli=cli, expect=1)

    def test_a10_receipt_integrity(self):
        self.scenario_a10(CLI)
        target = self.baseline()
        self.tamper(target, "check", lambda d: d.update(status="PASS", payload=d["payload"]))
        self.run_cli("verify-receipt", "--project", target, expect=0)
        path = self.tamper(target, "finalize", lambda d: d["payload"].update(check_seq=1))
        done = self.run_cli("verify-receipt", "--project", target, expect=1)
        self.assertIn("receipt edited", done.stdout)
        target = self.baseline()
        next((target / ".research" / "receipts").glob("002-*.json")).unlink()
        done = self.run_cli("verify-receipt", "--project", target, expect=1)
        self.assertIn("missing receipt: 002", done.stdout)
        a, b = self.baseline(), self.baseline()
        shutil.copyfile(next((b / ".research" / "receipts").glob("003-*.json")),
                        next((a / ".research" / "receipts").glob("003-*.json")))
        done = self.run_cli("verify-receipt", "--project", a, expect=1)
        self.assertIn("receipt from another project", done.stdout)
        self.run_cli("release", "--project", a, "--out", self.fresh("o") / "x", expect=1)

    def test_a10_mutation_release_reads_only_last_status(self):
        mutant = self.mutant('    chain = chain_or_exit(project)\n    last = chain[-1]',
                             '    chain = load_chain(project)[0]\n    last = chain[-1]',
                             also=(('records, problems = load_chain(project) if step != "init" else ([], [])',
                                    'records, problems = load_chain(project)[0], []'),))
        with self.assertRaises(AssertionError):
            self.scenario_a10(mutant)

    def test_a10_release_rechecks_chain_and_finalize_refuses_fail(self):
        target, _ = self.up_to_check(check_expect=0)
        self.run_cli("finalize", "--project", target, expect=0)
        self.run_cli("finalize", "--project", target, expect=1)


# ---------------------------------------------------------------- A6

class TestStats(Base):
    def setUp(self):
        super().setUp()
        self.stats = load_module("stats_under_test", MOD / "stats.py")

    def verdicts(self, text):
        return [(i["status"], i["detail"]) for i in self.stats.check_text(text)]

    def test_a6_reference_vectors_pass(self):
        for text in ("t(10) = 2.228, p = .05", "chi2(1) = 3.841, p = .05", "F(1, 10) = 4.965, p = .05",
                     "z = 1.96, p = .05", "t(1) = 12.706, p = .05", "t(120) = 1.980, p = .05",
                     "F(3, 40) = 2.839, p = .05", "chi2(10) = 18.307, p = .05", "z = 2.576, p = .01",
                     "t(10) = 4.587, p = .001", "χ²(1) = 3.84, p = .05", "t(28) = −2.10, p = .045",
                     "t(28) = 2.10, p < .05", "t(10) = 2.228, p = 0.05"):
            self.assertEqual([s for s, _ in self.verdicts(text)], ["PASS"], text)

    def test_a6_invalid_report_and_grammar(self):
        status, detail = self.verdicts("t(28) = 2.10, p = .001")[0]
        self.assertEqual(status, "FAIL")
        self.assertIn("recomputed p = .0449", detail)
        self.assertEqual(self.verdicts("t(28) = 2.10, p < .001")[0][0], "FAIL")
        for text in ("t(28) = 2.10, p = ", "r(30) = .41, p = .02", "t = 2.10, p = .04", "p < .05 alone"):
            self.assertEqual([s for s, _ in self.verdicts(text)], ["unsupported-grammar"], text)

    def test_a6_unsupported_grammar_makes_check_incomplete_not_pass(self):
        target = self.project()
        self.run_cli("init", "--project", target, expect=0)
        d = target / "10_paper" / "draft.md"
        d.write_text(d.read_text() + "\nr(30) = .41, p = .02\n")
        self.rebind(target)
        done = self.run_cli("check", "--project", target, expect=3)
        self.assertIn("stats: UNAVAILABLE: unsupported-grammar: r(30)", done.stdout)

    def test_a6_mutation_unbounded_tolerance(self):
        path = self.mutant("half = 0.5 * 10.0 ** -decimals(stat_text.lstrip(\"+-\"))", "half = 5.0", "stats.py")
        mutated = load_module("stats_mutant", path)
        self.assertEqual(mutated.check_text("t(28) = 2.10, p = .001")[0]["status"], "PASS")
        self.assertEqual(self.stats.check_text("t(28) = 2.10, p = .001")[0]["status"], "FAIL")


# ---------------------------------------------------------------- A8, A9

class TestRunnerAndContracts(Base):
    def test_a8_analysis_runner_records_exit_hashes_and_wall(self):
        target, _ = self.up_to_check()
        receipt = json.loads(next((target / ".research" / "receipts").glob("003-check.json")).read_text())
        run = receipt["payload"]["analysis_run"]
        self.assertEqual(run["exit"], 0)
        self.assertIsInstance(run["wall_s"], float)
        self.assertEqual(run["outputs"]["06_tables/table1.csv"], sha(target / "06_tables" / "table1.csv"))
        self.assertEqual(run["entry"][0], "python3")

    def test_a8_nonzero_exit_fails(self):
        def plant(p):
            (p / "03_code" / "run_all.py").write_text("import sys\nsys.exit(3)\n")
        _, done = self.up_to_check(plant=plant, check_expect=1)
        self.assertIn("analysis: FAIL: exit 3", done.stdout)

    def test_a8_timeout_kills_child_and_group(self):
        pidfile = self.fresh("pid") / "pid.txt"

        def plant(p):
            (p / "03_code" / "run_all.py").write_text(
                "import subprocess, sys, time\n"
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                "time.sleep(120)\n")
            data = json.loads((p / "analysis.json").read_text())
            data["timeout_s"] = 1
            (p / "analysis.json").write_text(json.dumps(data))
        _, done = self.up_to_check(plant=plant, check_expect=1)
        self.assertIn("analysis: FAIL: timeout after 1 s", done.stdout)
        pid = int(pidfile.read_text())
        deadline = __import__("time").monotonic() + 10
        while alive(pid) and __import__("time").monotonic() < deadline:
            pass
        self.assertFalse(alive(pid))

    def test_a8_rscript_absent_is_unavailable(self):
        def plant(p):
            data = json.loads((p / "analysis.json").read_text())
            data["entry"] = ["Rscript", "03_code/run_all.R"]
            (p / "analysis.json").write_text(json.dumps(data))
        empty = self.fresh("path")
        target = self.fresh("proj") / "p"
        shutil.copytree(MINI, target)
        plant(target)
        self.run_cli("init", "--project", target, expect=0)
        self.run_cli("query", "--project", target, "--index", self.index(), expect=0)
        done = self.run_cli("check", "--project", target, "--companion", self.stub, env={"PATH": str(empty)}, expect=3)
        self.assertIn("analysis: UNAVAILABLE: Rscript not found on PATH", done.stdout)

    def test_a9_audit_finding_needs_disposition_and_evidence(self):
        def plant(p):
            data = json.loads((p / "00_admin" / "audit.json").read_text())
            data["findings"].append({"id": "F2", "disposition": "ignored", "evidence": "00_admin/audit-evidence.md",
                                     "evidence_sha256": sha(p / "00_admin" / "audit-evidence.md")})
            (p / "00_admin" / "audit.json").write_text(json.dumps(data))
        _, done = self.up_to_check(plant=plant, check_expect=1)
        self.assertIn("audit: FAIL: finding F2: needs a fixed|refuted|accepted-risk disposition and evidence", done.stdout)

        def bad_evidence(p):
            data = json.loads((p / "00_admin" / "audit.json").read_text())
            data["findings"][0]["evidence_sha256"] = "0" * 64
            (p / "00_admin" / "audit.json").write_text(json.dumps(data))
        _, done = self.up_to_check(plant=bad_evidence, check_expect=1)
        self.assertIn("finding F1: evidence missing or its sha256 differs", done.stdout)

    def test_f7_reviews_are_bound_to_what_they_reviewed(self):
        def plant(p):
            d = p / "10_paper" / "draft.md"
            d.write_text(d.read_text() + "\nAfter the review.\n")
        _, done = self.up_to_check(plant=plant, check_expect=1)
        self.assertIn("audit: FAIL: stale-review: 10_paper/draft.md", done.stdout)
        self.assertIn("referee: FAIL: stale-review: 10_paper/draft.md", done.stdout)

    def test_a9_referee_not_run_is_unavailable_and_release_refused(self):
        def plant(p):
            data = json.loads((p / "00_admin" / "referee.json").read_text())
            data["status"] = "NOT RUN"
            (p / "00_admin" / "referee.json").write_text(json.dumps(data))
        target, done = self.up_to_check(plant=plant, check_expect=3)
        self.assertIn("referee: UNAVAILABLE: referee NOT RUN", done.stdout)
        self.run_cli("finalize", "--project", target, expect=3)
        out, _ = self.release(target, expect=3)
        self.assertFalse((out / "release-manifest.json").exists())

    def test_a9_replicate_matches_and_leaves_tree_unchanged(self):
        target = self.baseline()
        before = tree_hash(target)
        done = self.run_cli("replicate", "--project", target, expect=0)
        self.assertIn("replicate: PASS", done.stdout)
        self.assertEqual(tree_hash(target), before)

    def test_a9_time_dependent_output_fails_replication(self):
        def plant(p):
            (p / "03_code" / "run_all.py").write_text(
                "import time\nfrom pathlib import Path\nPath('06_tables').mkdir(exist_ok=True)\n"
                "Path('05_figures').mkdir(exist_ok=True)\n"
                "Path('06_tables/table1.csv').write_text(str(time.time_ns()))\n"
                "Path('05_figures/fig1.txt').write_text('x')\n")
        target = self.fresh("proj") / "p"
        shutil.copytree(MINI, target)
        plant(target)
        self.run_cli("init", "--project", target, expect=0)
        before = tree_hash(target, skip_research=True)
        done = self.run_cli("replicate", "--project", target, expect=1)
        self.assertIn("output differs 06_tables/table1.csv", done.stdout)
        self.assertEqual(tree_hash(target, skip_research=True), before)


# ---------------------------------------------------------------- tribune F1/F2: lineage and replication

NOOP = "pass\n"
RANDOM_SCRIPT = (
    "import random\nfrom pathlib import Path\nPath('06_tables').mkdir(exist_ok=True)\n"
    "Path('05_figures').mkdir(exist_ok=True)\n"
    "Path('06_tables/table1.csv').write_text(str(random.random()))\n"
    "Path('05_figures/fig1.txt').write_text('x')\n")


def write_script(text):
    return lambda p: (p / "03_code" / "run_all.py").write_text(text)


def edit_json(rel, change):
    def plant(p):
        data = json.loads((p / rel).read_text())
        change(data)
        (p / rel).write_text(json.dumps(data))
    return plant


class TestLineageAndReplication(Base):
    def companions(self):
        return [self.stub] + ([Path(REAL)] if REAL else [])

    def scenario_noop_script(self, cli):
        for companion in self.companions():
            target, done = self.up_to_check(companion, cli, plant=write_script(NOOP), check_expect=1)
            self.assertIn("analysis: FAIL: missing output 06_tables/table1.csv", done.stdout)
            self.assertIn("overall: FAIL", done.stdout)
            out, _ = self.release(target, companion, cli=cli, expect=1)
            self.assertFalse((out / "release-manifest.json").exists())

    def test_f1_noop_script_fails_lineage_and_release_is_refused(self):
        self.scenario_noop_script(CLI)

    def test_f1_mutation_skip_pre_run_removal(self):
        mutant = self.mutant("                gone.unlink()", "                pass")
        with self.assertRaises(AssertionError):
            self.scenario_noop_script(mutant)

    def test_f1_script_writing_a_different_table_fails(self):
        script = (
            "from pathlib import Path\nPath('06_tables').mkdir(exist_ok=True)\nPath('05_figures').mkdir(exist_ok=True)\n"
            "Path('06_tables/table1.csv').write_text('n,slope\\n1,9.9999\\n')\n"
            "Path('05_figures/fig1.txt').write_text(Path('05_figures/fig1.txt').read_text() "
            "if Path('05_figures/fig1.txt').exists() else 'x')\n")
        for companion in self.companions():
            _, done = self.up_to_check(companion, plant=write_script(script), check_expect=1)
            self.assertIn("analysis: FAIL: output differs 06_tables/table1.csv", done.stdout)

    def test_f1_emptied_input_with_a_script_that_reads_it_fails(self):
        def plant(p):
            (p / "data" / "input.csv").write_text("x,y\n")
        for companion in self.companions():
            _, done = self.up_to_check(companion, plant=plant, check_expect=1)
            self.assertIn("analysis: FAIL: exit 1", done.stdout)

    def test_f1_emptied_input_with_noop_script_fails(self):
        def plant(p):
            (p / "data" / "input.csv").write_text("x,y\n")
            write_script(NOOP)(p)
        for companion in self.companions():
            _, done = self.up_to_check(companion, plant=plant, check_expect=1)
            self.assertIn("analysis: FAIL: missing output", done.stdout)

    def test_f1_manifest_output_hash_is_the_run_hash(self):
        target = self.baseline()
        out, _ = self.release(target)
        manifest = json.loads((out / "release-manifest.json").read_text())
        run_hash = {e["file"]: e["analysis_run"]["output_sha256"] for e in manifest["exhibits"]}
        self.assertEqual(run_hash["06_tables/table1.csv"], sha(target / "06_tables" / "table1.csv"))

    def scenario_replication_contract(self, cli):
        cases = [
            (edit_json("replication.json", lambda d: d.update(entry=["python3", "03_code/noop.py"])), "entry"),
            (edit_json("replication.json", lambda d: d.update(outputs=["06_tables/table1.csv"])), "outputs"),
        ]
        for plant, key in cases:
            for companion in self.companions():
                _, done = self.up_to_check(companion, cli, plant=plant, check_expect=1)
                self.assertIn(f"replication: FAIL: replication.json: {key} differs from analysis.json", done.stdout)

    def test_f2_replication_may_not_change_entry_or_outputs(self):
        self.scenario_replication_contract(CLI)

    def test_f2_mutation_compare_only_replication_listed_outputs(self):
        mutant = self.mutant("if key in data and cfg[key] != base[key]:", "if False:")
        with self.assertRaises(AssertionError):
            self.scenario_replication_contract(mutant)

    def test_f2_nondeterministic_script_fails_replication(self):
        for companion in self.companions():
            _, done = self.up_to_check(companion, plant=write_script(RANDOM_SCRIPT), check_expect=1)
            self.assertIn("replication: FAIL: output differs 06_tables/table1.csv", done.stdout)

    def test_f2_replicate_command_rejects_noop_entry(self):
        target = self.project()
        edit_json("replication.json", lambda d: d.update(entry=["python3", "03_code/noop.py"]))(target)
        self.run_cli("init", "--project", target, expect=0)
        done = self.run_cli("replicate", "--project", target, expect=1)
        self.assertIn("entry differs from analysis.json", done.stdout)

    def test_f2_runs_and_seed_env_are_the_only_additions(self):
        target = self.project()
        edit_json("replication.json", lambda d: d.update(runs=2, env={"PYTHONHASHSEED": "0"}))(target)
        self.run_cli("init", "--project", target, expect=0)
        self.run_cli("replicate", "--project", target, expect=0)
        for bad in ({"env": {"PATH": "/tmp"}}, {"runs": 0}, {"extra": 1}):
            target = self.project()
            edit_json("replication.json", lambda d, bad=bad: d.update(bad))(target)
            self.run_cli("init", "--project", target, expect=0)
            self.run_cli("replicate", "--project", target, expect=1)


# ---------------------------------------------------------------- A11, F12

class TestConfinement(Base):
    def init_copy(self):
        target = self.project()
        self.run_cli("init", "--project", target, expect=0)
        return target

    def test_a11_symlinks_are_refused(self):
        target = self.init_copy()
        elsewhere = self.fresh("else")
        shutil.copytree(target / "02_sources", elsewhere / "s")
        shutil.rmtree(target / "02_sources")
        (target / "02_sources").symlink_to(elsewhere / "s")
        self.run_cli("check", "--project", target, "--companion", self.stub, expect=2)
        target = self.init_copy()
        note = target / "01_notes" / "lit" / f"{NOTE_A}.md"
        moved = elsewhere / "note.md"
        shutil.move(note, moved)
        note.symlink_to(moved)
        self.run_cli("check", "--project", target, "--companion", self.stub, expect=2)

    def test_a11_escaping_paths_and_duplicate_keys_exit_two(self):
        def edit(rel, change):
            target = self.init_copy()
            data = json.loads((target / rel).read_text())
            change(data)
            (target / rel).write_text(json.dumps(data))
            self.run_cli("check", "--project", target, "--companion", self.stub, expect=2)
        edit("exhibits.json", lambda d: d["exhibits"][0].update(file="../outside.csv"))
        edit("exhibits.json", lambda d: d["exhibits"][0].update(produced_by="/etc/passwd"))
        edit("analysis.json", lambda d: d.update(outputs=["/abs/out.csv"]))
        edit("analysis.json", lambda d: d.update(entry=["python3", "../run.py"]))
        edit("replication.json", lambda d: d.update(cwd=".."))
        edit("00_admin/audit.json", lambda d: d["reviewed"].update({"../x": "0" * 64}))
        target = self.init_copy()
        (target / "analysis.json").write_text('{"entry": ["python3", "03_code/run_all.py"], "entry": [], '
                                              '"timeout_s": 5, "outputs": ["06_tables/table1.csv"]}')
        done = self.run_cli("check", "--project", target, "--companion", self.stub, expect=2)
        self.assertIn("duplicate JSON key", done.stderr)

    def test_a11_nothing_written_outside_research_and_out(self):
        target = self.baseline()
        before = tree_hash(target, skip_research=True)
        parent = sorted(p.name for p in target.parent.iterdir())
        self.run_cli("check", "--project", target, "--companion", self.stub, expect=0)
        self.run_cli("replicate", "--project", target, expect=0)
        self.assertEqual(tree_hash(target, skip_research=True), before)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), parent)

    def test_a11_audit_hook_sees_no_socket_use_and_catches_a_planted_one(self):
        events = []
        armed = [False]

        def hook(event, args):
            if armed[0] and event.startswith("socket."):
                events.append(event)
        sys.addaudithook(hook)
        module = load_module("integrity_in_process", CLI)
        target = self.project()
        armed[0] = True
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["init", "--project", str(target)]), 0)
                self.assertEqual(module.main(["check", "--project", str(target), "--companion", str(self.stub)]), 3)
            self.assertEqual(events, [])
            probe = socket.socket()
            with contextlib.suppress(OSError):
                probe.settimeout(0.2)
                probe.connect(("127.0.0.1", 9))
            probe.close()
            self.assertIn("socket.connect", events)
        finally:
            armed[0] = False

    def test_f12_out_dir_rules(self):
        target = self.baseline()
        full = self.fresh("o")
        (full / "keep").write_text("x")
        self.run_cli("release", "--project", target, "--out", full, expect=2)
        link = self.fresh("l") / "link"
        link.symlink_to(self.fresh("t"))
        self.run_cli("release", "--project", target, "--out", link, expect=2)
        self.run_cli("release", "--project", target, "--out", target / "inside", expect=2)
        self.assertFalse((full / "release-manifest.json").exists())


# ---------------------------------------------------------------- A12, A13, F14

class TestExampleAndFence(Base):
    def run_verify(self, *args, env=None):
        full = dict(os.environ)
        full.pop("AIARCH_REQUIRE_COMPANION", None)
        full["PYTHONDONTWRITEBYTECODE"] = "1"
        if env:
            full.update(env)
        return subprocess.run([sys.executable, "-B", str(VERIFY), *map(str, args)], capture_output=True,
                              text=True, env=full)

    def test_a12_f14_bare_run_exits_zero_and_says_companion_not_run(self):
        done = self.run_verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("companion: NOT RUN (", done.stdout)

    def test_a12_require_companion_without_one_exits_one(self):
        done = self.run_verify(env={"AIARCH_REQUIRE_COMPANION": "1"})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_a12_tampered_receipt_exits_one(self):
        target = self.baseline()
        receipt = next((target / ".research" / "receipts").glob("002-query.json"))
        self.assertEqual(self.run_verify("--receipt", receipt).returncode, 0)
        data = json.loads(receipt.read_text())
        data["payload"]["index_status"] = "edited"
        receipt.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        self.assertEqual(self.run_verify("--receipt", receipt).returncode, 1)

    def test_a12_with_companion_reaches_release_pass_and_shows_a4(self):
        done = self.run_verify("--companion", self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("release: PASS", done.stdout)
        self.assertIn("A4: planted fake quote refused", done.stdout)

    @unittest.skipUnless(REAL, SKIP_REAL)
    def test_a12_with_real_companion(self):
        done = self.run_verify("--companion", REAL)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("release: PASS", done.stdout)

    def test_a13_descriptor_and_contract_adapter(self):
        contracts = load_module("contracts_u18", ROOT / "modules/contracts/contracts.py")
        descriptor = contracts.check_module_descriptor(MOD, module="research-integrity")
        self.assertEqual(descriptor["claimed_support"], "planned")
        adapter = load_module("integrity_adapter", CLI).ADAPTER
        out = contracts.execute_adapter(adapter, "describe", 1, descriptor["dependencies"])
        self.assertEqual(out["module"], "research-integrity")

    def new_files(self):
        found = [p for p in MOD.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        found += [p for p in MINI.rglob("*") if p.is_file()]
        found.append(VERIFY)
        return found

    def test_a13_f11_privacy_scan_and_named_files(self):
        files = {p.relative_to(ROOT).as_posix() for p in self.new_files()}
        for named in ("modules/research-integrity/integrity.py", "modules/research-integrity/stats.py",
                      "modules/research-integrity/descriptor.json", "modules/research-integrity/README.md",
                      "modules/research-integrity/INTEGRATION.md",
                      "examples/research-companion/verify_receipt.py",
                      "examples/research-companion/mini_study/10_paper/draft.md",
                      "examples/research-companion/mini_study/02_sources/manifest.csv"):
            self.assertIn(named, files)
        privacy = load_module("privacy_u18", ROOT / "scripts/privacy_scan.py")
        self.assertEqual(privacy.scan(ROOT / "modules" / "research-integrity"), [])
        self.assertEqual(privacy.scan(ROOT / "examples" / "research-companion"), [])
        names = {getpass.getuser(), socket.gethostname()}
        for path in self.new_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(HOMEFRAG, text, path)
            for name in names:
                if len(name) > 4:
                    self.assertNotIn(name, text, path)

    @unittest.skipUnless(REAL, SKIP_REAL)
    def test_a13_no_new_file_is_byte_identical_to_a_companion_file(self):
        companion = {sha(p) for p in Path(REAL).rglob("*") if p.is_file()}
        for path in self.new_files():
            self.assertNotIn(sha(path), companion, path)

    def test_a13_diff_names_only_fence_paths(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        branch = subprocess.run([git, "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
        if branch.stdout.strip() != "r9-u18":
            self.skipTest("fence diff is checked on the r9-u18 branch only")
        names = subprocess.run([git, "-C", str(ROOT), "diff", "--name-only", "fa042a0a5b5e4e6ce56e710c2cff83591d5f4606"],
                               capture_output=True, text=True).stdout.split()
        names += subprocess.run([git, "-C", str(ROOT), "ls-files", "--others", "--exclude-standard"],
                                capture_output=True, text=True).stdout.split()
        allowed = ("modules/research-integrity/", "examples/research-companion/mini_study/")
        extra = ("examples/research-companion/verify_receipt.py", "examples/research-companion/README.md")
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith(allowed) or name in extra, name)

    def test_a13_u17_demo_still_runs(self):
        done = subprocess.run([sys.executable, "-B", str(ROOT / "examples/research-companion/run.py"), "--offline"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_f10_readme_states_limits(self):
        text = (MOD / "README.md").read_text()
        self.assertIn("tamper-evident only against partial edits", text)
        self.assertIn("recorded attestations", text)


if __name__ == "__main__":
    unittest.main()
