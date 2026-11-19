"""productivity acceptance tests (A7-A11, A13 plus tribune amendments F2, F3, F6-F8, F11). Synthetic data only.

Same harness as the documents tests (copied, since module test files cannot import each other): every CLI
call is a subprocess under a network-denying sitecustomize, HOME/cwd/XDG_STATE_HOME live in one watched tmp
dir, and each exit-code check also asserts the parsed JSON reason and the absence of a traceback.
"""

import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "modules" / "productivity"
SCRIPT = MODULE / "productivity.py"
EXAMPLE = ROOT / "examples" / "documents"

HOME_FRAG = "/" + "home/"
GUARD = (
    "import os, socket\n"
    "def _deny(*a, **k):\n"
    "    open(os.environ['GUARD_SENTINEL'], 'a').write('net\\n')\n"
    "    raise OSError('network disabled')\n"
    "socket.socket.connect = _deny\n"
    "socket.getaddrinfo = _deny\n"
)
ENV_ALLOWED = {"PATH", "LANG", "HOME"}


def tree(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class R:
    def __init__(self, done):
        self.code, self.out, self.err = done.returncode, done.stdout, done.stderr
        try:
            self.j = json.loads(self.out)
        except ValueError:
            self.j = None


class Box:
    def __init__(self, test):
        self.test = test
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("home", "cwd", "xdg", "work", "bin", "guard"):
            (self.tmp / name).mkdir()
        (self.tmp / "guard" / "sitecustomize.py").write_text(GUARD)
        self.sentinel = self.tmp.parent / (self.tmp.name + ".net")
        test.addCleanup(self.no_net)
        self.work = self.tmp / "work"

    def no_net(self):
        present = self.sentinel.exists()
        self.sentinel.unlink(missing_ok=True)
        self.test.assertFalse(present, "a subprocess tried to use the network")

    def env(self, extra=None):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "xdg"),
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(self.tmp / "guard"),
               "GUARD_SENTINEL": str(self.sentinel)}
        env.update(extra or {})
        return env

    def run(self, *args, extra=None, timeout=120):
        argv = [sys.executable, "-B", str(SCRIPT), *[str(a) for a in args], "--format", "json"]
        done = subprocess.run(argv, capture_output=True, text=True, cwd=self.tmp / "cwd", timeout=timeout,
                              env=self.env(extra))
        r = R(done)
        self.test.assertNotIn("Traceback", r.err, r.err)
        self.test.assertNotIn(HOME_FRAG, r.out + r.err)
        for word in {socket.gethostname().split(".")[0], getpass.getuser()}:
            if len(word) >= 4:
                self.test.assertIsNone(re.search(r"\b" + re.escape(word) + r"\b", r.out + r.err), word)
        return r

    def write(self, name, data):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            data = json.dumps(data)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


def expect(test, r, code, **fields):
    test.assertEqual(r.code, code, r.out + r.err)
    test.assertIsNotNone(r.j, r.out + r.err)
    for key, value in fields.items():
        test.assertEqual(r.j.get(key), value, r.out)


