"""R9-U07: observation production and episodic recall (A1-A13 plus the F1-F12 amendments).

Real subprocesses where possible, temp dirs, temp HOME, no network, no model. In-process cases drive the same modules
through main() so audit hooks and monkeypatches can see them.
"""

import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
BK = ROOT / "modules" / "memory-backends"
OBSERVE, RECALL = BK / "observe.py", BK / "recall.py"
RUN = ROOT / "examples" / "memory" / "run.py"
CURATED = ROOT / "modules" / "memory" / "curated.py"
FIXTURE = BK / "fixtures" / "session.jsonl"
SCOPE = "/work/demo"
EXAMPLE_ROOT = ROOT / "modules" / "memory" / "example" / "root"

# secret fixtures are assembled from fragments so no scanner flags this file
SECRETS = {
    "sk": "sk-" + "A1b2C3d4E5f6G7h8I9j0",
    "ghp": "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
    "pw": "password=" + "Hunter2xyzq",
    "key": "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END " + "PRIVATE KEY-----",
    "user": "ZEBRA" + "1234secretx",
}
RUNS = {"sk": "a1b2c3d4e5f6g7h8i9j0", "ghp": "abcdefghijklmnopqrstuvwxyz0123456789", "aws": "akiaiosfodnn7example",
        "pw": "hunter2xyzq", "key": "miievqibadanbgkqhkig9w0baqefaasc", "user": "zebra1234secretx"}


def loadmod(name, path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def post(tool, desc, cwd=SCOPE, path=None, **extra):
    event = {"hook_event_name": "PostToolUse", "tool_name": tool, "cwd": cwd, "tool_input": {"description": desc},
             "session_id": "SESSIONCANARY", "transcript_path": "/x/TRANSCRIPTCANARY.jsonl"}
    if path:
        event["tool_input"]["file_path"] = path
    event.update(extra)
    return event


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_store(case, store, needles):
    """F4: every file in the store dir, case-insensitive; returns names of hits."""
    hits = []
    for name in os.listdir(store):
        data = (Path(store) / name).read_bytes().lower()
        for needle in needles:
            if needle.lower().encode() in data:
                hits.append((name, needle))
    return hits


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "canary.txt").write_text("HOMECANARY")
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "canary.txt").write_text("OUTSIDECANARY")
        self.store = self.mkstore("store")
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"}

    def mkstore(self, name):
        path = self.tmp / name
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        (path / "OPT-IN").write_text("")
        return path

    def run_py(self, script, *args, stdin=None, env=None, timeout=60):
        return subprocess.run([sys.executable, "-B", str(script), *map(str, args)], input=stdin, capture_output=True,
                              text=True, env=env or self.env, timeout=timeout)

    def cap(self, events, *extra, scope=SCOPE, store=None, stdin=None, env=None):
        store = store or self.store
        if stdin is None:
            path = self.tmp / "events.jsonl"
            if isinstance(events, (str, Path)):
                path = Path(events)
            else:
                path.write_text("".join(json.dumps(e) + "\n" for e in events))
            argv = ["capture", "--store", store, "--events", path, "--scope", scope, *extra]
            proc = self.run_py(OBSERVE, *argv, env=env)
        else:
            proc = self.run_py(OBSERVE, "capture", "--store", store, "--events", "-", "--scope", scope, *extra,
                               stdin=stdin, env=env)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            out = None
        return proc, out

    def rec(self, q, *extra, store=None, env=None):
        proc = self.run_py(RECALL, "query", "--store", store or self.store, "--q", q, *extra, env=env)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            out = None
        return proc, out


class A1Production(Base):
    def test_fixture_session_then_recall(self):
        proc, out = self.cap(FIXTURE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["captured"], 4)
        self.assertEqual(out["dropped_scope"], 1)
        proc, res = self.rec("rounding")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(res["state"], "ok")
        self.assertFalse(res["abstained"])
        cand = res["candidates"][0]
        self.assertEqual((cand["authority"], cand["trust"]), ("observed", "untrusted-data"))
        self.assertRegex(cand["id"], r"^obs-[0-9a-f]+$")
        observe = loadmod("observe", OBSERVE)
        source = json.loads(FIXTURE.read_text().splitlines()[0])
        self.assertEqual(cand["excerpt"], observe.summarize(source, SCOPE))
        self.assertIn("feeding.py", cand["excerpt"])
        self.assertNotIn(SCOPE, cand["excerpt"])

    def test_stdin_hook_payload_idempotent_and_stable_id(self):
        event = post("Edit", "Tuned the lamination timer for the croissant batch.", path=SCOPE + "/src/timer.py")
        payload = json.dumps(event)
        proc, first = self.cap(None, stdin=payload)
        self.assertEqual((proc.returncode, first["captured"]), (0, 1), proc.stderr)
        proc, second = self.cap(None, stdin=payload)
        self.assertEqual(second["captured"], 0)
        _, a = self.rec("lamination")
        _, b = self.rec("croissant batch")
        self.assertEqual(len(a["candidates"]), 1)
        self.assertEqual(a["candidates"][0]["id"], b["candidates"][0]["id"])
        observe = loadmod("observe", OBSERVE)
        self.assertEqual(a["candidates"][0]["excerpt"], observe.summarize(event, SCOPE))

    def test_stdin_cap(self):
        proc, _ = self.cap(None, stdin=json.dumps(post("Edit", "x" * (1 << 20 + 1))))
        self.assertEqual(proc.returncode, 2)

    def test_unsupported_summarizer(self):
        proc, _ = self.cap(FIXTURE, "--summarizer", "model")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no model summarizer in this collection", proc.stderr)
        self.assertEqual(os.listdir(self.store), ["OPT-IN"])


class A2OptIn(Base):
    def assert_refused(self, store):
        proc, _ = self.cap([post("Edit", "alpha")], store=store)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_missing_opt_in(self):
        bare = self.tmp / "bare"
        bare.mkdir(mode=0o700)
        self.assert_refused(bare)
        self.assertEqual(os.listdir(bare), [])

    def test_symlinked_store(self):
        link = self.tmp / "link"
        link.symlink_to(self.store)
        self.assert_refused(link)
        self.assertFalse((self.store / "obs.db").exists())

    def test_wrong_mode(self):
        os.chmod(self.store, 0o755)
        self.assert_refused(self.store)
        self.assertFalse((self.store / "obs.db").exists())

    def test_not_owned_by_user(self):
        observe = loadmod("observe", OBSERVE)
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(observe.Refuse):
                observe.open_store(str(self.store))

    def test_relative_traversal(self):
        proc = self.run_py(OBSERVE, "capture", "--store", f"{self.store}/../store", "--events", FIXTURE, "--scope", SCOPE)
        self.assertEqual(proc.returncode, 2)


