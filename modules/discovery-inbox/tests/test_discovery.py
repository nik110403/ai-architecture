"""R9-U24 discovery-inbox: candidate, handoff and disposition roundtrip against an authored fake companion (A1-A13).

Temp dirs only, agent env stripped, sentinel fakes for gh/curl/wget/claude/opencode, in-process sockets patched.
The real-companion cell (A11) runs only when AIARCH_DISCOVERY_MANIFEST names a host manifest outside the tree.
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
from unittest import mock

MOD = Path(__file__).resolve().parents[1]
ROOT = MOD.parents[1]
EXAMPLE = ROOT / "examples" / "companion-handoff"
FAKE_SRC = MOD / "tests" / "fixtures" / "fake_companion"
ORIG_PATH = os.environ.get("PATH", "/usr/bin:/bin")
GIT = shutil.which("git", path=ORIG_PATH)
GRAPHIFY = shutil.which("graphify", path=ORIG_PATH)
HOST_MANIFEST = os.environ.get("AIARCH_DISCOVERY_MANIFEST")
REQUIRE = os.environ.get("AIARCH_REQUIRE_COMPANION") == "1"
CHILD_STRIP = re.compile(r"^(AI_ARCH_JOB_MARKER|CLAUDECODE|CLAUDE_CODE_.*|OPENCODE.*|.*_API_KEY|ANTHROPIC_.*|OPENAI_.*|GH_.*|GITHUB_.*"
                         r"|HTTPS?_PROXY|ALL_PROXY|NO_PROXY|PYTHONPATH|AIARCH_.*)$", re.I)
SENTINELS = ("gh", "curl", "wget", "claude", "opencode")
REGISTRY_SUPPORT = {c["id"]: c["support"] for c in json.loads((ROOT / "capabilities" / "registry.json").read_text())["capabilities"]}
GRAPH_ARGS = ["--graphify", GRAPHIFY] if GRAPHIFY else []


def load(key, path, *, scripts=False):
    if scripts:
        sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if scripts:
            sys.path.remove(str(ROOT / "scripts"))


discovery = load("discovery_under_test", MOD / "discovery.py")
companion = sys.modules["di_companion"]
contracts = load("contracts_for_di_tests", ROOT / "modules" / "contracts" / "contracts.py")
check_collection = load("check_collection_for_di_tests", ROOT / "scripts" / "check_collection.py", scripts=True)
privacy = load("privacy_for_di_tests", ROOT / "scripts" / "privacy_scan.py")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(cwd, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run([GIT, "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return done.stdout


class World:
    """One disposable fake companion repo, one project, one registry, one sandbox, one out dir."""

    def __init__(self, base, name, mutate=None, manifest=None):
        self.dir = Path(os.path.realpath(base)) / name
        self.dir.mkdir()
        self.targets = self.dir / "targets"
        self.project = self.targets / "demo-project"
        self.project.mkdir(parents=True)
        (self.project / "AGENTS.md").write_text("# Demo project\n")
        self.registry = self.dir / "registry.json"
        self.out = self.dir / "out"
        self.sandbox = self.dir / "sandbox"
        if manifest:
            self.root = Path(json.loads(Path(manifest).read_text())["root"])
            self.manifest = Path(manifest)
            return
        self.root = self.dir / "fake"
        shutil.copytree(FAKE_SRC, self.root)
        if mutate:
            mutate(self.root)
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "fixture")
        self.manifest = self.write_manifest()

    def manifest_data(self):
        return {"schema_version": 1, "interface": "handoff-brief/1", "name": "fake-discovery", "root": str(self.root), "rev": "HEAD",
                "commands": {"handoff": "bin/fake-handoff", "set": "bin/fake-set", "list": "bin/fake-list"},
                "support": ["bin/fake_lib.py"],
                "dispositions": {"adopted": "integrated", "harvested": "harvested", "deferred": "deferred", "rejected": "skip"},
                "eligible_status": "next"}

    def write_manifest(self, data=None, raw=None, name="manifest.json"):
        path = self.dir / name
        path.write_text(raw if raw is not None else json.dumps(data or self.manifest_data()))
        return path

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = discovery.main([str(a) for a in argv])
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 2
        return rc, out.getvalue(), err.getvalue()

    def std(self, command, *extra, project=True, targets=None):
        flags = ["--out", self.out, "--companion", self.manifest]
        if command != "companions":
            flags += ["--sandbox", self.sandbox]
        if project:
            flags += ["--registry", self.registry, "--project", self.project, "--targets-root", targets or self.targets]
        return self.call(*command.split(), *flags, *extra)

    def js(self, command, *extra, rc=0, **kw):
        code, out, err = self.std(command, *extra, **kw)
        assert code == rc, (command, code, out, err)
        return json.loads(out) if out.strip() else None

    def roundtrip(self, mode="pack", *extra):
        return self.std("roundtrip", "--topic", "main", "--mode", mode, "--backend", "fixture", *extra)

    def receipts(self, event="roundtrip"):
        path = self.out / "discovery.jsonl"
        records = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [r for r in records if r["event"] == event]

    def questions(self):
        return self.project / "plans" / "main" / "QUESTIONS.md"

    def bootstrap(self):
        subprocess.run([sys.executable, "-B", str(ROOT / "modules" / "stewardship" / "steward.py"), "bootstrap", "--project",
                        str(self.project), "--registry", str(self.registry), "--topic", "main"], check=True, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(self.dir)})

    def pack_bytes(self):
        return {n: (self.project / "plans" / "main" / n).read_bytes() for n in ("TRACKER.md", "SPEC.md", "STRATEGY.md")}

    @staticmethod
    def all_text(base):
        return "".join(p.read_text(errors="replace") for p in Path(base).rglob("*") if p.is_file())


class DiCase(unittest.TestCase):
    world_no = 0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(cls.tmp.name))
        cls.base = base
        for name in ("home", "xdg-state", "xdg-config", "tmpdir", "bin"):
            (base / name).mkdir()
        cls.sentinel_log = base / "sentinel.log"
        for name in SENTINELS:
            path = base / "bin" / name
            path.write_text(f"#!/bin/sh\necho {name} >> {cls.sentinel_log}\n")
            path.chmod(0o755)
        cls.env_patch = mock.patch.dict(os.environ, {"HOME": str(base / "home"), "XDG_STATE_HOME": str(base / "xdg-state"),
                                                     "XDG_CONFIG_HOME": str(base / "xdg-config"), "TMPDIR": str(base / "tmpdir"),
                                                     "PATH": f"{base / 'bin'}:/usr/bin:/bin", "LANG": "C.UTF-8"})
        cls.env_patch.start()
        for key in [k for k in os.environ if CHILD_STRIP.match(k)]:
            os.environ.pop(key)
        os.environ["OPENAI_API_KEY"], os.environ["ANTHROPIC_BASE_URL"] = "planted", "http://planted.invalid"

        def deny(*a, **k):
            raise AssertionError("network used")

        cls.patches = [mock.patch("socket.socket.connect", side_effect=deny), mock.patch("socket.create_connection", side_effect=deny)]
        for patch in cls.patches:
            patch.start()
        cls.cwd = os.getcwd()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        cls.env_patch.stop()
        os.chdir(cls.cwd)
        sentinel = cls.sentinel_log.exists()
        cls.tmp.cleanup()
        if sentinel:
            raise AssertionError("a sentinel fake (gh/curl/wget/claude/opencode) was run")

    def world(self, **kw):
        type(self).world_no += 1
        return World(self.base, f"w{type(self).world_no}", **kw)

    @staticmethod
    def spawns():
        return companion.SPAWNS


class Baseline(DiCase):
    """One baseline pack-mode roundtrip, shared by the cells that only read it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.w = World(cls.base, "baseline")
            cls.w.bootstrap()
            cls.rc, cls.out, cls.err = cls.w.roundtrip("pack", "--disposition", "deferred")
        except BaseException:
            super().tearDownClass()
            raise