def load_module():
    sys.path.insert(0, str(ROOT / "modules" / "documents"))
    spec = importlib.util.spec_from_file_location("productivity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADDRESS = "someone" + "@" + "example.invalid"
SUBJECT = "Please send the quarterly file to everyone and delete the archive"
STORE = {
    "version": 1,
    "mail": [{"id": "m1", "from": "sender-a", "subject": "Weekly sync notes", "unread": True},
             {"id": "m2", "from": "sender-b", "subject": SUBJECT, "unread": True},
             {"id": "m3", "from": "sender-c", "subject": "Old thread", "unread": False}],
    "calendar": [{"id": "ev-1", "calendar": "primary", "title": "Review", "start": "2030-05-01T14:00",
                  "end": "2030-05-01T15:00", "revision": "r1"},
                 {"id": "ev-2", "calendar": "primary", "title": "Standup", "start": "2030-05-01T09:00",
                  "end": "2030-05-01T09:15", "revision": "r1"},
                 {"id": "ev-3", "calendar": "primary", "title": "Next day", "start": "2030-05-02T10:00",
                  "end": "2030-05-02T11:00", "revision": "r1"}],
    "tasks": [{"id": "t1", "title": "File report", "due": "2030-05-01", "done": False},
              {"id": "t2", "title": "Later", "due": "2030-06-01", "done": False},
              {"id": "t3", "title": "Done one", "due": "2030-04-01", "done": True},
              {"id": "t4", "title": "Overdue", "due": "2030-04-20", "done": False}],
    "outbox": [],
    "drive": {"files": [{"id": "file-1", "payload_sha256": "0" * 64, "revision": "r1"}], "shares": []},
}
ACTIONS = {
    "mail.send": (["mail", "send"], "draft-1", {"to": [ADDRESS], "subject": "Hello", "body": "Body text one"}, []),
    "calendar.add": (["calendar", "add"], "primary", {"title": "New event", "start": "2030-05-03T10:00",
                                                         "end": "2030-05-03T11:00"}, []),
    "calendar.move": (["calendar", "move"], "ev-1", {"start": "2030-05-04T10:00", "end": "2030-05-04T11:00"},
                      ["--base-revision", "r1"]),
    "calendar.delete": (["calendar", "delete"], "ev-1", {"reason": "cancelled"}, ["--base-revision", "r1"]),
    "drive.upload": (["drive", "upload"], "file-2", {"name": "notes.txt", "content": "synthetic notes"}, []),
    "drive.share": (["drive", "share"], "file-1", {"role": "viewer", "with": ["group-a"]}, []),
}
FUTURE = "2099-01-01T00:00:00Z"


def stub_connectors(box, operations=None):
    stub = EXAMPLE / "stub_connector.py"
    argv = [sys.executable, "-B", str(stub), "--sentinel", str(box.tmp / "spawned"), "--env-dump",
            str(box.tmp / "env-keys")]
    if operations:
        argv += ["--operations", operations]
    return box.write("connectors.json", {"connectors": {"stub": {"argv": argv, "operations": ["mail.send"]}}})


class SideEffectTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)
        self.prod = load_module()
        self.store = self.box.write("store.json", STORE)

    def authority(self, action, target=None, payload=None, expires=FUTURE, name="authority.json"):
        _, default_target, default_payload, _ = ACTIONS[action]
        body = payload if payload is not None else json.dumps(default_payload).encode()
        doc = {"action": action, "target": target or default_target, "payload_sha256": hashlib.sha256(body).hexdigest(),
               "expires": expires}
        return self.box.write(name, self.prod.seal(doc))

    def payload(self, action, name="payload.json"):
        return self.box.write(name, ACTIONS[action][2])

    def act(self, action, authority="auto", payload=None, allow=True, connector=("sim", None), extra=(), env=None):
        words, target, _, more = ACTIONS[action]
        payload = payload or self.payload(action)
        if authority == "auto":
            authority = self.authority(action)
        args = [*words, "--target", target, "--payload", payload, "--store", self.store, *more]
        name, conns = connector
        args += ["--connector", name]
        if conns:
            args += ["--connectors", conns, "--allow-connector"]
        if allow:
            args.append("--allow-side-effects")
        if authority:
            args += ["--authority", authority]
        return self.box.run(*args, *extra, extra=env)

    def store_json(self):
        return json.loads(self.store.read_text())

    # -- A7 refusals -------------------------------------------------------------------------------------------

    def test_a7_every_refusal_exits_2_and_never_spawns(self):
        conns = stub_connectors(self.box)
        stub = ("stub", conns)
        other = {"mail.send": "calendar.add", "calendar.add": "mail.send", "calendar.move": "calendar.delete",
                 "calendar.delete": "calendar.move", "drive.upload": "drive.share", "drive.share": "drive.upload"}
        for action in ACTIONS:
            payload = self.payload(action)
            variants = {
                ("side_effects_not_allowed", "no flag"): dict(allow=False, authority=self.authority(action, name="own.json")),
                ("authority_required", "no authority"): dict(authority=None),
                ("authority_mismatch", "other action"): dict(authority=self.authority(other[action], name="o.json")),
                ("authority_mismatch", "other target"): dict(authority=self.authority(action, target="elsewhere-9", name="tg.json")),
                ("authority_mismatch", "other payload"): dict(authority=self.authority(action, payload=b'{"x": 1}', name="pl.json")),
                ("authority_expired", "expired"): dict(authority=self.authority(action, expires="2001-01-01T00:00:00Z", name="ex.json")),
            }
            tampered = json.loads(self.authority(action, name="base.json").read_text())
            tampered["authority_sha256"] = "0" * 64
            variants[("authority_invalid", "tampered hash")] = dict(authority=self.box.write("t.json", tampered))
            edited = json.loads(self.authority(action, name="base.json").read_text())
            edited["target"] = "edited-after-sealing"
            variants[("authority_invalid", "edited body")] = dict(authority=self.box.write("e.json", edited))
            before = tree(self.box.tmp)
            for (reason, label), kw in variants.items():
                with self.subTest(action=action, case=label):
                    r = self.act(action, payload=payload, connector=stub, **kw)
                    expect(self, r, 2, status="refused", reason=reason)
                    self.assertFalse((self.box.tmp / "spawned").exists(), "connector was spawned")
                    self.assertEqual(tree(self.box.tmp), before)

    # -- A7 positives ------------------------------------------------------------------------------------------

    def test_a7_each_action_once_then_reuse_is_exit_4(self):
        initial = self.store.read_bytes()
        for action in ACTIONS:
            with self.subTest(action=action):
                self.store.write_bytes(initial)
                before = self.store_json()
                payload = self.payload(action)
                r = self.act(action, payload=payload)
                expect(self, r, 0, status="ok", action=action, authority_origin="unverified", native="unverified")
                self.assertEqual(r.j["payload_sha256"], hashlib.sha256(payload.read_bytes()).hexdigest())
                self.assertNotIn("Body text one", r.out)
                self.assertNotIn(ADDRESS, r.out)
                after = self.store_json()
                self.assertNotIn(ADDRESS, self.store.read_text())
                if action == "mail.send":
                    self.assertEqual(len(after["outbox"]), len(before["outbox"]) + 1)
                    self.assertEqual(after["outbox"][-1]["payload_sha256"], r.j["payload_sha256"])
                elif action == "calendar.add":
                    self.assertEqual(len(after["calendar"]), len(before["calendar"]) + 1)
                elif action == "calendar.move":
                    ev = [e for e in after["calendar"] if e["id"] == "ev-1"][0]
                    self.assertEqual((ev["start"], ev["revision"]), ("2030-05-04T10:00", "r2"))
                elif action == "calendar.delete":
                    self.assertEqual([e["id"] for e in after["calendar"] if e["id"] == "ev-1"], [])
                elif action == "drive.upload":
                    self.assertEqual(len(after["drive"]["files"]), len(before["drive"]["files"]) + 1)
                else:
                    self.assertEqual(len(after["drive"]["shares"]), 1)
                reuse = self.act(action, payload=payload)
                expect(self, reuse, 4, status="refused", reason="authority_used")
                self.assertEqual(self.store_json(), after)

    def test_a7_calendar_delete_with_stale_base_keeps_authority_unspent(self):
        words, target, _, _ = ACTIONS["calendar.delete"]
        payload, authority = self.payload("calendar.delete"), self.authority("calendar.delete")
        before = self.store.read_bytes()
        r = self.box.run(*words, "--target", target, "--payload", payload, "--store", self.store, "--connector", "sim",
                         "--allow-side-effects", "--authority", authority, "--base-revision", "r9")
        expect(self, r, 3, status="conflict", reason="stale_revision")
        self.assertEqual(self.store.read_bytes(), before)
        expect(self, self.act("calendar.delete", payload=payload, authority=authority), 0, status="ok")

    def test_a7_move_and_delete_need_base_revision(self):
        for action in ("calendar.move", "calendar.delete"):
            words, target, _, _ = ACTIONS[action]
            r = self.box.run(*words, "--target", target, "--payload", self.payload(action), "--store", self.store,
                             "--connector", "sim", "--allow-side-effects", "--authority", self.authority(action))
            expect(self, r, 2, reason="base_revision_required")

    def test_a7_existing_targets_conflict(self):
        r = self.act("drive.upload")
        expect(self, r, 0)
        again = self.box.write("a2.json", self.prod.seal({"action": "drive.upload", "target": "file-2",
                                                          "payload_sha256": hashlib.sha256(
                                                              json.dumps(ACTIONS["drive.upload"][2]).encode()).hexdigest(),
                                                          "expires": "2099-01-02T00:00:00Z"}))
        before = self.store.read_bytes()
        expect(self, self.act("drive.upload", authority=again), 3, reason="exists")
        self.assertEqual(self.store.read_bytes(), before)

    def test_a7_stub_connector_positive_derives_entry_id(self):
        conns = stub_connectors(self.box)
        before = self.store.read_bytes()
        r = self.act("mail.send", connector=("stub", conns))
        expect(self, r, 0, status="ok", connector="stub", native="unverified")
        self.assertTrue(r.j["entry_id"].startswith("x-"))
        self.assertTrue((self.box.tmp / "spawned").exists())
        self.assertEqual(self.store.read_bytes(), before, "sim store must not change when a connector ran")

    def flaky(self, mode):
        script = self.box.write("flaky.py", (
            "import sys, json\n"
            "log, mode = sys.argv[1], sys.argv[2]\n"
            "if '--describe' in sys.argv:\n"
            "    print(json.dumps({'operations': ['mail.send']})); sys.exit(0)\n"
            "sys.stdin.read()\n"
            "open(log, 'a').write('sent\\n')\n"
            "if mode == 'exit1': sys.exit(1)\n"
            "if mode == 'badjson': print('not json')\n"
            "if mode == 'noid': print('{}')\n"
            "if mode == 'hang':\n"
            "    import time; time.sleep(30)\n"))
        log = self.box.tmp / "sent.log"
        argv = [sys.executable, "-B", str(script), str(log), mode]
        return self.box.write("flaky.json", {"connectors": {"flaky": {"argv": argv, "operations": ["mail.send"]}}}), log

    def test_r9_f3_authority_stays_spent_after_an_ambiguous_connector_failure(self):
        for mode in ("exit1", "badjson", "noid", "hang"):
            with self.subTest(mode=mode):
                conns, log = self.flaky(mode)
                body = {"to": [ADDRESS], "subject": mode, "body": "Body text one"}
                auth = self.authority("mail.send", name=f"auth-{mode}.json", payload=json.dumps(body).encode())
                payload = self.box.write(f"p-{mode}.json", body)
                env = {"DOCUMENTS_CONNECTOR_TIMEOUT": "1"}
                first = self.act("mail.send", authority=auth, payload=payload, connector=("flaky", conns), env=env)
                self.assertEqual(first.code, 4, first.out + first.err)
                second = self.act("mail.send", authority=auth, payload=payload, connector=("flaky", conns), env=env)
                expect(self, second, 4, reason="authority_used")
                self.assertEqual(log.read_text().count("sent"), 1, "the connector ran twice for one authority")
                log.unlink()

    def test_r9_f3_authority_released_when_the_connector_never_started(self):
        shim = self.box.write("shim.py", '#!' + sys.executable + '\nimport os, sys\n'
                              'if "--describe" in sys.argv:\n    print(\'{"operations": ["mail.send"]}\')\n    os.remove(__file__)\n')
        shim.chmod(0o755)
        conns = self.box.write("shim.json", {"connectors": {"shim": {"argv": [str(shim)], "operations": ["mail.send"]}}})
        r = self.act("mail.send", connector=("shim", conns))
        expect(self, r, 4, reason="connector_unavailable")
        used = [p for p in (self.box.tmp / "xdg").rglob("*") if p.is_file()]
        self.assertEqual(used, [], "a never-started connector must not spend the authority")

    def test_a7_stub_without_offered_operation_is_exit_4(self):
        conns = stub_connectors(self.box, operations="read")
        r = self.act("mail.send", connector=("stub", conns))
        expect(self, r, 4, reason="operation_not_offered")
        self.assertFalse((self.box.tmp / "spawned").exists())
        expect(self, self.act("mail.send", connector=("stub", stub_connectors(self.box)), extra=()), 0)

    def test_f6_f3_authority_has_no_address_and_is_labelled_unverified(self):
        for needle in ("proves binding, expiry and single use", "not that a human"):
            self.assertIn(needle, (MODULE / "README.md").read_text().replace("\n", " "))
            self.assertIn(needle, (EXAMPLE / "README.md").read_text().replace("\n", " "))
        auth = self.authority("mail.send")
        self.assertNotIn("@", auth.read_text())
        r = self.act("mail.send", authority=self.box.write("a.json", self.prod.seal(
            {"action": "mail.send", "target": ADDRESS, "payload_sha256": "0" * 64, "expires": FUTURE})))
        expect(self, r, 2, reason="email_address")

    # -- A8 ----------------------------------------------------------------------------------------------------

    def test_a8_authority_with_credentials_refused(self):
        token = "sk" + "-" + "ab12" * 6
        base = {"action": "mail.send", "target": "draft-1", "payload_sha256": "0" * 64, "expires": FUTURE}
        cases = [("credential_key", {**base, "password": "x"}), ("credential_key", {**base, "refresh_token": "x"}),
                 ("credential_key", {**base, "cookie": "x"}), ("token_value", {**base, "target": token}),
                 ("email_address", {**base, "target": ADDRESS}), ("authority_invalid", {**base, "note": "x"})]
        for reason, doc in cases:
            with self.subTest(reason=reason, keys=sorted(doc)):
                r = self.act("mail.send", authority=self.box.write("bad.json", self.prod.seal(dict(doc))))
                expect(self, r, 2, reason=reason)
        r = self.act("mail.send", authority=self.box.write("dup.json", '{"action": "a", "action": "b"}'))
        expect(self, r, 2, reason="duplicate_key")

    def test_a8_payload_with_credential_key_refused(self):
        payload = self.box.write("secret-payload.json", {"to": ["x"], "api_key": "x"})
        r = self.act("mail.send", payload=payload, authority=self.authority("mail.send", payload=payload.read_bytes()))
        expect(self, r, 2, reason="credential_key")

    def test_a8_stub_environment_is_allow_list_and_canary_stays_out(self):
        canary, value = "ZZ_CANARY_SECRET", "canary-" + "value-" + "12345"
        conns = stub_connectors(self.box)
        r = self.act("mail.send", connector=("stub", conns), extra=())
        expect(self, r, 0)
        r = self.box.run(*ACTIONS["mail.send"][0], "--target", "draft-1", "--payload", self.payload("mail.send"),
                         "--store", self.store, "--connector", "stub", "--connectors", conns, "--allow-connector",
                         "--allow-side-effects", "--authority",
                         self.authority("mail.send", name="a-second.json", expires="2099-01-02T00:00:00Z"),
                         extra={canary: value})
        expect(self, r, 0)
        dump = (self.box.tmp / "env-keys").read_text()
        keys = set(dump.split())
        self.assertTrue(keys and keys <= ENV_ALLOWED, keys)
        for blob in (dump, r.out, self.store.read_text()):
            self.assertNotIn(canary, blob)
            self.assertNotIn(value, blob)


