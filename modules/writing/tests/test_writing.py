"""writing acceptance tests (A1-A10 plus verifier amendments F1-F12). Synthetic data only."""

import contextlib
import copy
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
MODULE = ROOT / "modules" / "writing"
EXAMPLE = ROOT / "examples" / "writing"
sys.path.insert(0, str(ROOT / "modules" / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))

HOME_FRAG = "/" + "home/"
WIN_FRAG = "C:" + "\\" + "Users"
CALLS = []
_REAL_CONNECT = socket.socket.connect


def _guard(self, *args, **kwargs):
    CALLS.append(args)
    raise AssertionError("network connect attempted")


def setUpModule():
    socket.socket.connect = _guard


def tearDownModule():
    socket.socket.connect = _REAL_CONNECT
    assert not CALLS, "socket.connect reached"


def load():
    spec = importlib.util.spec_from_file_location("writing_under_test", MODULE / "writing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(*argv):
    writing = load()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = writing.main([str(a) for a in argv])
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


def jcli(*argv):
    code, out, err = cli(*argv, "--format", "json")
    return code, (json.loads(out) if out.strip().startswith(("{", "[")) else out), err


STUB = EXAMPLE / "stub_writer.py"
SOURCE = (EXAMPLE / "source.md").read_text(encoding="utf-8") if (EXAMPLE / "source.md").exists() else ""


def write_writers(directory, name="stub", argv=None, registers=None, destinations=None):
    argv = argv if argv is not None else [sys.executable, str(STUB)]
    spec = {"argv": argv, "registers": registers or ["essay"], "destinations": destinations or ["file"]}
    path = Path(directory) / "writers.json"
    path.write_text(json.dumps({"writers": {name: spec}}), encoding="utf-8")
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def render(self, register, out=None, extra=()):
        writers = write_writers(self.tmp, registers=["correspondence", "essay", "personal", "chat"],
                                destinations=["file"])
        src = self.tmp / "source.md"
        src.write_text(SOURCE, encoding="utf-8")
        out = out or self.tmp / ("out-" + register)
        code, data, err = jcli("writer", "run", "--writers", writers, "--name", "stub", "--register", register,
                               "--destination", "file", "--source", src, "--out", out, "--allow-writer", *extra)
        return code, data, err, Path(out)

    def check(self, source, draft, register="essay", extra=()):
        s, d = self.tmp / "s.md", self.tmp / "d.md"
        s.write_text(source, encoding="utf-8")
        d.write_text(draft, encoding="utf-8")
        out = self.tmp / "chk"
        code, data, err = jcli("check", "--source", s, "--draft", d, "--register", register, "--out", out, *extra)
        return code, json.loads((out / "fidelity.json").read_text()) if (out / "fidelity.json").exists() else None


class A1Registers(Tmp):
    def table(self):
        return json.loads((MODULE / "registers.json").read_text())

    def validate(self, table):
        path = self.tmp / "r.json"
        path.write_text(json.dumps(table), encoding="utf-8")
        return cli("validate-registers", path)

    def test_shipped_valid(self):
        self.assertEqual(cli("validate-registers", MODULE / "registers.json")[0], 0)
        code, data, _ = jcli("registers")
        self.assertEqual(code, 0)
        self.assertEqual(set(data["registers"]), {"correspondence", "chat", "essay", "op-ed", "academic",
                                                  "personal", "fiction"})

    def test_mutations(self):
        def mut(fn):
            table = copy.deepcopy(self.table())
            fn(table["registers"] if "registers" in table else table)
            return self.validate(table)

        code, _, err = mut(lambda t: t["correspondence"]["may_inherit_from"].append("essay"))
        self.assertEqual(code, 1)
        self.assertIn("correspondence-no-essay-academic", err)
        code, _, err = mut(lambda t: t["correspondence"]["may_inherit_from"].append("academic"))
        self.assertEqual(code, 1)
        code, _, err = mut(lambda t: t["fiction"]["rule_sets"].append("evidence-density"))
        self.assertEqual(code, 1)
        self.assertIn("personal-fiction-isolated", err)
        code, _, err = mut(lambda t: t["personal"]["may_inherit_from"].append("chat"))
        self.assertEqual(code, 1)
        code, _, err = mut(lambda t: t["chat"].update(default="essay"))
        self.assertEqual(code, 1)
        self.assertIn("no-essay-default", err)


class A2Registers(Tmp):
    def test_three_registers_pass(self):
        table = json.loads((MODULE / "registers.json").read_text())["registers"]
        drafts = []
        for register in ("correspondence", "essay", "personal"):
            code, data, err, out = self.render(register)
            self.assertEqual(code, 0, err)
            receipt = json.loads((out / "fidelity.json").read_text())
            self.assertEqual(receipt["verdict"], "pass")
            self.assertGreaterEqual(receipt["facts"]["expected"], 4)
            self.assertEqual(receipt["review"]["rule_sets"], table[register]["rule_sets"])
            self.assertEqual(receipt["review"]["writer"], "stub")
            self.assertEqual(receipt["review"]["register"], register)
            drafts.append((out / "draft.md").read_text())
        self.assertEqual(len(set(drafts)), 3)
        self.assertNotIn(SOURCE, drafts)
        self.assertTrue(drafts[0].lower().startswith("dear"))

    def draft(self):
        return self.render("essay")[3].joinpath("draft.md").read_text()

    def test_drop_fact_and_intent(self):
        draft = self.draft()
        code, receipt = self.check(SOURCE, draft.replace("12 weeks", "some weeks"))
        self.assertEqual((code, receipt["verdict"], receipt["facts"]["missing"]), (1, "fail", ["12 weeks"]))
        intent = [l for l in SOURCE.splitlines() if l.startswith("Intent:")][0][7:].strip()
        code, receipt = self.check(SOURCE, draft.replace(intent, "Please consider continuing."))
        self.assertEqual((code, receipt["verdict"]), (1, "fail"))
        self.assertEqual(receipt["intent"], {"expected": 1, "kept": 0})

    def test_token_boundaries(self):
        draft = self.draft()
        code, receipt = self.check(SOURCE, draft.replace("4.2%", "14.2%"))
        self.assertEqual((code, receipt["facts"]["missing"]), (1, ["4.2%"]))
        url = re.search(r"https?://\S+", SOURCE).group(0)
        code, receipt = self.check(SOURCE, draft.replace(url, url[:-1] + "x"))
        self.assertEqual(code, 1)
        self.assertEqual(receipt["facts"]["missing"], [url])
        code, receipt = self.check(SOURCE, draft.replace(url, url + "x"))
        self.assertEqual(code, 1)

    def test_fact_kinds_present(self):
        code, receipt = self.check(SOURCE, SOURCE)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(receipt["facts"]["expected"], 5)

    def test_unknown_register(self):
        self.assertEqual(self.check(SOURCE, SOURCE, register="nope")[0], 2)


class A3Edits(Tmp):
    def run_edit(self, draft, previous, edited):
        (self.tmp / "p.md").write_text(previous)
        (self.tmp / "e.md").write_text(edited)
        return self.check("plain text\n", draft, extra=("--previous", self.tmp / "p.md", "--edited", self.tmp / "e.md"))

    def test_edits(self):
        p = "alpha line\nbeta line\ngamma line\n"
        e = "alpha line\nbeta edited\ngamma changed\n"
        code, receipt = self.run_edit("x\nbeta edited\ngamma changed\n", p, e)
        self.assertEqual((code, receipt["user_edits"]["expected"], receipt["user_edits"]["lost"]), (0, 2, []))
        code, receipt = self.run_edit("x\nbeta edited\ngamma line\n", p, e)
        self.assertEqual((code, receipt["verdict"], receipt["user_edits"]["lost"]), (1, "fail", ["gamma changed"]))

    def test_deletion(self):
        p = "alpha line\nbeta line\n"
        e = "beta line\n"
        self.assertEqual(self.run_edit("beta line\n", p, e)[0], 0)
        code, receipt = self.run_edit("alpha line\nbeta line\n", p, e)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["user_edits"]["resurrected"], ["alpha line"])

    def run_writer(self, previous, edited):
        writers = write_writers(self.tmp, registers=["essay"], destinations=["file"])
        src = self.tmp / "s.md"
        src.write_text(SOURCE)
        (self.tmp / "p.md").write_text(previous)
        (self.tmp / "e.md").write_text(edited)
        code, _, err = cli("writer", "run", "--writers", writers, "--name", "stub", "--register", "essay",
                           "--destination", "file", "--source", src, "--out", self.tmp / "o", "--allow-writer",
                           "--previous", self.tmp / "p.md", "--edited", self.tmp / "e.md")
        return code, json.loads((self.tmp / "o" / "fidelity.json").read_text())["user_edits"], \
            (self.tmp / "o" / "draft.md").read_text()

    def test_writer_run_passes_edits(self):
        code, err, draft = self.run_writer("alpha line\n", "alpha line\nzz unique lost edit\n")
        self.assertEqual(code, 1)
        self.assertEqual(err["lost"], ["zz unique lost edit"])
        line = next(l.strip() for l in draft.splitlines() if len(l.strip()) > 12)
        code, err, _ = self.run_writer(line + "\nkeep line\n", "keep line\n")
        self.assertEqual(code, 1)
        self.assertEqual(err["resurrected"], [line])

    def test_previous_needs_edited(self):
        (self.tmp / "p.md").write_text("a\n")
        code, _ = self.check("plain\n", "plain\n", extra=("--previous", self.tmp / "p.md"))
        self.assertEqual(code, 2)