class A3Scope(Base):
    def test_scope_kinds_and_sibling_prefix(self):
        events = [post("Edit", "inscopecanary alpha"),
                  post("Edit", "othercanary beta", cwd="/work/other"),
                  post("Edit", "evilcanary gamma", cwd="/work/demo-evil/src"),
                  post("Edit", "dotdotcanary delta", cwd="/work/demo/../demo-evil"),
                  {"hook_event_name": "Stop", "cwd": SCOPE, "last_assistant_message": "stopcanary epsilon."},
                  post("Edit", "nocwdcanary zeta", cwd=None)]
        events[-1].pop("cwd")
        proc, out = self.cap(events, "--kinds", "tool")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((out["captured"], out["dropped_scope"]), (1, 5))
        for term in ("othercanary", "evilcanary", "dotdotcanary", "stopcanary", "nocwdcanary"):
            _, res = self.rec(term)
            self.assertTrue(res["abstained"], term)
            self.assertEqual(res["candidates"], [])
        _, res = self.rec("inscopecanary")
        self.assertEqual(len(res["candidates"]), 1)

    def test_no_session_id_transcript_or_raw_event(self):
        self.cap([post("Edit", "storedlean alpha", path=SCOPE + "/a.py")])
        self.assertEqual(scan_store(self, self.store, ["SESSIONCANARY", "TRANSCRIPTCANARY", "transcript_path",
                                                        "session_id", "tool_input"]), [])
        con = sqlite3.connect(self.store / "obs.db")
        cols = [r[1] for r in con.execute("PRAGMA table_info(obs)")]
        con.close()
        self.assertEqual(set(cols), {"id", "project", "kind", "tool_name", "ts", "captured_at", "summary"})

    def test_paths_relative_or_outside(self):
        events = [post("Read", "Looked at it.", path=SCOPE + "/pkg/mod.py"),
                  post("Read", "Looked at config.", path="/opt/elsewhere/secret-config.yaml"),
                  post("Read", "Went up and out.", path=SCOPE + "/../demo-evil/x.py"),
                  post("Bash", "Opened /etc/passwd and /work/demo/lib/a.txt for reading.")]
        self.cap(events)
        _, res = self.rec("looked config went opened")
        text = " ".join(c["excerpt"] for c in res["candidates"])
        self.assertIn("pkg/mod.py", text)
        self.assertIn("<outside>", text)
        self.assertIn("lib/a.txt", text)
        self.assertNotRegex(text, r"(?<![\w<])/[\w.-]+/")
        self.assertNotIn("secret-config", text)


ZW = "\u200b"
HOMEDIR = "/" + "home/u"