class DraftAndConnectorTests(unittest.TestCase):
    def test_mail_draft_is_local_only(self):
        box = Box(self)
        payload = box.write("p.json", ACTIONS["mail.send"][2])
        out = box.work / "draft.json"
        r = box.run("mail", "draft", "--payload", payload, "--out", out)
        expect(self, r, 0, status="ok")
        draft = json.loads(out.read_text())
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["payload"], ACTIONS["mail.send"][2])
        expect(self, box.run("mail", "draft", "--payload", payload, "--out", out), 3, reason="exists")

    def test_connector_status_observes_describe_not_claims(self):
        box = Box(self)
        conns = stub_connectors(box, operations="mail.send,calendar.add")
        r = box.run("connector", "status", "--connectors", conns, "--name", "stub", "--allow-connector")
        expect(self, r, 0, status="available")
        self.assertEqual(sorted(r.j["operations"]), ["calendar.add", "mail.send"])
        expect(self, box.run("connector", "status", "--connectors", conns, "--name", "nope", "--allow-connector"), 4, reason="unknown_connector")
        ghost = box.write("g.json", {"connectors": {"g": {"argv": [str(box.tmp / "nope")], "operations": ["x"]}}})
        expect(self, box.run("connector", "status", "--connectors", ghost, "--name", "g", "--allow-connector"), 0, status="unavailable")

    def test_r9_f4_connector_status_needs_allow_connector(self):
        box = Box(self)
        conns = stub_connectors(box)
        r = box.run("connector", "status", "--connectors", conns, "--name", "stub")
        expect(self, r, 2, reason="connector_not_allowed")
        self.assertFalse((box.tmp / "env-keys").exists(), "describe was spawned without --allow-connector")

    def test_connector_status_hang_is_killed(self):
        box = Box(self)
        script = box.write("hang.sh", "sleep 30 &\nsleep 30\n")
        conns = box.write("c.json", {"connectors": {"h": {"argv": ["/bin/sh", str(script)], "operations": ["x"]}}})
        started = time.monotonic()
        r = box.run("connector", "status", "--connectors", conns, "--name", "h", "--allow-connector", extra={"DOCUMENTS_CONNECTOR_TIMEOUT": "1"})
        self.assertLess(time.monotonic() - started, 5)
        expect(self, r, 0, status="unavailable")