class A4Truthful(Tmp):
    def route(self, writers, register="fiction", destination="newspaper"):
        return jcli("route", "--register", register, "--destination", destination, "--writers", writers)

    def test_limitation(self):
        writers = write_writers(self.tmp, name="essayist", registers=["essay", "fiction"], destinations=["newspaper"])
        # claims fiction in file, but the writer's own describe only offers essay/newspaper
        spec = json.loads(writers.read_text())
        spec["writers"]["essayist"]["argv"] += ["--registers", "essay", "--destinations", "newspaper"]
        writers.write_text(json.dumps(spec))
        code, data, _ = self.route(writers)
        self.assertEqual(code, 0)
        self.assertEqual(data["writer"], "assistant-draft")
        self.assertEqual(data["limitation"], "essayist does not support fiction for newspaper; "
                                              "delivered as assistant draft")
        self.assertIs(data["destination_updated"], False)
        code, data, _ = self.route(writers, "essay")
        self.assertEqual((data["writer"], data["destination_updated"]), ("essayist", False))
        empty = self.tmp / "empty.json"
        empty.write_text('{"writers": {}}')
        code, data, _ = self.route(empty)
        self.assertEqual(data["limitation"], "none does not support fiction for newspaper; "
                                              "delivered as assistant draft")
        self.assertEqual(self.route(empty, "bogus")[0], 2)

    def test_unavailable_writer_is_assistant_draft(self):
        writers = write_writers(self.tmp, name="gone", argv=["/nonexistent/writer-bin"],
                                registers=["fiction"], destinations=["newspaper"])
        code, data, _ = self.route(writers)
        self.assertEqual(data["writer"], "assistant-draft")
        self.assertIn("gone does not support fiction for newspaper", data["limitation"])
        self.assertIn("unavailable", data["reason"])
        src = self.tmp / "s.md"
        src.write_text("x\n")
        code, _, _ = cli("writer", "run", "--writers", writers, "--name", "gone", "--register", "fiction",
                         "--destination", "newspaper", "--source", src, "--out", self.tmp / "o", "--allow-writer")
        self.assertEqual(code, 2)
        self.assertFalse((self.tmp / "o" / "draft.md").exists())

    def test_nothing_written_outside_out(self):
        home, cwd = self.tmp / "home", self.tmp / "cwd"
        home.mkdir()
        cwd.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        writers = write_writers(self.tmp, registers=["essay"], destinations=["file"])
        src = self.tmp / "s.md"
        src.write_text(SOURCE)
        out, prof = self.tmp / "out", self.tmp / "prof"
        profile = self.tmp / "p.json"
        profile.write_text(json.dumps({"version": 1, "registers": {"essay": {"prefer": ["short"], "avoid": []}},
                                       "samples": []}))
        base = [sys.executable, "-B", str(MODULE / "writing.py")]
        for args in (["route", "--register", "essay", "--destination", "file", "--writers", str(writers)],
                     ["writer", "status", "--writers", str(writers), "--name", "stub"],
                     ["writer", "run", "--writers", str(writers), "--name", "stub", "--register", "essay",
                      "--destination", "file", "--source", str(src), "--out", str(out), "--allow-writer"],
                     ["profile", "import", str(profile), "--profile-dir", str(prof)],
                     ["profile", "import", str(profile), "--profile-dir", str(prof)],
                     ["profile", "rollback", "--profile-dir", str(prof)],
                     ["profile", "export", "--profile-dir", str(prof), "--out", str(out / "exp.json")]):
            result = subprocess.run(base + args, cwd=cwd, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(home.rglob("*")), [])
        self.assertEqual(list(cwd.rglob("*")), [])
        top = {p.name for p in self.tmp.iterdir()}
        self.assertEqual(top, {"home", "cwd", "writers.json", "s.md", "out", "prof", "p.json"})
        self.assertTrue(all(p.is_relative_to(prof) for p in prof.rglob("*")))