class F1RedactAfterNormalize(Base):
    def stored(self):
        return b"".join((self.store / n).read_bytes() for n in os.listdir(self.store)).lower()

    def test_zero_width_split_secrets(self):
        cases = {"sk": "sk-abcd" + ZW + "efghij1234", "pw": "pass" + ZW + "word=hunter2zz",
                 "aws": "AK" + ZW + "IA0123456789ABCDEF", "ghp": "gh" + ZW + "p_" + "abcdefghijklmnopqrstuvwx"}
        runs = {"sk": b"abcdefghij1234", "pw": b"hunter2zz", "aws": b"ia0123456789abcdef",
                "ghp": b"abcdefghijklmnopqrstuvwx"}
        for key, secret in cases.items():
            with self.subTest(key):
                store = self.mkstore("s-" + key)
                proc, out = self.cap([post("Bash", f"Rotated {secret} for staging.")], store=store)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertGreater(out["redacted"], 0)
                data = b"".join((store / n).read_bytes() for n in os.listdir(store)).lower()
                self.assertNotIn(runs[key], data)
                self.assertNotIn(secret.encode().lower(), data)

    def test_tool_name_redacted(self):
        tool = "ghp_" + "abcdefghijklmnopqrstuvwx"
        proc, out = self.cap([post(tool, "Ran it.")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"abcdefghijklmnopqrstuvwx", self.stored())

    def test_redact_before_light_is_caught(self):
        observe = loadmod("observe", OBSERVE)
        secret = "sk-abcd" + ZW + "efghij1234"
        red = observe.Redactor()
        out = observe.summarize(post("Bash", secret), SCOPE, red)
        self.assertNotIn("efghij1234", out)
        self.assertGreater(red.count, 0)

    def test_early_redact_layer_alone(self):
        # the 200-char sentence cut would leave a secret prefix too short for the final pass to match
        observe = loadmod("observe", OBSERVE)
        out = observe.summarize(post("Bash", "x" * 189 + " sk-ABCDEFGHIJKLMNOP1234 tail."), SCOPE, observe.Redactor())
        self.assertNotIn("sk-ABC", out)

    def test_final_redact_layer_alone(self):
        # the tool-name head joins the sentence after the early pass: "pwd" + ": " + value forms a secret only then
        observe = loadmod("observe", OBSERVE)
        out = observe.summarize(post("pwd", "hunter2zz rotated."), SCOPE, observe.Redactor())
        self.assertNotIn("hunter2zz", out)


class F2AbsolutePaths(Base):
    FORMS = ["file://" + HOMEDIR + "/x", "cwd:" + HOMEDIR + "/y", "PATH=/usr/bin:" + HOMEDIR + "/bin",
             HOMEDIR + "/My Documents/plan.txt", "~/x", "~/My Notes/n.txt"]

    def test_forms_leave_no_fragment(self):
        observe = loadmod("observe", OBSERVE)
        for form in self.FORMS:
            with self.subTest(form):
                out = observe.scrub_paths(f"see {form} now", SCOPE)
                self.assertNotRegex(out, r"/(home|usr)|~|file:|Documents|plan\.txt|My Notes|n\.txt")
                self.assertIn("<outside>", out)

    def test_redirect_markup_forms(self):
        observe = loadmod("observe", OBSERVE)
        for form in ("x <code>%s/secret/a.txt</code> y", "run 2>%s/err.log now", "a ->%s/x b", "see <%s/in.txt>",
                     "make >%s/build.log"):
            with self.subTest(form):
                out = observe.scrub_paths(form % HOMEDIR, SCOPE)
                self.assertNotIn("home", out)
                self.assertIn("<outside>", out)
        self.assertEqual(observe.scrub_paths("x <code>/work/demo/a.txt</code>", SCOPE), "x <code>a.txt</code>")

    def test_urls_and_odd_forms(self):
        observe = loadmod("observe", OBSERVE)
        self.assertEqual(observe.scrub_paths("see https://github.com/foo/bar ok", SCOPE),
                         "see https://github.com<outside> ok")
        self.assertEqual(observe.scrub_paths(HOMEDIR + "//secret.txt", SCOPE), "<outside>")
        for form in ("file://localhost" + HOMEDIR + "/x", "File://" + HOMEDIR + "/x", "FILE:" + HOMEDIR + "/x"):
            with self.subTest(form):
                self.assertEqual(observe.scrub_paths(form, SCOPE), "<outside>")
                self.assertEqual(observe.shown_path(form, SCOPE), "<outside>")

    def test_url_embedded_paths(self):
        observe = loadmod("observe", OBSERVE)
        H = HOMEDIR
        cases = {
            "http://localhost:5173/__open-in-editor?file=%s/proj/a.py" % H: "http://localhost:5173<outside>?file=<outside>",
            "see http://x/open?path=~/notes.txt ok": "see http://x<outside>?path=<outside> ok",
            "https://x.com/a,%s/s.txt" % H: "https://x.com<outside>,<outside>",
            "https://x.com/a;%s/s" % H: "https://x.com<outside>;<outside>",
            "(https://x.com)%s/s" % H: "(https://x.com)<outside>",
            "http://h/p?a=1&b=%s/x#%s/y" % (H, H): "http://h<outside>?a=1&b=<outside>#<outside>",
            "https://x.com/cb?next=https://y.org/a": "https://x.com<outside>?next=https://y.org<outside>",
            "vscode://file%s/p/a.py:3" % H: "<outside>:3",
            "sqlite://%s/db" % H: "<outside>",
            "UNIX:%s/s.sock" % H: "<outside>",
            "https://github.com/foo/bar?x=1": "https://github.com<outside>?x=1",
        }
        for src, want in cases.items():
            with self.subTest(src):
                self.assertEqual(observe.scrub_paths(src, SCOPE), want)
        out = observe.summarize({"hook_event_name": "Stop", "last_assistant_message":
                                 "Opened http://localhost:5173/__open-in-editor?file=%s/proj/a.py for you." % H},
                                SCOPE, observe.Redactor())
        self.assertNotIn("home", out)

    PLAIN = ["https://github.com/foo/bar?x=1", "https://maps.google.com/@48.2,16.3,15z", "https://x.com/cb?next=https://y.org/a",
             "http://127.0.0.1:8080/#/dashboard"]
    LEAKS = ("home/u", "Users/u", "a.py", "~/a", "/a")

    def test_generated_corpus_every_cell_scrubbed(self):
        import string
        observe = loadmod("observe", OBSERVE)
        preds = [c for c in string.punctuation] + ["](", "="]
        forms = [HOMEDIR + "/a.py", "~/a", "/Users/u/a"]
        bad = []
        for pred in preds:
            for form in forms:
                cells = {
                    "query": "https://h.example/p?q=1" + pred + form,
                    "fragment": "https://h.example/p#frag" + pred + form,
                    "path": "https://h.example/p" + pred + form + "/z",
                    "glued": "see https://h.example/p" + pred + form,
                    "nested-file": "https://h.example/p?u=" + pred + "file://" + form,
                    "nested-vscode": "https://h.example/p?u=" + pred + "vscode://file" + form,
                    "nested-https": "https://h.example/p?u=" + pred + "https://h2.example/" + form.lstrip("/") + pred + form,
                }
                for pos, src in cells.items():
                    out = observe.scrub_paths(src, SCOPE)
                    if re.search(r"home/u|Users/u|a\.py|~/a|/Users", out) or (pos != "glued" and "<outside>" not in out):
                        bad.append((pos, pred, form, out))
        self.assertEqual(bad, [])

    def test_fix3_repros_named_cells(self):
        observe = loadmod("observe", OBSERVE)
        H = HOMEDIR
        cells = {
            "F1-file": "http://x/?next=file://%s/notes" % H,
            "F1-vscode": "http://x/?u=vscode://file%s/a.py" % H,
            "F1-sqlite": "http://x/?u=sqlite://%s/db" % H,
            "F1-FILE": "http://x/?u=FILE://%s/db" % H,
            "F1-pdfjs": "Open http://localhost:8000/web/viewer.html?file=file://" + HOMEDIR + "/proj/report.pdf to view it.",
            "F2-plus": "https://www.google.com/search?q=ls+%s/notes" % H,
            "F2-paren": "http://h/p?f=(%s/x)" % H,
            "F2-bracket": "http://h/p?f=[%s/x]" % H,
            "F2-brace": "http://h/p?f={%s/x}" % H,
            "F2-pipe-q": "http://h/p?a=1|%s/x" % H,
            "F2-amp": "http://h/p?a&%s/x" % H,
            "F2-bang": "http://h/p?a=!%s/x" % H,
            "F2-pipe-path": "https://x.com/a|%s/s" % H,
            "F2-rbracket": "https://x.com/a]%s/s" % H,
            "F2-md": "[https://x.com](%s/notes.md)" % H,
            "F2-summary": "Search https://www.google.com/search?q=ImportError+" + HOMEDIR + "/proj/app.py for that.",
        }
        for name, src in cells.items():
            with self.subTest(name):
                out = observe.scrub_paths(src, SCOPE)
                self.assertNotRegex(out, r"home|notes|a\.py|report|/db")
                self.assertIn("<outside>", out)

    def test_plain_url_authority_kept(self):
        observe = loadmod("observe", OBSERVE)
        for url, host in zip(self.PLAIN, ("https://github.com", "https://maps.google.com", "https://x.com", "http://127.0.0.1:8080")):
            with self.subTest(url):
                out = observe.scrub_paths("see " + url + " ok", SCOPE)
                self.assertTrue(out.startswith("see " + host) and out.endswith(" ok"), out)

    def test_fix5_f1_f2_named_cells(self):
        observe = loadmod("observe", OBSERVE)
        cells = {}
        for name, g in (("comma", ","), ("semi", ";"), ("paren", ")"), ("hash", "#"), ("query", "?a=x:")):
            cells["F1-" + name] = "https://example.com" + g + "~nik/private/plan.md"
            for root in ("/media", "/run/media", "/srv"):
                cells["F2-%s-%s" % (name, root)] = "https://example.com" + g + root + "/nik/private/plan.md"
        cells["F2-query-value"] = "https://e.com/p?f=" + "/media" + "/nik/plan.md"
        cells["F2-fragment"] = "https://e.com/p#" + "/srv" + "/nik/plan.md"
        for name, src in cells.items():
            with self.subTest(name):
                out = observe.scrub_paths("see " + src + " ok", SCOPE)
                self.assertNotRegex(out, r"nik|private|plan|media|srv")
                self.assertIn("<outside>", out)
                self.assertTrue(out.startswith("see https://e"))

    def test_corpus_outputs_hold_no_home_roots(self):
        import string
        observe = loadmod("observe", OBSERVE)
        forms = [HOMEDIR + "/a.py", "~/a", "~nik/a", "/Users/u/a", "/media/nik/a", "/run/media/nik/a", "/srv/nik/a"]
        root = re.compile(r"/(?:home|Users|root|srv|media)\b|/run/media|~\w*/")
        bad = []
        for pred in list(string.punctuation) + ["](", "=", "x:"]:
            for form in forms:
                for src in ("https://h.example/p?q=1" + pred + form, "https://h.example#" + pred + form,
                            "see https://h.example" + pred + form, "https://h.example/p?u=" + pred + "file://" + form):
                    out = observe.scrub_paths(src, SCOPE)
                    for tok in out.split():
                        if root.search(tok):
                            bad.append((src, out))
        self.assertEqual(bad, [])

    def test_spaced_tail_whole_token_and_prose_kept(self):
        observe = loadmod("observe", OBSERVE)
        self.assertEqual(observe.scrub_paths("a " + HOMEDIR + "/My Documents/plan.txt b", SCOPE), "a <outside> b")
        self.assertEqual(observe.scrub_paths("Opened /etc/passwd and /work/demo/lib/a.txt ok", SCOPE),
                         "Opened <outside> and lib/a.txt ok")
        self.assertEqual(observe.scrub_paths("file:///work/demo/My Docs/a.txt", SCOPE), "My Docs/a.txt")

    def test_stored_summaries(self):
        events = [post("Bash", f"Looked at {f} today.", path=p)
                  for f, p in zip(self.FORMS, ["~/x", "file://" + HOMEDIR + "/z", HOMEDIR + "/My Documents/p.txt",
                                              "~/y", "/etc/q", SCOPE + "/in.py"])]
        self.assertEqual(self.cap(events)[0].returncode, 0)
        _, res = self.rec("looked today")
        text = " ".join(c["excerpt"] for c in res["candidates"])
        self.assertIn("in.py", text)
        self.assertNotRegex(text, r"/(home|usr|etc)|~|file:|Documents|plan\.txt")
        self.assertNotRegex(text, r"(?<![\w<])/[\w.-]+")


class F3HookMode(Base):
    def both(self, setup, stdin=None, use_mock=False):
        for hook in (False, True):
            store = self.mkstore("h%d%s" % (hook, setup.__name__))
            extra = setup(store) or ()
            args = ["capture", "--store", store, "--events", "-", "--scope", SCOPE, *extra]
            if hook:
                args.append("--hook")
            payload = stdin if stdin is not None else json.dumps(post("Edit", "alpha"))
            proc = self.run_py(OBSERVE, *args, stdin=payload)
            if hook:
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
                self.assertFalse((store / "obs.db").exists())
            else:
                self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_missing_opt_in(self):
        def setup(store):
            os.remove(store / "OPT-IN")
        self.both(setup)

    def test_deleted_store(self):
        def setup(store):
            os.remove(store / "OPT-IN")
            os.rmdir(store)
        for hook in (False, True):
            gone = self.tmp / ("gone%d" % hook)
            args = ["capture", "--store", gone, "--events", "-", "--scope", SCOPE] + (["--hook"] if hook else [])
            proc = self.run_py(OBSERVE, *args, stdin="{}")
            self.assertEqual(proc.returncode, 0 if hook else 2, proc.stderr)
            if hook:
                self.assertEqual((proc.stdout, len(proc.stderr.strip().splitlines())), ("", 1))
                self.assertFalse(gone.exists())

    def test_mode_0755(self):
        def setup(store):
            os.chmod(store, 0o755)
        self.both(setup)

    def test_oversized_stdin(self):
        def setup(store):
            return ()
        self.both(setup, stdin=json.dumps(post("Edit", "x" * (2 << 20))))

    def test_missing_fts5(self):
        observe = loadmod("observe", OBSERVE)
        for hook in (False, True):
            store = self.mkstore("f%d" % hook)
            argv = ["capture", "--store", str(store), "--events", "-", "--scope", SCOPE] + (["--hook"] if hook else [])
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(observe, "probe_fts5", return_value=False), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = observe.main(argv)
            self.assertEqual(rc, 0 if hook else 2)
            if hook:
                self.assertEqual((out.getvalue(), len(err.getvalue().strip().splitlines())), ("", 1))
                self.assertFalse((store / "obs.db").exists())

    def test_bad_args_hook(self):
        proc = self.run_py(OBSERVE, "capture", "--hook", "--bogus")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)

    def test_hook_success_silent_and_stores(self):
        proc = self.run_py(OBSERVE, "capture", "--store", self.store, "--events", "-", "--scope", SCOPE, "--hook",
                           stdin=json.dumps(post("Edit", "hookstored alpha")))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertFalse(self.rec("hookstored")[1]["abstained"])