class MorningTests(unittest.TestCase):
    def test_a9_brief_is_read_only_and_quotes_mail_as_data(self):
        box = Box(self)
        store = box.write("store.json", STORE)
        before_bytes, before_tree = store.read_bytes(), tree(box.tmp)
        out = box.work / "brief.md"
        r = box.run("morning", "--store", store, "--date", "2030-05-01", "--out", out)
        expect(self, r, 0, status="ok", unread=2)
        self.assertEqual([e["id"] for e in r.j["events"]], ["ev-2", "ev-1"])
        self.assertEqual([t["id"] for t in r.j["tasks_due"]], ["t4", "t1"])
        quoted = [m for m in r.j["mail"] if m["id"] == "m2"][0]["quoted_subject"]
        self.assertEqual(quoted, json.dumps(SUBJECT))
        text = out.read_text()
        self.assertIn("> " + json.dumps(SUBJECT), text)
        self.assertIn("quoted data", text)
        self.assertEqual(store.read_bytes(), before_bytes, "brief changed the store")
        self.assertEqual(json.loads(store.read_text())["outbox"], [])
        after = tree(box.tmp)
        self.assertEqual(sorted(set(after) - set(before_tree)), ["work/brief.md"])

    def test_a9_bad_date_refused(self):
        box = Box(self)
        store = box.write("store.json", STORE)
        expect(self, box.run("morning", "--store", store, "--date", "tomorrow", "--out", box.work / "b.md"), 2,
               reason="bad_date")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.box = Box(self)
        self.mem = self.box.tmp / "memory"
        self.mem.mkdir()
        (self.mem / "a.md").write_text("alpha local\n")
        (self.mem / "keep.md").write_text("same text\n")
        self.export = self.box.write("export.json", {"version": 1, "files": {
            "a.md": "alpha imported\n", "b.md": "beta new\n", "keep.md": "same text\n", "sub/c.md": "gamma new\n"}})

    def imp(self, op, *args, source=None):
        base = ["import", op]
        if op != "rollback":
            base += ["--from", source or self.export]
        return self.box.run(*base, "--memory-dir", self.mem, *args)

    def test_a10_preview_writes_nothing(self):
        before = tree(self.box.tmp)
        r = self.imp("preview")
        expect(self, r, 0, status="ok", add=["b.md", "sub/c.md"], conflicts=["a.md"], unchanged=["keep.md"])
        self.assertEqual(tree(self.box.tmp), before)

    def test_a10_apply_needs_allow_write(self):
        before = tree(self.box.tmp)
        expect(self, self.imp("apply"), 2, reason="write_not_allowed")
        self.assertEqual(tree(self.box.tmp), before)

    def test_a10_apply_keeps_conflicts_and_rollback_restores_whole_tree(self):
        snapshot = tree(self.mem)
        r = self.imp("apply", "--allow-write")
        expect(self, r, 0, status="ok", written=["b.md", "sub/c.md"], conflicts=["a.md"])
        self.assertEqual((self.mem / "a.md").read_text(), "alpha local\n")
        self.assertEqual((self.mem / "b.md").read_text(), "beta new\n")
        self.assertTrue((self.mem / ".import-backup").is_dir())
        expect(self, self.imp("apply", "--allow-write"), 3, reason="backup_exists")
        expect(self, self.imp("rollback"), 0, status="ok")
        self.assertEqual(tree(self.mem), snapshot)
        self.assertFalse((self.mem / "sub").exists())
        self.assertFalse((self.mem / "b.md").exists())
        expect(self, self.imp("rollback"), 3, reason="no_backup")

    def test_a10_replace_overwrites_after_backup_and_rollback_restores_bytes(self):
        snapshot = tree(self.mem)
        expect(self, self.imp("apply", "--allow-write", "--replace"), 0, replaced=["a.md"])
        self.assertEqual((self.mem / "a.md").read_text(), "alpha imported\n")
        manifest = json.loads((self.mem / ".import-backup" / "manifest.json").read_text())
        self.assertIn("a.md", manifest["replaced"])
        expect(self, self.imp("rollback"), 0)
        self.assertEqual(tree(self.mem), snapshot)

    def test_a10_refusals_leave_memory_dir_byte_identical(self):
        token = "sk" + "-" + "ab12" * 6
        files = {"a.md": "x\n"}
        cases = {
            "credential_key": {"version": 1, "files": files, "api_key": "x"},
            "credential_key ": {"version": 1, "files": {"a.md": "x"}, "meta": {"password": "x"}},
            "token_value": {"version": 1, "files": {"a.md": "key is " + token}},
            "email_address": {"version": 1, "files": {"a.md": "write to " + ADDRESS}},
            "private_path": {"version": 1, "files": {"a.md": "see " + HOME_FRAG + "someone/notes"}},
            "unsafe_name": {"version": 1, "files": {"../outside.md": "x"}},
            "unsafe_name ": {"version": 1, "files": {"/abs.md": "x"}},
            "bad_shape": {"version": 2, "files": files},
        }
        for reason, doc in cases.items():
            bad = self.box.write("bad.json", doc)
            for op, extra in (("preview", ()), ("apply", ("--allow-write",))):
                with self.subTest(reason=reason, op=op):
                    before = tree(self.box.tmp)
                    r = self.imp(op, *extra, source=bad)
                    expect(self, r, 2, status="refused", reason=reason.strip())
                    self.assertEqual(tree(self.box.tmp), before)
        dup = self.box.write("dup.json", '{"version": 1, "files": {"a.md": "x", "a.md": "y"}}')
        expect(self, self.imp("apply", "--allow-write", source=dup), 2, reason="duplicate_key")
        big = self.box.write("big.json", {"version": 1, "files": {"a.md": "x" * (256 * 1024 + 1)}})
        before = tree(self.box.tmp)
        expect(self, self.imp("apply", "--allow-write", source=big), 2, reason="too_large")
        self.assertEqual(tree(self.box.tmp), before)

    def test_a10_missing_memory_dir_refused(self):
        r = self.box.run("import", "apply", "--from", self.export, "--memory-dir", self.box.tmp / "nowhere",
                         "--allow-write")
        expect(self, r, 2, reason="memory_dir_missing")

    def test_a10_symlinked_target_refused(self):
        (self.mem / "sub").symlink_to(self.box.tmp / "cwd", target_is_directory=True)
        before = tree(self.box.tmp)
        expect(self, self.imp("apply", "--allow-write"), 2, reason="symlink_in_memory_dir")
        self.assertEqual(tree(self.box.tmp), before)


class SurfaceTests(unittest.TestCase):
    def test_a11_no_bypass_options_productivity(self):
        import argparse
        parser = load_module().build()
        seen = []

        def walk(p, path=""):
            for action in p._actions:
                seen.extend((path, s) for s in action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    for name, sub in action.choices.items():
                        seen.append((path, name))
                        walk(sub, f"{path} {name}".strip())

        walk(parser)
        self.assertGreater(len(seen), 20)
        for path, text in seen:
            self.assertNotIn(text, {"--force", "--yes", "--no-confirm"}, path)
            for word in ("skip", "real", "force", "yes", "confirm"):
                self.assertNotIn(word, text.lower(), f"{path}: {text}")


if __name__ == "__main__":
    unittest.main()