class A5Profile(Tmp):
    P1 = {"version": 1, "registers": {"essay": {"prefer": ["plain verbs"], "avoid": ["filler"]}},
          "samples": [{"register": "essay", "sha256": "a" * 64}]}
    P2 = {"version": 1, "registers": {"chat": {"prefer": ["short"], "avoid": []}}, "samples": []}

    def put(self, name, obj):
        path = self.tmp / name
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return path

    def export(self, prof, name="exp.json"):
        out = self.tmp / name
        self.assertEqual(cli("profile", "export", "--profile-dir", prof, "--out", out)[0], 0)
        return out.read_bytes()

    def test_round_trip_and_rollback(self):
        a, b = self.tmp / "a", self.tmp / "b"
        f1 = self.put("p1.json", self.P1)
        self.assertEqual(cli("profile", "import", f1, "--profile-dir", a)[0], 0)
        first = self.export(a, "e1.json")
        exported = self.tmp / "e1.json"
        self.assertEqual(cli("profile", "import", exported, "--profile-dir", b)[0], 0)
        self.assertEqual(self.export(b, "e2.json"), first)
        self.assertEqual(cli("profile", "rollback", "--profile-dir", b)[0], 3)
        self.assertEqual(cli("profile", "import", self.put("p2.json", self.P2), "--profile-dir", a)[0], 0)
        self.assertNotEqual(self.export(a, "e3.json"), first)
        self.assertEqual(cli("profile", "rollback", "--profile-dir", a)[0], 0)
        self.assertEqual(self.export(a, "e4.json"), first)
        self.assertEqual(oct((a / "profile.json").stat().st_mode & 0o777), "0o600")

    def test_refusals(self):
        prof = self.tmp / "prof"
        self.assertEqual(cli("profile", "import", self.put("ok.json", self.P1), "--profile-dir", prof)[0], 0)
        before = self.export(prof, "before.json")
        bad = {}
        with_text = copy.deepcopy(self.P1)
        with_text["samples"][0]["text"] = "hello"
        bad["text"] = with_text
        for label, value in (("home", HOME_FRAG + "x/y"), ("win", WIN_FRAG + "\\x"), ("mail", "a" + "@" + "b.example")):
            obj = copy.deepcopy(self.P1)
            obj["registers"]["essay"]["prefer"] = [value]
            bad[label] = obj
        obj = copy.deepcopy(self.P1)
        obj["extra"] = 1
        bad["topkey"] = obj
        obj = copy.deepcopy(self.P1)
        obj["registers"]["essay"]["prefer"] = [3]
        bad["nonstring"] = obj
        obj = copy.deepcopy(self.P1)
        obj["registers"]["essay"]["prefer"] = ["x" * 70000]
        bad["big"] = obj
        bad["dup"] = '{"version": 1, "version": 1, "registers": {}, "samples": []}'
        for label, value in bad.items():
            code, _, _ = cli("profile", "import", self.put(label + ".json", value), "--profile-dir", prof)
            self.assertEqual(code, 2, label)
            self.assertEqual(self.export(prof, "after.json"), before, label)

    def test_escaped_private_values_refused(self):
        prof = self.tmp / "prof"
        self.assertEqual(cli("profile", "import", self.put("ok.json", self.P1), "--profile-dir", prof)[0], 0)
        before = self.export(prof, "before.json")
        tmpl = '{"version": 1, "registers": {"essay": {"prefer": ["%s"], "avoid": []}}, "samples": []}'
        for label, esc in (("home", "\\u002fhome\\u002fx"), ("mail", "a\\u0040b.example"),
                           ("win", "C:\\u005cUsers\\u005cx")):
            code, _, _ = cli("profile", "import", self.put(label + ".json", tmpl % esc), "--profile-dir", prof)
            self.assertEqual(code, 2, label)
            self.assertEqual(self.export(prof, "after.json"), before, label)
        key = '{"version": 1, "registers": {"essay": {"prefer": [], "avoid": []}}, "samples": [], "a\\u0040b.example": 1}'
        self.assertEqual(cli("profile", "import", self.put("key.json", key), "--profile-dir", prof)[0], 2)