class A4Redaction(Base):
    def fixtures(self):
        return [post("Bash", f"Rotated {k}label credential {SECRETS[k]} for the staging box.") for k in SECRETS]

    def test_secrets_absent_everywhere(self):
        redact = self.tmp / "redact.txt"
        redact.write_text("# user patterns\nZEBRA[0-9]{4}[a-z]+\n")
        proc, out = self.cap(self.fixtures(), "--redact-file", redact)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["redacted"], len(SECRETS))
        self.assertEqual(out["captured"], len(SECRETS))
        self.assertEqual(scan_store(self, self.store, list(RUNS.values())), [])
        for term in ("rotated", "credential", "staging"):
            proc, res = self.rec(term)
            blob = proc.stdout.lower()
            for run in RUNS.values():
                self.assertNotIn(run, blob)
        self.assertIn("REDACTED", blob.upper())

    def test_without_redact_file_user_pattern_not_applied(self):
        _, out = self.cap(self.fixtures())
        self.assertEqual(out["redacted"], len(SECRETS) - 1)


class A5Retention(Base):
    def test_age_and_rows_pruned_and_unrecoverable(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=40)
        self.cap([post("Edit", "xylophoneancient alpha"), post("Edit", "quartzmarmot beta")], "--now", iso(old))
        proc, out = self.cap([post("Edit", "freshcanary gamma")], "--retain-days", "30")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((out["captured"], out["pruned"]), (1, 2))
        for term in ("xylophoneancient", "quartzmarmot"):
            _, res = self.rec(term)
            self.assertTrue(res["abstained"])
        self.assertEqual(scan_store(self, self.store, ["xylophoneancient", "quartzmarmot"]), [])
        _, res = self.rec("freshcanary")
        self.assertEqual(len(res["candidates"]), 1)

    def test_max_rows(self):
        events = [post("Edit", f"rowcanary{n} filler") for n in "abcdef"]
        proc, out = self.cap(events, "--max-rows", "4")
        self.assertEqual((out["captured"], out["pruned"]), (6, 2))
        gone = [t for t in ("rowcanarya", "rowcanaryb")]
        for term in gone:
            self.assertTrue(self.rec(term)[1]["abstained"])
        self.assertFalse(self.rec("rowcanaryf")[1]["abstained"])
        self.assertEqual(scan_store(self, self.store, gone), [])

    def test_future_ts_refused_and_captured_at_governs(self):
        future = iso(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3))
        _, out = self.cap([post("Edit", "futurecanary alpha", ts=future)])
        self.assertEqual((out["captured"], out["dropped_future"]), (0, 1))
        # an event whose own ts is ancient is retained by captured_at, not ts
        _, out = self.cap([post("Edit", "oldtscanary beta", ts="2001-01-01T00:00:00Z")], "--retain-days", "30")
        self.assertEqual((out["captured"], out["pruned"]), (1, 0))
        self.assertFalse(self.rec("oldtscanary")[1]["abstained"])