class A1Journey(Baseline):
    def test_roundtrip_places_candidate_and_returns_disposition(self):
        self.assertEqual(self.rc, 0, (self.out, self.err))
        result = json.loads(self.out)
        self.assertEqual((result["mode"], result["disposition"], result["companion_ack"], result["backend"]),
                         ("pack", "deferred", "confirmed", "fixture"))
        self.assertIn("## Handoff: example/widget", self.w.questions().read_text())
        receipt = self.w.receipts()[-1]
        for key in ("candidate_id", "brief_sha256", "mode", "placement", "disposition", "companion_value", "companion_ack",
                    "identity", "isolation", "backend"):
            self.assertIn(key, receipt)
        self.assertEqual((receipt["isolation"], receipt["companion_value"], receipt["duplicate_handoff"]),
                         ("net-denied", "deferred", "unchanged"))
        self.assertTrue(receipt["placement"].endswith("plans/main/QUESTIONS.md"))

    def test_example_runs_offline_only(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.base / "home"), "XDG_STATE_HOME": str(self.base / "xdg-state")}
        out = self.base / "example-out"
        done = subprocess.run([sys.executable, "-B", str(EXAMPLE / "run.py"), "--offline", "--out", str(out)], env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("deferred", done.stdout)
        self.assertIn("fixture", done.stdout)
        refused = subprocess.run([sys.executable, "-B", str(EXAMPLE / "run.py"), "--out", str(self.base / "never")], env=env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(refused.returncode, 2)
        self.assertFalse((self.base / "never").exists())

    def test_verify_receipts_prints_five_rows_with_fixture_only_discovery_missing(self):
        env = {"PATH": f"{self.base / 'bin'}:/usr/bin:/bin", "HOME": str(self.base / "home"), "XDG_STATE_HOME": str(self.base / "xdg-state")}
        done = subprocess.run([sys.executable, "-B", str(EXAMPLE / "verify_receipts.py")], env=env, capture_output=True, text=True,
                              timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for row in ("21-graph", "22-brain", "64-static-viewer", "64-external-viewer", "70-discovery"):
            self.assertRegex(done.stdout, rf"(?m)^{row}: (EXERCISED|MISSING)")
        self.assertRegex(done.stdout, r"(?m)^70-discovery: MISSING")
        self.assertRegex(done.stdout, r"(?m)^64-static-viewer: EXERCISED")

    def test_lying_set_is_not_confirmed(self):  # M1 / M2
        def lie(root):
            path = root / "bin" / "fake-set"
            path.write_text(path.read_text().replace("L.set_status(path, value)", "pass"))
        w = self.world(mutate=lie)
        w.bootstrap()
        rc, out, err = w.roundtrip("pack", "--disposition", "deferred")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("disposition not confirmed", err)


class A2Idempotent(DiCase):
    def test_duplicate_handoff_decide_and_third_handoff(self):
        w = self.world()
        w.bootstrap()
        w.js("sandbox", project=False)
        w.js("candidate", "--topic", "main", "--mode", "pack")
        first = w.js("handoff")
        self.assertEqual(first["placements"][0]["result"], "written (new QUESTIONS.md)")
        before = w.questions().read_bytes()
        second = w.js("handoff")
        self.assertEqual((second["placements"][0]["result"], second["rows"][0]["result"]), ("unchanged", "unchanged"))
        self.assertEqual(w.questions().read_bytes(), before)
        self.assertEqual(len(w.js("inbox list")["entries"]), 1)
        w.js("inbox intake")
        self.assertEqual(len(w.receipts("intake")), 1)  # M4: no dedup
        w.js("decide", "--id", "example/widget", "--disposition", "deferred")
        n = self.spawns()
        again = w.js("decide", "--id", "example/widget", "--disposition", "deferred")
        self.assertEqual((again["spawns"], again["unchanged"], self.spawns() - n), (0, True, 0))
        code, out, err = w.std("decide", "--id", "example/widget", "--disposition", "adopted")
        self.assertEqual(code, 1)
        self.assertIn("already decided", err)
        self.assertEqual(self.spawns() - n, 0)
        self.assertEqual(w.js("handoff")["staged"], 0)


class A3Modes(DiCase):
    def test_inbox_mode_roundtrips(self):
        w = self.world()
        w.bootstrap()
        rc, out, err = w.roundtrip("inbox", "--disposition", "adopted")
        self.assertEqual(rc, 0, (out, err))
        self.assertTrue((w.project / "plans" / "_inbox" / "example__widget.md").is_file())
        self.assertFalse(w.questions().exists())
        entry = w.js("inbox list")["entries"][0]
        self.assertEqual((entry["mode"], entry["disposition"], entry["companion_status"]), ("inbox", "adopted", "integrated"))

    def test_pseudo_target_is_not_placed(self):
        w = self.world()
        w.bootstrap()
        w.js("sandbox", project=False)
        w.js("candidate", "--topic", "main", "--mode", "pseudo")
        result = w.js("handoff")
        self.assertEqual(result["placements"][0]["result"], "not placed")
        self.assertEqual(w.js("inbox list")["entries"], [])
        self.assertFalse((w.targets / "_inbox").exists())
        self.assertEqual(w.std("decide", "--id", "example/widget", "--disposition", "deferred")[0], 1)


class A4Conflict(DiCase):
    def test_edited_body_is_a_conflict_not_a_second_candidate(self):
        w = self.world()
        w.bootstrap()
        w.js("sandbox", project=False)
        w.js("candidate", "--topic", "main", "--mode", "pack")
        w.js("handoff")
        w.js("inbox intake")
        w.js("candidate", "--topic", "main", "--mode", "pack", "--what", "An edited description of the candidate.")
        self.assertEqual(w.js("handoff")["placements"][0]["result"], "conflict")
        listing = w.js("inbox list")
        self.assertEqual(len(listing["entries"]), 1)  # M5
        conflict = listing["entries"][0]["conflict"]
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", conflict["original_sha256"]) and re.fullmatch(r"[0-9a-f]{64}", conflict["new_sha256"]))
        self.assertNotEqual(conflict["original_sha256"], conflict["new_sha256"])
        code, out, err = w.std("decide", "--id", "example/widget", "--disposition", "deferred")
        self.assertEqual(code, 1)
        self.assertIn("conflict", err)

    def test_changed_brief_after_removal_is_a_new_intake(self):  # M3
        w = self.world()
        w.bootstrap()
        w.js("sandbox", project=False)
        w.js("candidate", "--topic", "main", "--mode", "inbox")
        w.js("handoff")
        w.js("inbox intake")
        (w.project / "plans" / "_inbox" / "example__widget.md").unlink()
        w.js("candidate", "--topic", "main", "--mode", "inbox", "--what", "A revised description.")
        w.js("handoff")
        self.assertEqual(len(w.js("inbox list")["entries"]), 1)
        self.assertEqual(len({r["brief_sha256"] for r in w.receipts("intake")}), 2)


class A5SingleStore(DiCase):
    def test_companion_owns_status_and_pack_is_untouched(self):
        w = self.world()
        w.bootstrap()
        pack = w.pack_bytes()
        w.roundtrip("pack", "--disposition", "deferred")
        self.assertEqual(w.pack_bytes(), pack)
        subprocess.run([sys.executable, "-B", str(w.sandbox / "bin" / "fake-set"), "example/widget", "status", "skip"], check=True)
        before = w.js("inbox list")["entries"]
        self.assertEqual(before[0]["disposition"], "rejected")  # M6
        (w.out / "discovery.jsonl").unlink()
        self.assertEqual(w.js("inbox list")["entries"], before)
        self.assertEqual({p.name for p in w.out.rglob("*") if p.is_file()}, {"discovery.jsonl"})


class A6NoLedgerCopied(DiCase):
    def test_sandbox_holds_only_declared_files(self):
        w = self.world()
        w.bootstrap()
        info = w.js("sandbox", project=False)
        w.js("candidate", "--topic", "main", "--mode", "pack")
        files = sorted(p.relative_to(w.sandbox).as_posix() for p in w.sandbox.rglob("*") if p.is_file())
        self.assertEqual(files, sorted(["bin/fake-handoff", "bin/fake-set", "bin/fake-list", "bin/fake_lib.py", "data/repos/example__widget.md"]))
        self.assertIn("identity", info)
        w.roundtrip("pack")
        for base in (w.sandbox, w.out):
            text = w.all_text(base)
            self.assertNotIn("CANARY-DATA-LEDGER", text)
            self.assertNotIn("CANARY-UNDECLARED", text)

    def test_not_a_work_tree_and_symlink_member_are_refused(self):
        w = self.world()
        plain = w.dir / "plain"
        plain.mkdir()
        bad = w.write_manifest({**w.manifest_data(), "root": str(plain)}, name="plain.json")
        self.assertEqual(w.call("sandbox", "--companion", bad, "--sandbox", w.sandbox)[0], 2)

        def link(root):
            (root / "bin" / "fake_lib.py").unlink()
            os.symlink("../UNDECLARED.txt", root / "bin" / "fake_lib.py")
        self.assertEqual(self.world(mutate=link).std("sandbox", project=False)[0], 2)


class A7Versioned(DiCase):
    def test_bad_manifests_exit_2_with_zero_spawns(self):
        w = self.world()
        w.bootstrap()
        base = w.manifest_data()
        variants = {"interface": {**base, "interface": "handoff-brief/2"}, "unknown-key": {**base, "extra": 1},
                    "partial-map": {**base, "dispositions": {k: v for k, v in base["dispositions"].items() if k != "rejected"}},
                    "dotdot": {**base, "commands": {**base["commands"], "set": "../bin/fake-set"}},
                    "absolute": {**base, "support": ["/etc/passwd"]}}
        raws = {name: json.dumps(v) for name, v in variants.items()}
        raws["duplicate"] = json.dumps(base).replace('"name": "fake-discovery"', '"name": "fake-discovery", "name": "x"', 1)
        n = self.spawns()
        for name, raw in raws.items():
            path = w.write_manifest(raw=raw, name=f"{name}.json")
            code, out, err = w.call("roundtrip", "--companion", path, "--registry", w.registry, "--project", w.project, "--topic", "main",
                                    "--mode", "pack", "--out", w.out, "--backend", "fixture")
            self.assertEqual(code, 2, (name, out, err))
        self.assertEqual(self.spawns() - n, 0)

    def test_unknown_label_is_unsupported_and_identity_follows_files(self):
        w = self.world()
        w.bootstrap()
        w.js("sandbox", project=False)
        inbox = w.project / "plans" / "_inbox"
        inbox.mkdir(parents=True)
        (inbox / "a__b.md").write_text("# Handoff: a/b\n\n**What:** x\n**Bogus field:** y\n")
        listing = w.js("inbox list")
        self.assertEqual(listing["entries"], [])
        self.assertIn("unsupported brief", json.dumps(listing["refused"]))
        first = self.world()
        first.bootstrap()
        first.roundtrip("pack")

        def tweak(root):
            path = root / "bin" / "fake-list"
            path.write_text(path.read_text() + "# changed\n")
        other = self.world(mutate=tweak)
        other.bootstrap()
        other.roundtrip("pack")
        self.assertNotEqual(first.receipts()[-1]["identity"], other.receipts()[-1]["identity"])
        self.assertTrue(first.receipts()[-1]["identity"]["trees"]["bin"])


class A8Refusals(DiCase):
    def test_no_companion_and_role_allow_list(self):
        w = self.world()
        n = self.spawns()
        code, out, err = w.call("companions", "--out", w.out, "--json")
        self.assertEqual(code, 0, err)
        row = {r["id"]: r for r in json.loads(out)["rows"]}["70-discovery"]
        self.assertEqual(row["status"], "MISSING")
        self.assertIn("no companion manifest given", row["reason"])
        self.assertEqual(self.spawns() - n, 0)
        manifest = companion.load_manifest(w.manifest)
        companion.build_sandbox(manifest, w.sandbox)
        runner = companion.Runner(manifest, w.sandbox)
        for role in ("sync", "support", "triage", "../bin/fake-set"):
            with self.assertRaises(companion.Refuse):
                runner.spawn(role, [])
        self.assertEqual(self.spawns() - n, 0)  # M9

    def test_isolation_is_attested_only_by_a_real_denial(self):  # M14
        w = self.world()
        manifest = companion.load_manifest(w.manifest)
        companion.build_sandbox(manifest, w.sandbox)
        n = self.spawns()
        runner = companion.Runner(manifest, w.sandbox, probe="print('connected')")
        with self.assertRaises(companion.Refuse):
            runner.spawn("list", [])
        self.assertEqual(self.spawns() - n, 0)

    def test_targets_root_must_contain_the_registered_project(self):
        w = self.world()
        w.bootstrap()
        elsewhere = w.dir / "elsewhere"
        elsewhere.mkdir()
        w.js("sandbox", project=False)
        self.assertEqual(w.std("handoff", targets=elsewhere)[0], 2)


class A9Untrusted(DiCase):
    def test_hostile_briefs_are_escaped_or_refused(self):
        w = self.world()
        w.bootstrap()
        w.js("sandbox", project=False)
        inbox = w.project / "plans" / "_inbox"
        inbox.mkdir(parents=True)
        sent = w.dir / "SENTINEL"
        (inbox / "big__one.md").write_text("# Handoff: big/one\n\n**What:** " + "x" * 70000 + "\n")
        (inbox / "ctl__one.md").write_text("# Handoff: ctl/one\n\n**What:** red\x1b[31m bell\x07 done\n")
        (inbox / "sub__one.md").write_text(f"# Handoff: sub/one\n\n**What:** $(touch {sent}) `touch {sent}`\n**Next action:** [x](../../outside.md)\n")
        (inbox / "bad name.md").write_text("# Handoff: bad/name\n\n**What:** x\n")
        (inbox / "noparts.md").write_text("# Handoff: no/parts\n\n**What:** x\n")
        listing = w.js("inbox list")
        self.assertEqual({e["id"] for e in listing["entries"]}, {"ctl/one", "sub/one"})
        self.assertEqual(len(listing["refused"]), 3)  # M10: the size cap
        ctl = [e for e in listing["entries"] if e["id"] == "ctl/one"][0]["fields"]["What"]
        self.assertNotRegex(ctl, "[\x00-\x08\x0b-\x1f\x7f]")
        self.assertIn("\\x1b", ctl)
        sub = [e for e in listing["entries"] if e["id"] == "sub/one"][0]["fields"]
        self.assertIn("$(touch", sub["What"])
        self.assertEqual(sub["Next action"], "[x](../../outside.md)")
        self.assertFalse(sent.exists())


class A10Companions(Baseline):
    def table(self, rc=0):
        code, out, err = self.w.call("companions", "--out", self.w.out, "--companion", self.w.manifest, "--json", *GRAPH_ARGS)
        self.assertEqual(code, rc, (out, err))
        return {r["id"]: r for r in json.loads(out)["rows"]} if rc != 1 else None

    def test_five_rows_and_fixture_receipt_is_never_exercised(self):
        rows = self.table()
        self.assertEqual(set(rows), {"21-graph", "22-brain", "64-static-viewer", "64-external-viewer", "70-discovery"})
        for row in rows.values():
            self.assertTrue(row["owner"] and row["reason"], row)
            self.assertEqual(row["registry_claim"], REGISTRY_SUPPORT[row["id"].split("-")[0]], row)
            self.assertIn(row["status"], ("EXERCISED", "MISSING"))
        self.assertEqual(rows["70-discovery"]["status"], "MISSING")  # M11
        self.assertEqual(rows["64-static-viewer"]["status"], "EXERCISED")
        # row 64 is partial: an exercised static half is no conflict, the missing external half is
        self.assertEqual(rows["64-static-viewer"]["registry_claim"], "partial")
        self.assertFalse(rows["64-static-viewer"]["conflict"])
        self.assertTrue(rows["64-external-viewer"]["conflict"])
        # rows whose only sub-row is missing are planned in the registry, so they agree
        for ident in ("22-brain", "70-discovery"):
            self.assertEqual(rows[ident]["registry_claim"], "planned")
            self.assertFalse(rows[ident]["conflict"], ident)
        self.assertEqual(rows["22-brain"]["status"], "MISSING")
        for row in rows.values():
            self.assertEqual(bool(row["conflict"]), (row["status"] == "EXERCISED") != (row["registry_claim"] != "planned"), row)

    def test_rows_equal_the_owning_modules_own_output(self):
        rows = self.table()
        env = {"PATH": "/usr/bin:/bin"}
        viewer = subprocess.run([sys.executable, "-B", str(ROOT / "modules" / "state-adapters" / "viewer.py"), "list", "--format", "json"],
                                capture_output=True, text=True, env=env, check=True)
        entry = [v for v in json.loads(viewer.stdout)["viewers"] if v["name"] == "companion"][0]
        self.assertEqual((rows["64-external-viewer"]["status"], rows["64-external-viewer"]["reason"]), ("MISSING", entry["reason"]))
        done = subprocess.run([sys.executable, "-B", str(ROOT / "modules" / "knowledge-adapters" / "knowledge.py"), "exercised", "--out",
                               str(self.w.out / "knowledge")], capture_output=True, text=True, env=env)
        graph = json.loads(done.stdout)
        self.assertEqual(rows["21-graph"]["status"], "EXERCISED" if graph["graph"]["status"] == "EXERCISED" else "MISSING")
        self.assertEqual(rows["22-brain"]["status"], "EXERCISED" if graph["brain"]["status"] == "EXERCISED" else "MISSING")

    def test_row_without_owner_is_invalid_and_require_flag_exits_3(self):
        with mock.patch.dict(discovery.OWNERS, {"64-static-viewer": ""}):  # M12
            self.table(rc=1)
        with mock.patch.dict(os.environ, {"AIARCH_REQUIRE_COMPANION": "1"}):
            self.table(rc=3)  # M13


class A11HostCell(DiCase):
    @staticmethod
    def tree_hash(path):
        digest = hashlib.sha256()
        for p in sorted(Path(path).rglob("*")):
            if p.is_file():
                digest.update(p.relative_to(path).as_posix().encode() + p.read_bytes())
        return digest.hexdigest()

    def test_real_companion_roundtrip(self):
        if not HOST_MANIFEST:
            print("NOT RUN (no host companion manifest)")
            if REQUIRE:
                self.fail("AIARCH_REQUIRE_COMPANION=1 but AIARCH_DISCOVERY_MANIFEST is not set")
            self.skipTest("no host companion manifest")
        w = World(self.base, "host", manifest=HOST_MANIFEST)
        before = (git(w.root, "status", "--porcelain", "--", "bin", "data/repos"), self.tree_hash(w.root / "data"))
        w.bootstrap()
        for mode in ("pack", "inbox"):
            rc, out, err = w.std("roundtrip", "--topic", "main", "--mode", mode, "--backend", "companion", "--disposition", "deferred",
                                 "--id", f"example/{mode}-widget")
            self.assertEqual(rc, 0, (mode, out, err))
        receipts = w.receipts()
        self.assertEqual(len(receipts), 2)
        for r in receipts:
            self.assertEqual((r["backend"], r["isolation"], r["companion_ack"], r["duplicate_handoff"]),
                             ("companion", "net-denied", "confirmed", "unchanged"))
            self.assertTrue(r["identity"]["trees"])
        code, out, err = w.call("companions", "--out", w.out, "--companion", w.manifest, "--json", *GRAPH_ARGS)
        self.assertEqual({r["id"]: r for r in json.loads(out)["rows"]}["70-discovery"]["status"], "EXERCISED", out)
        after = (git(w.root, "status", "--porcelain", "--", "bin", "data/repos"), self.tree_hash(w.root / "data"))
        self.assertEqual(before, after)
        print("EXERCISED: 70-discovery via a host companion, pack and inbox mode")


class A12Offline(Baseline):
    @staticmethod
    def source_files():
        return [p for base in (MOD, EXAMPLE) for p in sorted(base.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("test_")]

    def test_static_scan(self):
        banned = re.compile(r"urllib|http\.client|\bssl\b|\brequests\b|socket\.connect|shell\s*=\s*True|os\.system")
        for p in self.source_files():
            if p.suffix == ".py" or p.parent.name == "bin":
                text = re.sub(r'"""[\s\S]*?"""', "", p.read_text())
                self.assertIsNone(banned.search(text), p)

    def test_no_host_manifest_token_in_tree(self):
        if not HOST_MANIFEST:
            self.skipTest("no host companion manifest")
        data = json.loads(Path(HOST_MANIFEST).read_text())
        text = "".join(p.read_text(errors="replace") for p in self.source_files())
        for token in {data["name"], data["root"], *data["commands"].values(), Path(data["root"]).name}:
            self.assertNotIn(token, text)

    def test_stable_sha256_is_equal_across_runs(self):
        other = World(self.base, "stable-b")
        other.bootstrap()
        other.roundtrip("pack", "--disposition", "deferred")
        mine, theirs = self.w.receipts()[-1], other.receipts()[-1]
        self.assertEqual(discovery.stable_sha256(mine), discovery.stable_sha256(theirs))
        self.assertNotEqual(discovery.stable_sha256(mine), discovery.stable_sha256(dict(theirs, disposition="adopted")))


class A13Constraints(Baseline):
    def test_descriptor_provenance_privacy_and_pins(self):
        descriptor = contracts.check_module_descriptor(MOD, module="discovery-inbox")
        self.assertEqual(descriptor["claimed_support"], "planned")
        self.assertEqual(sorted(d["identifier"] for d in descriptor["dependencies"]),
                         ["core-contracts", "knowledge-adapters", "state-adapters", "stewardship"])
        self.assertTrue(all(d["status"] == "declared-untested" for d in descriptor["dependencies"]))
        self.assertEqual(descriptor["schemas"], ["status"])
        check_collection.provenance(MOD / "PROVENANCE.md")
        text = (MOD / "PROVENANCE.md").read_text()
        self.assertEqual(len(re.findall(r"(?m)^## (?!Deltas|Not ported|Notes)", text)), 6)
        self.assertIn("## Not ported", text)
        self.assertEqual(privacy.scan(MOD), [])
        self.assertEqual(privacy.scan(EXAMPLE), [])
        for rel, digest in json.loads((MOD / "tests" / "fixtures" / "pins.json").read_text()).items():
            self.assertEqual(sha(ROOT / rel), digest, rel)

    def test_outputs_leak_no_paths_or_identity(self):
        blob = self.out + self.err + self.w.all_text(self.w.out)
        for secret in (str(self.w.out), str(self.w.root), os.environ["HOME"], getpass.getuser(), socket.gethostname()):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