class A6Writer(Tmp):
    def status(self, writers, env=None):
        with mock.patch.dict(os.environ, env or {}):
            return jcli("writer", "status", "--writers", writers, "--name", "w")

    def test_available_reports_describe_not_claim(self):
        writers = write_writers(self.tmp, name="w", registers=["fiction", "essay"], destinations=["newspaper"])
        code, data, _ = self.status(writers)
        self.assertEqual(data["status"], "available")
        self.assertIn("essay", data["registers"])
        self.assertNotIn("fiction", data["registers"])
        self.assertNotIn("newspaper", data["destinations"])

    def test_unavailable(self):
        exe = sys.executable
        cases = {"missing": ["/nonexistent/bin"],
                 "slow": [exe, "-c", "import time; time.sleep(5)"],
                 "nonjson": [exe, "-c", "print('hello')"]}
        for label, argv in cases.items():
            writers = write_writers(self.tmp, name="w", argv=argv)
            code, data, _ = self.status(writers, {"WRITING_DESCRIBE_TIMEOUT": "1"})
            self.assertTrue(data["status"].startswith("unavailable"), (label, data))
            self.assertTrue(data.get("reason"), label)

    def test_unsupported_shape(self):
        writers = write_writers(self.tmp, name="w", argv=[sys.executable, "-c", "print('{\"name\": 1}')"])
        code, data, _ = self.status(writers)
        self.assertEqual(data["status"], "unsupported")