class A6KillSwitch(Base):
    def test_disabled(self):
        self.cap([post("Edit", "beforecanary alpha")])
        before = (self.store / "obs.db").read_bytes()
        (self.store / "DISABLED").write_text("")
        proc, _ = self.cap([post("Edit", "aftercanary beta")])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("capture disabled", proc.stdout)
        self.assertEqual((self.store / "obs.db").read_bytes(), before)
        proc, res = self.rec("beforecanary")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual((res["state"], res["candidates"]), ("disabled", []))

    def test_abstains(self):
        self.cap([post("Edit", "something present")])
        _, res = self.rec("nonexistentterm")
        self.assertEqual((res["state"], res["abstained"], res["candidates"]), ("ok", True, []))

    def test_no_db_yet_abstains(self):
        _, res = self.rec("anything")
        self.assertEqual((res["state"], res["abstained"]), ("ok", True))

    def test_child_guard(self):
        env = dict(self.env, AI_ARCH_JOB_MARKER="1")
        proc, out = self.cap([post("Edit", "childcanary alpha")], env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.store / "obs.db").exists())
        self.cap([post("Edit", "parentcanary alpha")])
        proc, res = self.rec("parentcanary", env=env)
        self.assertEqual((res["state"], res["candidates"]), ("disabled", []))
        # per-event subagent marker
        _, out = self.cap([post("Edit", "agentcanary alpha", agent_id="a1")])
        self.assertEqual(out["captured"], 0)
        self.assertTrue(self.rec("agentcanary")[1]["abstained"])


def build_large(store, rows):
    con = sqlite3.connect(store / "obs.db")
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    con.executemany("INSERT INTO obs VALUES(?,?,?,?,?,?,?)",
                    [(f"obs-bulk{n:08x}", "demo", "tool", "Edit", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                      f"{text} row{n}") for n in range(rows)])
    con.execute("INSERT INTO obs_fts(rowid, summary) SELECT rowid, summary FROM obs")
    con.commit()
    con.close()


class A7Timeout(Base):
    def test_progress_handler_timeout(self):
        self.cap([post("Edit", "seed alpha")])
        build_large(self.store, 300000)
        start = time.monotonic()
        proc, res = self.rec("alpha beta gamma", "--timeout", "0.05", "--limit", "50")
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((res["state"], res["candidates"]), ("timeout", []))
        self.assertLess(elapsed, 2.0)
        # the same query with room finishes (the fixture is genuinely large)
        _, res = self.rec("alpha beta gamma", "--timeout", "30", "--limit", "5")
        self.assertEqual(res["state"], "ok")
        self.assertEqual(len(res["candidates"]), 5)

    def cmd_config(self, argv, **extra):
        cfg = {"argv": argv, "timeout_s": 1, "name": "fake-tool", "version": "0.1", "license": "MIT", "network": False}
        cfg.update(extra)
        path = self.tmp / "cmd.json"
        path.write_text(json.dumps(cfg))
        return path

    def test_command_sleep_killed(self):
        cfg = self.cmd_config(["sleep", "30"])
        start = time.monotonic()
        proc = self.run_py(RECALL, "query", "--backend", "command", "--config", cfg, "--q", "alpha")
        elapsed = time.monotonic() - start
        res = json.loads(proc.stdout)
        self.assertEqual((res["state"], res["candidates"]), ("timeout", []))
        self.assertLess(elapsed, 2.0)

    def test_command_grandchild_killed(self):
        cfg = self.cmd_config(["sh", "-c", "sleep 31.7 & wait"])
        start = time.monotonic()
        proc = self.run_py(RECALL, "query", "--backend", "command", "--config", cfg, "--q", "alpha")
        elapsed = time.monotonic() - start
        self.assertEqual(json.loads(proc.stdout)["state"], "timeout")
        self.assertLess(elapsed, 2.0)
        time.sleep(0.2)
        left = [p for p in Path("/proc").glob("[0-9]*/cmdline")
                if b"sleep\x0031.7" in (p.read_bytes() if p.exists() else b"")]
        self.assertEqual(left, [])