class A7NoSpend(Tmp):
    def test_requires_flag(self):
        sentinel = self.tmp / "spawned"
        writers = write_writers(self.tmp, name="w", argv=[sys.executable, str(STUB), "--sentinel", str(sentinel)],
                                registers=["essay"], destinations=["file"])
        src = self.tmp / "s.md"
        src.write_text(SOURCE)
        args = ["writer", "run", "--writers", writers, "--name", "w", "--register", "essay",
                "--destination", "file", "--source", src, "--out", self.tmp / "o"]
        self.assertEqual(cli(*args)[0], 2)
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.tmp / "o").exists())
        code, data, _ = jcli(*args, "--allow-writer")
        self.assertEqual(code, 0)
        self.assertTrue(sentinel.exists())
        self.assertTrue((self.tmp / "o" / "draft.md").exists())
        receipt = json.loads((self.tmp / "o" / "fidelity.json").read_text())
        self.assertEqual((receipt["review"]["writer"], receipt["review"]["verdict"]), ("w", "pass"))

    def test_no_send_options(self):
        words = re.compile(r"send|publish|upload|train|e-?mail", re.I)
        commands = [[], ["registers"], ["validate-registers"], ["route"], ["check"], ["profile"],
                    ["profile", "export"], ["profile", "import"], ["profile", "rollback"], ["writer"],
                    ["writer", "status"], ["writer", "run"]]
        for command in commands:
            code, out, _ = cli(*command, "--help")
            self.assertEqual(code, 0, command)
            self.assertIsNone(words.search(out), (command, words.search(out)))


class A8Example(Tmp):
    def run_example(self, out):
        return subprocess.run([sys.executable, "-B", str(EXAMPLE / "run.py"), "--offline", "--out", str(out)],
                              capture_output=True, text=True, cwd=self.tmp)

    def test_run_and_verify(self):
        result = self.run_example(self.tmp / "o")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[-1], "writing: OK")
        self.assertGreaterEqual(len(lines), 6)
        self.assertEqual(sum(1 for l in lines if "route" in l), 1)
        verify = subprocess.run([sys.executable, "-B", str(EXAMPLE / "verify_receipt.py")], capture_output=True,
                                text=True, cwd=self.tmp)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_tamper(self):
        out = self.tmp / "o"
        self.assertEqual(self.run_example(out).returncode, 0)
        verify = [sys.executable, "-B", str(EXAMPLE / "verify_receipt.py"), "--receipt", str(out / "receipt.json")]
        self.assertEqual(subprocess.run(verify, capture_output=True, text=True).returncode, 0)
        receipt = json.loads((out / "receipt.json").read_text())
        receipt["steps"].pop(0)
        (out / "receipt.json").write_text(json.dumps(receipt))
        self.assertEqual(subprocess.run(verify, capture_output=True, text=True).returncode, 1)
        self.assertEqual(self.run_example(out).returncode, 0)
        draft = next(out.glob("*/draft.md"))
        draft.write_text(draft.read_text() + "tampered\n")
        self.assertEqual(subprocess.run(verify, capture_output=True, text=True).returncode, 1)


class A9A10Portable(Tmp):
    def test_outputs_portable(self):
        import getpass
        out = self.tmp / "o"
        result = subprocess.run([sys.executable, "-B", str(EXAMPLE / "run.py"), "--offline", "--out", str(out)],
                                capture_output=True, text=True)
        texts = [result.stdout] + [p.read_text() for p in out.rglob("*") if p.is_file()]
        user, host = getpass.getuser(), socket.gethostname()
        for text in texts:
            self.assertNotIn(HOME_FRAG, text)
            if len(user) > 3:
                self.assertNotIn(user, text)
            if len(host) > 3:
                self.assertNotIn(host, text)

    def test_descriptor_and_scope(self):
        import contracts
        contracts.check_module_descriptor(MODULE, module="writing")
        descriptor = json.loads((MODULE / "descriptor.json").read_text())
        self.assertEqual((descriptor["claimed_support"], descriptor["evidence"], descriptor["schemas"],
                          descriptor["dependencies"]), ("planned", [], [], []))
        import privacy_scan
        for directory in (MODULE, EXAMPLE):
            self.assertEqual(privacy_scan.scan(directory), [])
            for path in directory.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.\w+", path.read_text()), path)


if __name__ == "__main__":
    unittest.main()