class AuditBase(Base):
    events = []
    installed = False

    @classmethod
    def install(cls):
        if not AuditBase.installed:
            sys.addaudithook(lambda name, args: AuditBase.events.append((name, args)) if AuditBase.active else None)
            AuditBase.installed = True

    active = False

    def record(self):
        self.install()
        AuditBase.events = []
        AuditBase.active = True
        self.addCleanup(lambda: setattr(AuditBase, "active", False))

    def stop(self):
        AuditBase.active = False
        return list(AuditBase.events)

    def positive_controls(self):
        """The hook must be able to see what the assertions claim is absent."""
        with contextlib.suppress(OSError):
            socket.socket(socket.AF_UNIX).connect(str(self.tmp / "no-such.sock"))
        subprocess.run(["true"])
        (self.outside / "canary.txt").read_text()


def inproc(module, argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code or 0, out.getvalue(), err.getvalue()


class A8NoNetwork(AuditBase):
    def test_capture_recall_run_audited(self):
        observe, recall = loadmod("observe", OBSERVE), loadmod("recall", RECALL)
        memrun = loadmod("memrun", RUN)
        self.record()
        self.positive_controls()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(inproc(observe, ["capture", "--store", str(self.store), "--events", str(FIXTURE),
                                              "--scope", SCOPE])[0], 0)
            self.assertEqual(inproc(recall, ["query", "--store", str(self.store), "--q", "rounding"])[0], 0)
            self.assertEqual(inproc(memrun, ["--offline", "--workdir", str(self.mkdir("wd"))])[0], 0)
        events = self.stop()
        names = [n for n, _ in events]
        self.assertIn("socket.connect", names)  # control: the hook sees it
        net = [(n, a) for n, a in events if n in ("socket.connect", "socket.getaddrinfo")]
        self.assertEqual(len(net), 1, net)  # only the control
        subs = [a[0] if a else None for n, a in events if n == "subprocess.Popen"]
        self.assertIn("true", [str(s) for s in subs])  # control
        argvs = [a[1] for n, a in events if n == "subprocess.Popen"]
        real = [v for v in argvs if v and v[0] != "true"]
        self.assertTrue(real)
        for argv in real:
            self.assertTrue(any(str(CURATED) in str(x) for x in argv), argv)
        connects = [a[0] for n, a in events if n == "sqlite3.connect"]
        self.assertTrue(any(str(self.store) in str(p) for p in connects), connects)

    def mkdir(self, name):
        path = self.tmp / name
        path.mkdir(mode=0o700)
        return path

    def test_offline_refuses_command_before_spawn(self):
        recall = loadmod("recall", RECALL)
        cfg = self.tmp / "cmd.json"
        cfg.write_text(json.dumps({"argv": ["true"], "timeout_s": 1, "name": "t", "version": "1", "license": "MIT",
                                   "network": True}))
        self.record()
        self.positive_controls()
        code, out, _ = inproc(recall, ["query", "--backend", "command", "--config", str(cfg), "--q", "alpha",
                                       "--offline"])
        env = dict(os.environ, AI_ARCH_OFFLINE="1")
        with mock.patch.dict(os.environ, {"AI_ARCH_OFFLINE": "1"}):
            code2, out2, _ = inproc(recall, ["query", "--backend", "command", "--config", str(cfg), "--q", "alpha"])
        events = self.stop()
        self.assertEqual(code, 0)
        for text in (out, out2):
            res = json.loads(text)
            self.assertEqual((res["state"], res["reason"], res["candidates"]), ("disabled", "offline", []))
        spawned = [a[1] for n, a in events if n == "subprocess.Popen" and a[1] and a[1][0] != "true"]
        self.assertEqual(spawned, [])

    def test_command_output_echoes_backend_and_network(self):
        tool = self.tmp / "tool.py"
        tool.write_text("import json,sys\njson.load(sys.stdin)\nprint(json.dumps({'candidates':[{'excerpt':'netcanary'}]}))\n")
        cfg = self.tmp / "cmd.json"
        cfg.write_text(json.dumps({"argv": [sys.executable, str(tool)], "timeout_s": 5, "name": "fake", "version": "2",
                                   "license": "Apache-2.0", "network": True}))
        proc = self.run_py(RECALL, "query", "--backend", "command", "--config", cfg, "--q", "x")
        res = json.loads(proc.stdout)
        self.assertEqual(res["backend"], {"name": "fake", "version": "2", "license": "Apache-2.0", "network": True})
        self.assertEqual(res["state"], "ok")

    def test_model_summarizer_refused(self):
        proc = self.run_py(OBSERVE, "capture", "--store", self.store, "--events", FIXTURE, "--scope", SCOPE,
                           "--summarizer", "model")
        self.assertEqual(proc.returncode, 2)


class A9Injection(Base):
    def setUp(self):
        super().setUp()
        self.cap([post("Edit", "alpha beta"), post("Edit", "gamma delta"), post("Edit", "epsilon only here")])

    def test_hostile_queries(self):
        for q in ('" OR 1 NEAR(x', "*", "NOT a", "a" * 10000, "x* OR y", "alpha AND NOT beta", 'alpha"; DROP TABLE obs;--'):
            proc, res = self.rec(q)
            self.assertNotIn("Traceback", proc.stderr, q)
            self.assertIn(proc.returncode, (0, 2), q)
            if proc.returncode == 0:
                self.assertIn(res["state"], ("ok",))
                self.assertLess(len(res["candidates"]), 3, q)

    def test_never_matches_everything(self):
        _, res = self.rec('" OR 1 NEAR(x')
        self.assertTrue(res["abstained"])
        _, res = self.rec("*")
        self.assertIsNone(res)

    def test_query_token_cap(self):
        self.cap([post("Edit", "tokcanaryhead and tokcanarytail")])
        q = " ".join(["tokcanaryhead"] + [f"pad{n}" for n in range(100)] + ["tokcanarytail"])
        res = self.rec(q)[1]
        self.assertEqual(len(res["candidates"]), 1)
        _, res = self.rec(" ".join([f"pad{n}" for n in range(100)] + ["tokcanarytail"]))
        self.assertTrue(res["abstained"])  # beyond the ~64-token cap

    def test_bad_limit_and_timeout(self):
        for extra in (("--limit", "0"), ("--timeout", "0"), ("--timeout", "x")):
            proc, _ = self.rec("alpha", *extra)
            self.assertEqual(proc.returncode, 2, extra)

    def test_command_config_validation(self):
        for cfg in ({"argv": "echo hi", "timeout_s": 1, "name": "t", "version": "1", "license": "MIT", "network": False},
                    {"argv": ["true"], "timeout_s": 1, "name": "t", "version": "1", "license": "MIT"},
                    {"argv": ["true"], "timeout_s": 1, "name": "t", "version": "1", "network": False},
                    {"argv": [], "timeout_s": 1, "name": "t", "version": "1", "license": "MIT", "network": False},
                    {"argv": ["true"], "timeout_s": "1", "name": "t", "version": "1", "license": "MIT", "network": False}):
            path = self.tmp / "bad.json"
            path.write_text(json.dumps(cfg))
            proc = self.run_py(RECALL, "query", "--backend", "command", "--config", path, "--q", "alpha")
            self.assertEqual(proc.returncode, 2, cfg)
        path.write_text("not json")
        proc = self.run_py(RECALL, "query", "--backend", "command", "--config", path, "--q", "alpha")
        self.assertEqual(proc.returncode, 2)


class CommandBackend(Base):
    def tool(self, body, **extra):
        script = self.tmp / "tool.py"
        script.write_text(body)
        cfg = {"argv": [sys.executable, str(script)], "timeout_s": 5, "name": "fake", "version": "1", "license": "MIT",
               "network": False}
        cfg.update(extra)
        path = self.tmp / "cmd.json"
        path.write_text(json.dumps(cfg))
        return self.run_py(RECALL, "query", "--backend", "command", "--config", path, "--q", "hello world", "--limit", "2")

    def test_query_on_stdin_and_fields_stripped(self):
        body = ("import json,sys\nreq=json.load(sys.stdin)\nassert req=={'q':'hello world','limit':2},req\n"
                "print(json.dumps([{'excerpt':'one [x] (y) '+'z'*900,'ts':'2026-01-01','project':'p','citation':'curated:a#L1',"
                "'authority':'curated','trust':'trusted','id':'curated-1'},{'excerpt':'two'},{'excerpt':'three'},{'nope':1}]))\n")
        proc = self.tool(body)
        res = json.loads(proc.stdout)
        self.assertEqual(res["state"], "ok", proc.stderr)
        self.assertEqual(len(res["candidates"]), 2)
        for cand in res["candidates"]:
            self.assertEqual((cand["authority"], cand["trust"]), ("observed", "untrusted-data"))
            self.assertRegex(cand["id"], r"^obs-[0-9a-f]+$")
            self.assertNotIn("citation", cand)
            self.assertLessEqual(len(cand["excerpt"]), 400)
            self.assertNotRegex(cand["excerpt"], r"[\[\]()]")

    def test_bad_outputs_are_errors(self):
        for body in ("print('not json')", "import sys\nprint('[]')\nsys.exit(3)", "print('x'*(2<<20))",
                     "print('{\"candidates\": 5}')"):
            res = json.loads(self.tool(body).stdout)
            self.assertEqual((res["state"], res["candidates"]), ("error", []), body)

    def test_env_cleared_and_cwd_temp(self):
        body = ("import json,sys,os\njson.load(sys.stdin)\n"
                "print(json.dumps([{'excerpt':'env '+','.join(sorted(os.environ))+' cwd-has-store '+str(os.path.exists('OPT-IN'))}]))\n")
        proc = self.run_py(RECALL, "query", "--backend", "command", "--config", self.write_cfg(body), "--q", "q",
                           env=dict(self.env, SECRETENV="1"))
        text = json.loads(proc.stdout)["candidates"][0]["excerpt"]
        self.assertNotIn("SECRETENV", text)
        self.assertNotIn("HOME", text)

    def write_cfg(self, body):
        script = self.tmp / "tool2.py"
        script.write_text(body)
        path = self.tmp / "cmd2.json"
        path.write_text(json.dumps({"argv": [sys.executable, str(script)], "timeout_s": 5, "name": "f", "version": "1",
                                    "license": "MIT", "network": False}))
        return path


class A10Separation(Base):
    def run_example(self, *extra):
        wd = self.tmp / "wd"
        wd.mkdir(exist_ok=True, mode=0o700)
        return wd, self.run_py(RUN, "--offline", "--workdir", wd, *extra)

    def split(self, out):
        head, _, rest = out.partition("## curated\n")
        curated, _, episodic = rest.partition("## episodic\n")
        return curated, episodic

    def test_sections_and_byte_identity(self):
        wd, proc = self.run_example()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        curated, episodic = self.split(proc.stdout)
        direct = self.run_py(CURATED, "query", "--root", f"notes={wd / 'curated'}", "--q", "sourdough starter feeding",
                             "--format", "json")
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(curated, direct.stdout)
        cur = json.loads(curated)
        self.assertTrue(cur["hits"])
        for hit in cur["hits"]:
            self.assertIn("citation", hit)
            self.assertNotIn("authority", hit)
        epi = json.loads(episodic)
        self.assertEqual(epi["state"], "ok")
        self.assertTrue(epi["candidates"])
        for cand in epi["candidates"]:
            self.assertNotIn("citation", cand)
            self.assertEqual(cand["authority"], "observed")
        self.assertNotIn("observed", curated)

    def test_example_leaves_no_state_outside_workdir(self):
        before = sorted(os.listdir(self.home))
        self.run_example()
        self.assertEqual(sorted(os.listdir(self.home)), before)

    def test_recall_text_format_labels(self):
        self.cap(FIXTURE)
        proc = self.run_py(RECALL, "query", "--store", self.store, "--q", "feeding", "--format", "text")
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("[observed")]
        self.assertTrue(lines, proc.stdout)
        for ln in lines:
            self.assertRegex(ln, r"^\[observed · untrusted-data · obs-[0-9a-f]+\] ")


class A11Missing(Base):
    def test_no_backend(self):
        wd = self.tmp / "wd"
        wd.mkdir(mode=0o700)
        full = self.run_py(RUN, "--offline", "--workdir", wd)
        wd2 = self.tmp / "wd2"
        wd2.mkdir(mode=0o700)
        proc = self.run_py(RUN, "--offline", "--no-backend", "--workdir", wd2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        c1 = full.stdout.partition("## curated\n")[2].partition("## episodic\n")[0]
        c2 = proc.stdout.partition("## curated\n")[2].partition("## episodic\n")[0]
        self.assertEqual(c1, c2)
        epi = json.loads(proc.stdout.partition("## episodic\n")[2])
        self.assertEqual((epi["state"], epi["candidates"]), ("unavailable", []))
        self.assertIn("setup", epi)

    def fake_connect(self):
        class Con:
            def execute(self, *a):
                raise sqlite3.OperationalError("no such module: fts5")

            def close(self):
                pass
        return mock.patch.object(sqlite3, "connect", lambda *a, **k: Con())

    def test_no_fts5_run_and_capture_and_recall(self):
        observe, recall = loadmod("observe", OBSERVE), loadmod("recall", RECALL)
        memrun = loadmod("memrun", RUN)
        wd = self.tmp / "wd"
        wd.mkdir(mode=0o700)
        with self.fake_connect():
            code, out, err = inproc(memrun, ["--offline", "--workdir", str(wd)])
            self.assertEqual(code, 0, err)
            epi = json.loads(out.partition("## episodic\n")[2])
            self.assertEqual(epi["state"], "unavailable")
            self.assertIn("FTS5", epi["setup"])
            self.assertTrue(json.loads(out.partition("## curated\n")[2].partition("## episodic\n")[0])["hits"])
            code, out, err = inproc(observe, ["capture", "--store", str(self.store), "--events", str(FIXTURE),
                                              "--scope", SCOPE])
            self.assertEqual(code, 2)
            self.assertIn("FTS5", err)
            code, out, _ = inproc(recall, ["query", "--store", str(self.store), "--q", "rounding"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["state"], "unavailable")
        self.assertFalse((self.store / "obs.db").exists())


class A12NoOutside(AuditBase):
    def test_symlinks_and_traversal(self):
        link = self.tmp / "ev-link.jsonl"
        link.symlink_to(FIXTURE)
        proc = self.run_py(OBSERVE, "capture", "--store", self.store, "--events", link, "--scope", SCOPE)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((self.store / "obs.db").exists())
        # a symlinked db inside the store
        target = self.outside / "target.db"
        target.write_bytes(b"OUTSIDECANARY")
        (self.store / "obs.db").symlink_to(target)
        proc, _ = self.cap(FIXTURE)
        self.assertEqual(proc.returncode, 2)
        proc, _ = self.rec("rounding")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_bytes(), b"OUTSIDECANARY")
        # redact file symlinked
        rf = self.tmp / "rf-link"
        rf.symlink_to(self.outside / "canary.txt")
        fresh = self.mkstore("fresh")
        proc, _ = self.cap(FIXTURE, "--redact-file", rf, store=fresh)
        self.assertEqual(proc.returncode, 2)
        # relative traversal in events path
        proc = self.run_py(OBSERVE, "capture", "--store", fresh, "--events", f"{self.outside}/../outside/canary.txt",
                           "--scope", SCOPE)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.outside / "canary.txt").read_text(), "OUTSIDECANARY")

    def test_audited_opens_stay_inside(self):
        observe, recall = loadmod("observe", OBSERVE), loadmod("recall", RECALL)
        memrun = loadmod("memrun", RUN)
        wd = self.tmp / "wd"
        wd.mkdir(mode=0o700)
        self.record()
        self.positive_controls()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            inproc(observe, ["capture", "--store", str(self.store), "--events", str(FIXTURE), "--scope", SCOPE])
            inproc(recall, ["query", "--store", str(self.store), "--q", "rounding"])
            inproc(memrun, ["--offline", "--workdir", str(wd)])
        events = self.stop()
        opened = [str(a[0]) for n, a in events if n == "open" and isinstance(a[0], (str, bytes, os.PathLike))
                  and not isinstance(a[0], int)]
        outside = [p for p in opened if str(self.outside) in p]
        self.assertEqual(len(outside), 1, outside)  # only the positive control
        self.assertEqual([p for p in opened if str(self.home) in p], [])
        self.assertTrue(any(str(FIXTURE) in p for p in opened))  # control: events file seen


class A13Descriptor(Base):
    def test_descriptor(self):
        contracts = loadmod("contracts_u07", ROOT / "modules" / "contracts" / "contracts.py")
        desc = contracts.check_module_descriptor(BK, module="memory-backends")
        self.assertEqual((desc["claimed_support"], desc["evidence"]), ("planned", []))
        self.assertEqual({d["identifier"] for d in desc["dependencies"]}, {"core-contracts", "memory"})
        for dep in desc["dependencies"]:
            self.assertEqual(dep["status"], "declared-untested")

    def test_privacy_scan(self):
        scan = loadmod("privacy_scan_u07", ROOT / "scripts" / "privacy_scan.py")
        for rel in ("modules/memory-backends", "examples/memory"):
            self.assertEqual(scan.scan(ROOT / rel), [], rel)

    def test_no_private_paths_in_outputs(self):
        wd = self.tmp / "wd"
        wd.mkdir(mode=0o700)
        blobs = [self.run_py(RUN, "--offline", "--workdir", wd).stdout]
        self.cap(FIXTURE)
        forms = F2AbsolutePaths.FORMS + ["2>" + HOMEDIR + "/e.log", "<code>" + HOMEDIR + "/c</code>"]
        self.cap([post("Bash", "Formsweep " + " and ".join(forms) + " done.", path=forms[0])])
        blobs.append(self.rec("feeding")[0].stdout)
        blobs.append(self.run_py(RECALL, "query", "--store", self.store, "--q", "feeding", "--format", "text").stdout)
        _, res = self.rec("feeding rounding opened looked wrote finished formsweep")
        self.assertTrue(any("Formsweep" in c["excerpt"] for c in res["candidates"]))
        for blob in blobs:
            self.assertNotIn(str(self.tmp), blob)
            self.assertNotIn("/" + "home/", blob)
        for cand in res["candidates"]:
            self.assertNotRegex(cand["excerpt"], r"(?<![\w<])/[\w.-]+/|~|file:|Documents|plan\.txt")

    def test_db_perms_journal_mode(self):
        self.cap(FIXTURE)
        self.assertEqual(oct((self.store / "obs.db").stat().st_mode & 0o777), "0o600")
        con = sqlite3.connect(self.store / "obs.db")
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        con.close()
        self.assertEqual(sorted(os.listdir(self.store)), ["OPT-IN", "obs.db"])

    def test_no_model_or_vector_words(self):
        for path in list(BK.glob("*.py")) + [RUN]:
            self.assertIsNone(re.search(r"(?i)embedding|openai|anthropic|urllib|http\.client|socket", path.read_text()), path)


if __name__ == "__main__":
    unittest.main()
