"""R9-U08 memory-operations: promotion, frozen snapshots plus eval, database-aware sync (A1-A12, A15 and AM1-AM16).

Disposable stores, temp HOME/XDG/TMPDIR, a PATH of fakes, no network (socket calls raise), agent env stripped.
"""

import contextlib
import errno
import getpass
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MOD = Path(__file__).resolve().parents[1]
ROOT = MOD.parents[1]
CLI = MOD / "memops.py"
BK = ROOT / "modules" / "memory-backends"
MEMCHECK = ROOT / "tools" / "memcheck" / "memcheck"
AGENT_VARS = ("AI_ARCH_JOB_MARKER", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "OPENCODE_X")


def memops_load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


observe = memops_load("observe_for_memops_tests", BK / "observe.py")
contracts = memops_load("contracts_for_memops_tests", ROOT / "modules" / "contracts" / "contracts.py")
sys.path.insert(0, str(ROOT / "scripts"))  # check_collection imports its sibling capabilities module
try:
    check_collection = memops_load("check_collection_for_memops_tests", ROOT / "scripts" / "check_collection.py")
finally:
    sys.path.remove(str(ROOT / "scripts"))
privacy = memops_load("privacy_for_memops_tests", ROOT / "scripts" / "privacy_scan.py")
memops = memops_load("memops_under_test", CLI)

# secret fixtures are assembled from fragments so no scanner flags this file; one per redaction class (12 in total)
SECRETS = {
    "private-key": "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END " + "PRIVATE KEY-----",
    "sk-key": "sk-" + "A1b2C3d4E5f6G7h8I9j0",
    "github-token": "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "slack-token": "xoxb-" + "1234567890-abcdefghij",
    "aws-key": "AKIA" + "IOSFODNN7EXAMPLE",
    "password": "password=" + "Hunter2xyzq",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "slack-webhook": "https://hooks.slack.com/services/" + "T0000000/B0000000/XXXXXXXXXXXXXXXX",
    "bearer": "Bearer " + "abcdEFGH1234ijklMNOP",
    "credential-url": "postgres://" + "dbuser:s3cretpass@db.example.org/x",
    "github_pat": "github_pat_" + "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
    "mysql-p": "mysql -u root -p" + "Sup3rSecretPw",
}
FRAGMENTS = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "A1b2C3d4E5f6G7h8I9j0", "abcdefghijklmnopqrstuvwxyz0123456789",
             "1234567890-abcdefghij", "IOSFODNN7EXAMPLE", "Hunter2xyzq", "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
             "XXXXXXXXXXXXXXXX", "abcdEFGH1234ijklMNOP", "s3cretpass", "11ABCDEFG0abcdefghijkl", "Sup3rSecretPw"]


def memops_hash(text):
    return "obs-" + hashlib.sha256(text.encode()).hexdigest()[:24]


def memops_add(db, summary, oid=None, ts="2026-01-01T00:00:00Z", captured="2026-01-01T00:00:01Z", tool="Bash",
               project="proj", kind="tool"):
    oid = oid or memops_hash(summary)
    cur = db.execute("INSERT INTO obs VALUES(?,?,?,?,?,?,?)", (oid, project, kind, tool, ts, captured, summary))
    db.execute("INSERT INTO obs_fts(rowid, summary) VALUES(?, ?)", (cur.lastrowid, summary))
    return oid


def memops_mkstore(path, summaries=(), wal_extra=()):
    """A real U07 store (0700, OPT-IN, obs.db). With wal_extra: WAL mode, those rows only in -wal, writer left open."""
    path = str(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    Path(path, "OPT-IN").write_text("")
    fd = observe.open_store(path)
    db = observe.open_db(fd, path)
    os.close(fd)
    for summary in summaries:
        memops_add(db, summary)
    db.commit()
    if not wal_extra:
        db.close()
        return None
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    for summary in wal_extra:
        memops_add(db, summary)
    db.commit()
    return db


def memops_dump(store):
    """Row dump (read-only) of a store dir or db file: the byte-independent identity of its contents."""
    path = Path(store)
    path = path / "obs.db" if path.is_dir() else path
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT id,project,kind,tool_name,ts,captured_at,summary FROM obs ORDER BY id").fetchall()
    finally:
        con.close()


def memops_ids(store):
    return {r[0] for r in memops_dump(store)}


def memops_tree(path, mtimes=False, skip=("-shm",)):
    out = {}
    base = Path(path)
    for p in sorted(base.rglob("*")):
        if p.name.endswith(skip):
            continue
        rel = p.relative_to(base).as_posix()
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            out[rel] = ("link", os.readlink(p))
        elif stat.S_ISREG(st.st_mode):
            out[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(st.st_mode), st.st_mtime_ns if mtimes else 0)
        else:
            out[rel] = ("dir", stat.S_IMODE(st.st_mode), st.st_mtime_ns if mtimes else 0)
    return out


def memops_env(tmp, extra=None):
    base = Path(tmp)
    for name in ("home", "xdg-state", "xdg-config", "tmpdir", "bin"):
        (base / name).mkdir(exist_ok=True)
    env = {"HOME": str(base / "home"), "XDG_STATE_HOME": str(base / "xdg-state"), "XDG_CONFIG_HOME": str(base / "xdg-config"),
           "TMPDIR": str(base / "tmpdir"), "PATH": f"{base / 'bin'}:/usr/bin:/bin", "LANG": "C.UTF-8"}
    env.update(extra or {})
    return env


NOTE_FM = "---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: reference\n---\n{body}\n"


def memops_root(tmp):
    root = Path(tmp) / "mroot"
    mem = root / "demo" / "memory"
    mem.mkdir(parents=True)
    (mem / "alpha-note.md").write_text(NOTE_FM.format(name="alpha-note", desc="Alpha facts.", body="Alpha body."))
    (mem / "MEMORY.md").write_text("- [Alpha Note](alpha-note.md) — Alpha facts.\n")
    return root


class MemopsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.env = memops_env(self.dir)
        env_patch = mock.patch.dict(os.environ, self.env, clear=True)  # agent variables stripped (AM10)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            patch = mock.patch(name, side_effect=AssertionError("network used"))
            patch.start()
            self.addCleanup(patch.stop)
        self.out = self.dir / "out"
        self.root = memops_root(self.dir)

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = memops.main([*argv, "--out", str(self.out)])
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 2
        return rc, out.getvalue(), err.getvalue()

    def sub(self, *argv, env=None):
        proc = subprocess.run([sys.executable, "-B", str(CLI), *argv, "--out", str(self.out)], capture_output=True, text=True,
                              env=env or self.env, cwd=self.dir, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def js(self, *argv, rc=0):
        code, out, err = self.call(*argv)
        self.assertEqual(code, rc, (out, err))
        return json.loads(out) if out.strip() else None

    def receipts(self, lane):
        path = self.out / f"{lane}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def stage(self, name="tea-notes", body="Tea facts for the cupboard.", cid="obs-aaa111", type_="reference"):
        recall = self.dir / f"recall-{name}.json"
        recall.write_text(json.dumps({"state": "ok", "candidates": [{"id": cid, "excerpt": body, "project": "p", "ts": "t"}]}))
        return self.js("promote", "stage", "--from-recall", str(recall), "--candidate-id", cid, "--name", name,
                       "--type", type_, "--description", "Tea facts.")

    def approve(self, cand):
        return self.js("promote", "approve", "--id", cand["id"], "--sha", cand["sha256"], "--approver", "reviewer-1")

    def apply(self, cid):
        return self.call("promote", "apply", "--id", cid, "--root", f"m={self.root}", "--dir", "demo/memory")

    def cand_path(self, cand):
        return self.out / "candidates" / cand["id"] / "candidate.json"

    def mem(self):
        return self.root / "demo" / "memory"


class A1Collision(MemopsCase):
    def refused_cleanly(self, cand):
        tree, receipts = memops_tree(self.root), (self.out / "promotion.jsonl").read_bytes()
        rc, out, err = self.apply(cand["id"])
        self.assertNotEqual(rc, 0, (out, err))
        self.assertEqual(memops_tree(self.root), tree)
        self.assertEqual((self.out / "promotion.jsonl").read_bytes(), receipts)

    def test_existing_file_symlink_and_symlinked_dir_refused(self):
        cand = self.stage()
        self.approve(cand)
        (self.mem() / "tea-notes.md").write_text("planted\n")
        self.refused_cleanly(cand)
        (self.mem() / "tea-notes.md").unlink()
        outside = self.dir / "outside.txt"
        outside.write_text("outside\n")
        (self.mem() / "tea-notes.md").symlink_to(outside)
        self.refused_cleanly(cand)
        self.assertEqual(outside.read_text(), "outside\n")
        (self.mem() / "tea-notes.md").unlink()
        real = self.dir / "elsewhere"
        self.mem().rename(real)
        self.mem().symlink_to(real)
        self.refused_cleanly(cand)
        self.assertEqual(sorted(p.name for p in real.iterdir()), ["MEMORY.md", "alpha-note.md"])

    def test_precheck_race_cannot_overwrite(self):  # AM5
        cand = self.stage()
        self.approve(cand)
        (self.mem() / "tea-notes.md").write_text("planted after the check\n" * 100)
        planted = (self.mem() / "tea-notes.md").read_bytes()
        with mock.patch.object(memops.promote, "target_free", return_value=True):
            rc, out, err = self.apply(cand["id"])
        self.assertNotEqual(rc, 0, (out, err))
        self.assertEqual((self.mem() / "tea-notes.md").read_bytes(), planted)
        self.assertFalse([r for r in self.receipts("promotion") if r.get("event") == "apply"])


class A2Approval(MemopsCase):
    def assertNoWrite(self, cid):
        tree = memops_tree(self.root)
        rc, out, err = self.apply(cid)
        self.assertNotEqual(rc, 0, (out, err))
        self.assertEqual(memops_tree(self.root), tree)
        self.assertFalse([r for r in self.receipts("promotion") if r.get("event") == "apply"])

    def test_no_approval(self):
        self.assertNoWrite(self.stage()["id"])

    def test_edited_body_keeps_stale_sha(self):  # AM16
        cand = self.stage()
        self.approve(cand)
        data = json.loads(self.cand_path(cand).read_text())
        data["body"] += " EDITED"
        self.assertEqual(data["sha256"], cand["sha256"])
        self.cand_path(cand).write_text(json.dumps(data))
        self.assertNoWrite(cand["id"])

    def test_approval_for_another_id(self):
        a, b = self.stage(name="note-a", cid="obs-aaa111"), self.stage(name="note-b", body="Other body.", cid="obs-bbb222")
        self.approve(b)
        self.assertNoWrite(a["id"])

    def test_approved_flag_in_candidate_is_not_approval(self):
        cand = self.stage()
        data = json.loads(self.cand_path(cand).read_text())
        data["approved"] = True
        self.cand_path(cand).write_text(json.dumps(data))
        self.assertNoWrite(cand["id"])

    def test_approve_refused_in_every_agent_env(self):  # AM10
        cand = self.stage()
        for var in AGENT_VARS:
            env = dict(self.env, **{var: "1"})
            rc, out, err = self.sub("promote", "approve", "--id", cand["id"], "--sha", cand["sha256"], "--approver", "r", env=env)
            self.assertNotEqual(rc, 0, var)
            self.assertFalse([r for r in self.receipts("promotion") if r.get("event") == "approve"], var)
        rc, _, _ = self.sub("promote", "approve", "--id", cand["id"], "--sha", cand["sha256"], "--approver", "r")
        self.assertEqual(rc, 0)

    def test_sensitive_types_need_type_confirm(self):
        cand = self.stage(type_="user")
        base = ("promote", "approve", "--id", cand["id"], "--sha", cand["sha256"], "--approver", "r")
        self.assertNotEqual(self.call(*base)[0], 0)
        self.assertEqual(self.call(*base, "--type-confirm", "user")[0], 0)


class A3Promotion(MemopsCase):
    def ready(self, **kw):
        cand = self.stage(**kw)
        self.approve(cand)
        return cand

    def test_apply_index_duplicate_and_rollback(self):
        before = (self.mem() / "MEMORY.md").read_bytes()
        cand = self.ready()
        rc, out, err = self.apply(cand["id"])
        self.assertEqual(rc, 0, (out, err))
        receipt = json.loads(out)["receipt_id"]
        note = (self.mem() / "tea-notes.md").read_bytes()
        lines = (self.mem() / "MEMORY.md").read_text().splitlines()  # AM6c
        self.assertEqual(len([ln for ln in lines if re.match(r"^- \[[^\]]+\]\(tea-notes\.md\)", ln)]), 1)
        self.assertEqual(len(lines), 2)
        report = subprocess.run([str(MEMCHECK), "--root", str(self.root)], capture_output=True, text=True, env=self.env).stdout
        self.assertNotIn("tea-notes.md", "\n".join(ln for ln in report.splitlines() if "rphan" in ln or "ot indexed" in ln))
        rc, out, err = self.apply(cand["id"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(len([r for r in self.receipts("promotion") if r.get("event") == "apply"]), 1)
        self.assertEqual((self.mem() / "tea-notes.md").read_bytes(), note)
        rc, out, err = self.call("promote", "rollback", "--receipt", receipt, "--root", f"m={self.root}")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual((self.mem() / "MEMORY.md").read_bytes(), before)
        self.assertFalse((self.mem() / "tea-notes.md").exists())

    def test_rollback_after_later_edit_refused(self):
        for victim in ("tea-notes.md", "MEMORY.md"):
            cand = self.ready(name=f"n-{victim[:3].lower()}", cid=f"obs-{victim[:3].lower()}111")
            rc, out, err = self.apply(cand["id"])
            self.assertEqual(rc, 0, (out, err))
            receipt = json.loads(out)["receipt_id"]
            target = self.mem() / (victim if victim == "MEMORY.md" else f"n-{victim[:3].lower()}.md")
            target.write_text(target.read_text() + "later edit\n")
            tree = memops_tree(self.root)
            rc, _, _ = self.call("promote", "rollback", "--receipt", receipt, "--root", f"m={self.root}")
            self.assertNotEqual(rc, 0, victim)
            self.assertEqual(memops_tree(self.root), tree)
            (self.mem() / "MEMORY.md").write_text("- [Alpha Note](alpha-note.md) — Alpha facts.\n")
            for stray in self.mem().glob("n-*.md"):
                stray.unlink()

    def test_receipt_write_failure_leaves_nothing(self):  # AM6b
        cand = self.ready()
        tree = memops_tree(self.root)
        index = (self.mem() / "MEMORY.md").read_bytes()
        real = memops.promote.append_receipt

        def failing(outfd, lane, record):
            if record.get("event") == "apply":
                raise OSError("disk full")
            return real(outfd, lane, record)

        with mock.patch.object(memops.promote, "append_receipt", side_effect=failing):
            rc, out, err = self.apply(cand["id"])
        self.assertNotEqual(rc, 0, (out, err))
        self.assertFalse((self.mem() / "tea-notes.md").exists())
        self.assertEqual((self.mem() / "MEMORY.md").read_bytes(), index)
        self.assertEqual(memops_tree(self.root), tree)

    def test_deleted_note_reapply_is_already_promoted(self):  # AM6a
        cand = self.ready()
        self.assertEqual(self.apply(cand["id"])[0], 0)
        (self.mem() / "tea-notes.md").unlink()
        rc, out, err = self.apply(cand["id"])
        self.assertNotEqual(rc, 0)
        self.assertIn("already promoted", err)
        self.assertNotIn("collision", err)


class A4Redaction(MemopsCase):
    def test_twelve_classes_by_name_never_written(self):  # AM7
        source = (MOD / "redact.py").read_text()
        self.assertFalse(re.search(r"^BUILTIN_REDACTIONS\s*=", source, re.M))
        self.assertRegex(source, r"observe\.Redactor")
        self.assertEqual(sorted(memops.redact.CLASSES), sorted(SECRETS))
        written = []
        real_write = os.write

        def spy(fd, data):
            written.append(bytes(data))
            return real_write(fd, data)

        body = " ".join(f"{name}: {secret}" for name, secret in SECRETS.items())
        with mock.patch("os.write", side_effect=spy):
            cand = self.stage(body=body)
            self.approve(cand)
            rc, out, err = self.apply(cand["id"])
        self.assertEqual(rc, 0, (out, err))
        blob = b"\n".join(written) + (self.out / "promotion.jsonl").read_bytes() + (self.mem() / "tea-notes.md").read_bytes() \
            + out.encode() + err.encode()
        for fragment in FRAGMENTS:
            self.assertNotIn(fragment.encode(), blob, fragment)
        self.assertGreaterEqual(cand["redacted_count"], 12)
        self.assertIn(b"[REDACTED]", self.cand_path(cand).read_bytes())


class SnapCase(MemopsCase):
    ROWS = ["alpha beta gamma deploy pipeline", "delta epsilon zeta rollback database", "eta theta iota kappa cache",
            "lambda mu nu xi router", "omicron pi rho sigma tau queue", "upsilon phi chi psi omega"]

    def setUp(self):
        super().setUp()
        self.live = self.dir / "live"
        memops_mkstore(self.live, self.ROWS)
        self.ids = {i: memops_hash(s) for i, s in enumerate(self.ROWS, 1)}

    def freeze(self, store=None):
        return self.js("snapshot", "freeze", "--store", str(store or self.live))

    def query(self, frozen, q):
        return self.js("snapshot", "query", "--snapshot", frozen["snapshot"], "--pin", frozen["pin"], "--q", q)

    def goldset(self, name, questions):
        path = self.dir / name
        path.write_text(json.dumps({"questions": [{"id": i, "q": q, "expect": [self.ids[e]] if e else []} for i, q, e in questions]}))
        return str(path)

    def evaluate(self, frozen, gold):
        return self.js("snapshot", "eval", "--snapshot", frozen["snapshot"], "--pin", frozen["pin"], "--goldset", gold)


GOLD8 = [("q1", "alpha beta gamma", 1), ("q2", "delta zeta rollback", 2), ("q3", "eta iota cache", 3), ("q4", "lambda router", 4),
         ("q5", "omicron queue sigma", 1), ("q6", "???", None), ("q7", "", None), ("q8", "alpha qqqq wwww eeee", 1)]


class A5FrozenIdentity(SnapCase):
    def test_freeze_is_frozen_verified_and_never_touches_live(self):
        frozen = self.freeze()
        self.assertEqual(stat.S_IMODE(os.stat(frozen["snapshot"]).st_mode), 0o444)
        pin = json.loads(Path(frozen["pin"]).read_text())
        self.assertEqual(set(pin), {"sha256", "rows", "source_store_id", "created_at"})
        self.assertEqual(pin["rows"], 6)
        self.assertEqual(pin["sha256"], hashlib.sha256(Path(frozen["snapshot"]).read_bytes()).hexdigest())
        before = self.query(frozen, "alpha beta")
        live = sqlite3.connect(self.live / "obs.db")
        memops_add(live, "alpha beta brand new capture after the freeze")
        live.commit()
        live.close()
        self.assertEqual(len(memops_dump(self.live)), 7)  # AM8: the live count rose, so "unchanged" is not vacuous
        opened = []
        real_connect = sqlite3.connect

        def spy(*a, **k):
            opened.append(str(a[0]) if a else "")
            return real_connect(*a, **k)

        os.rename(self.live, self.dir / "live-moved")
        with mock.patch("sqlite3.connect", side_effect=spy):
            after = self.query(frozen, "alpha beta")
        self.assertEqual(after["candidates"], before["candidates"])
        self.assertTrue(before["candidates"])
        self.assertFalse([o for o in opened if "live" in o], opened)

    def test_flipped_byte_is_sha_mismatch_exit_4(self):  # AM8
        frozen = self.freeze()
        copy = self.dir / "copy.db"
        data = bytearray(Path(frozen["snapshot"]).read_bytes())
        data[data.index(b"deploy pipeline")] ^= 0x01  # inside a known summary string: the database stays valid
        copy.write_bytes(bytes(data))
        self.assertEqual(sqlite3.connect(copy).execute("PRAGMA integrity_check").fetchone()[0], "ok")
        rc, out, err = self.call("snapshot", "query", "--snapshot", str(copy), "--pin", frozen["pin"], "--q", "alpha")
        self.assertEqual(rc, 4, (out, err))
        self.assertIn("sha-mismatch", err)


class A6Eval(SnapCase):
    def test_labels_summary_and_abstention(self):
        res = self.evaluate(self.freeze(), self.goldset("g8.json", GOLD8))
        labels = {q["id"]: q["label"] for q in res["questions"]}
        self.assertEqual([labels[k] for k in ("q1", "q2", "q3", "q4")], ["hit"] * 4)
        self.assertEqual(labels["q5"], "miss")
        self.assertEqual([labels[k] for k in ("q6", "q7", "q8")], ["abstain"] * 3)  # q8: 1 of 4 terms < ceil(4/2) (AM9)
        self.assertEqual(res["summary"], {"questions": 8, "answered": 5, "hits": 4, "misses": 1, "abstained": 3})
        self.assertEqual(res["status"], "ok")

    def test_six_with_two_termless_and_four_are_insufficient(self):
        frozen = self.freeze()
        six = self.evaluate(frozen, self.goldset("g6.json", [GOLD8[0], GOLD8[1], GOLD8[2], GOLD8[3], GOLD8[5], GOLD8[6]]))
        labels = {q["id"]: q["label"] for q in six["questions"]}
        self.assertEqual((labels["q6"], labels["q7"]), ("abstain", "abstain"))
        self.assertEqual(six["status"], "insufficient")
        self.assertEqual(self.evaluate(frozen, self.goldset("g4.json", GOLD8[:4]))["status"], "insufficient")

    def test_compare_differing_question_ids_is_insufficient(self):
        a = self.freeze()
        live = sqlite3.connect(self.live / "obs.db")
        memops_add(live, "zzz added later")
        live.commit()
        live.close()
        b = self.freeze()
        ra = self.evaluate(a, self.goldset("ga.json", GOLD8))
        rb = self.evaluate(b, self.goldset("gb.json", [("x" + i, q, e) for i, q, e in GOLD8]))
        rb_same = self.evaluate(b, self.goldset("gs.json", GOLD8))
        base = ("snapshot", "compare", "--a", a["snapshot"], "--a-pin", a["pin"], "--b", b["snapshot"], "--b-pin", b["pin"])
        cmp = self.js(*base, "--eval-a", ra["run"], "--eval-b", rb["run"])
        self.assertEqual(cmp["eval"]["status"], "insufficient")
        self.assertEqual(len(cmp["added"]), 1)
        self.assertEqual((cmp["removed"], cmp["changed"]), ([], []))
        self.assertEqual(cmp["basis"], "descriptive")
        same = self.js(*base, "--eval-a", ra["run"], "--eval-b", rb_same["run"])
        self.assertEqual(same["eval"]["status"], "ok")
        self.assertEqual(same["eval"]["summary_delta"]["hits"], 0)


class SyncCase(MemopsCase):
    A_ROWS = ["shared one", "shared two", "only on host a"]
    B_ROWS = ["shared one", "shared two", "bonsai only on host b"]

    def setUp(self):
        super().setUp()
        self.a, self.b = self.dir / "a", self.dir / "b"
        memops_mkstore(self.a, self.A_ROWS)
        memops_mkstore(self.b, self.B_ROWS)

    def merge(self, target, replica, apply=True, rc=0):
        argv = ["sync", "merge", "--target", str(target), "--replica", str(replica)] + (["--apply"] if apply else [])
        return self.js(*argv, rc=rc)

    def journals(self):
        return [p for p in (self.out / "sync").glob("*.json") if not p.name.endswith(".plan.json")]

    def journal(self, jid):
        return json.loads((self.out / "sync" / f"{jid}.json").read_text())

    def recall(self, store, q):
        proc = subprocess.run([sys.executable, "-B", str(BK / "recall.py"), "query", "--store", str(store), "--q", q],
                              capture_output=True, text=True, env=self.env)
        return json.loads(proc.stdout)

    def add_rows(self, store, count):
        db = sqlite3.connect(Path(store) / "obs.db")
        for n in range(count):
            memops_add(db, f"replica extra row number {n}")
        db.commit()
        db.close()


class A7Wal(SyncCase):
    def test_backup_contains_wal_only_row_and_leaves_source_alone(self):
        wal = self.dir / "walstore"
        writer = memops_mkstore(wal, ["base row"], wal_extra=["row only in the wal"])
        self.addCleanup(writer.close)
        self.assertGreater(os.path.getsize(wal / "obs.db-wal"), 0)
        marks = {n: (Path(wal, n).read_bytes(), os.stat(wal / n).st_mtime_ns) for n in ("obs.db", "obs.db-wal")}
        res = self.js("sync", "backup", "--store", str(wal))
        backup = self.out / "sync" / "backups" / f"{res['backup_id']}.db"
        self.assertIn("row only in the wal", [r[6] for r in memops_dump(backup)])
        for n, (data, mtime) in marks.items():  # AM2: main db and -wal; -shm is excluded (readers write read-marks)
            self.assertEqual(Path(wal, n).read_bytes(), data, n)
            self.assertEqual(os.stat(wal / n).st_mtime_ns, mtime, n)


class A8DryRun(SyncCase):
    def test_status_and_default_merge_change_nothing(self):
        writer = memops_mkstore(self.dir / "rwal", ["shared one", "extra wal row"], wal_extra=["wal only"])
        self.addCleanup(writer.close)
        for replica in (self.b, self.dir / "rwal"):
            snap = (memops_tree(self.a, mtimes=True), memops_tree(replica, mtimes=True))
            self.js("sync", "status", "--store", str(self.a))
            plan = self.merge(self.a, replica, apply=False)
            self.assertFalse(plan["apply"])
            self.assertEqual((memops_tree(self.a, mtimes=True), memops_tree(replica, mtimes=True)), snap)
        names = {p.name for p in (self.out / "sync").iterdir()}
        self.assertTrue(names and all(n.endswith(".plan.json") for n in names), names)

    def test_no_uri_flags_that_skip_locking(self):  # AM2
        for path in MOD.glob("*.py"):
            text = path.read_text()
            self.assertNotIn("immutable=", text, path.name)
            self.assertNotIn("nolock=", text, path.name)


class A9Reconcile(SyncCase):
    def test_pairwise_reconciliation_and_fts_rows(self):
        self.assertEqual(self.merge(self.a, self.b)["state"], "verified")
        self.assertEqual(self.merge(self.b, self.a)["state"], "verified")
        self.assertEqual(memops_ids(self.a), memops_ids(self.b))
        self.assertEqual(len(memops_ids(self.a)), 4)
        found = self.recall(self.a, "bonsai")
        self.assertEqual([c["id"] for c in found["candidates"]], [memops_hash("bonsai only on host b")])  # AM1
        self.assertEqual(self.merge(self.a, self.b)["added"], 0)

    def test_conflicts_exit_4_and_write_nothing(self):  # AM11
        columns = {"summary": "shared one, rewritten", "ts": "2027-01-01T00:00:00Z", "captured_at": "2027-01-01T00:00:09Z",
                   "tool_name": "Edit"}
        oid = memops_hash("shared one")
        for column, value in columns.items():
            db = sqlite3.connect(self.b / "obs.db")
            old = db.execute(f"SELECT {column} FROM obs WHERE id=?", (oid,)).fetchone()[0]
            db.execute(f"UPDATE obs SET {column}=? WHERE id=?", (value, oid))
            db.commit()
            db.close()
            tree = memops_tree(self.a)
            rc, out, err = self.call("sync", "merge", "--target", str(self.a), "--replica", str(self.b), "--apply")
            self.assertEqual(rc, 4, (column, out, err))
            self.assertEqual(memops_tree(self.a), tree, column)
            db = sqlite3.connect(self.b / "obs.db")
            db.execute(f"UPDATE obs SET {column}=? WHERE id=?", (old, oid))
            db.commit()
            db.close()


class A10Interrupted(SyncCase):
    def test_in_process_failure_after_k_rows(self):
        self.add_rows(self.b, 5)
        pre = memops_dump(self.a)
        real = memops.sync.insert_row
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 4:
                raise sqlite3.OperationalError("injected failure after 3 rows")
            return real(*a, **k)

        with mock.patch.object(memops.sync, "insert_row", side_effect=flaky):
            rc, out, err = self.call("sync", "merge", "--target", str(self.a), "--replica", str(self.b), "--apply")
        self.assertNotEqual(rc, 0, (out, err))
        self.assertEqual(len(calls), 4)
        self.assertEqual(memops_dump(self.a), pre)
        journals = self.journals()
        self.assertEqual(len(journals), 1)
        jid = journals[0].stem
        self.assertEqual(self.journal(jid)["state"], "backed_up")
        self.assertEqual(self.js("sync", "status", "--store", str(self.a))["state"], "interrupted")
        rec = self.js("sync", "recover", "--id", jid, "--target", str(self.a))
        self.assertEqual(rec["integrity"], "ok")
        self.assertEqual(memops_dump(self.a), pre)
        self.assertEqual(self.js("sync", "status", "--store", str(self.a))["state"], "ok")

    def test_sigkill_inside_transaction_then_capture_then_recover(self):  # AM3, AM4
        self.add_rows(self.b, 5)
        pre = memops_dump(self.a)
        ready = self.dir / "ready"
        env = dict(self.env, AI_ARCH_MEMOPS_TEST_PAUSE_AFTER="3", AI_ARCH_MEMOPS_TEST_READY=str(ready))
        proc = subprocess.Popen([sys.executable, "-B", str(CLI), "sync", "merge", "--target", str(self.a), "--replica", str(self.b),
                                 "--apply", "--out", str(self.out)], env=env, cwd=self.dir, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        try:
            for _ in range(300):
                if ready.exists() or proc.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertTrue(ready.exists(), proc.poll())
            jid = self.journals()[0].stem
            self.assertEqual(self.journal(jid)["state"], "backed_up")
            self.assertTrue((self.a / "obs.db-journal").exists())  # the hot rollback journal is there
        finally:
            proc.kill()
            proc.communicate()
        capture = sqlite3.connect(self.a / "obs.db", timeout=5)
        memops_add(capture, "a capture landing after the kill")
        capture.commit()
        capture.close()
        rec = self.js("sync", "recover", "--id", jid, "--target", str(self.a))
        self.assertEqual(rec["integrity"], "ok")
        self.assertEqual(rec["preserved_extra_rows"], 1)
        self.assertEqual(sorted(memops_dump(self.a)), sorted(pre + [r for r in memops_dump(self.a) if r[6] == "a capture landing after the kill"]))
        self.assertEqual(len(memops_dump(self.a)), len(pre) + 1)

    def _merge_with_failing_committed_write(self):
        """Merge whose `committed` journal write fails with ENOSPC after COMMIT; returns the journal id."""
        real = memops.sync.journal_write

        def full_disk(sdir, journal):
            if journal["state"] == "committed":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(sdir, journal)

        with mock.patch.object(memops.sync, "journal_write", side_effect=full_disk):
            try:
                self.call("sync", "merge", "--target", str(self.a), "--replica", str(self.b), "--apply")
            except OSError:
                pass
        jid = self.journals()[0].stem
        self.assertEqual(self.journal(jid)["state"], "backed_up")
        return jid

    def test_committed_write_fails_then_recover_confirms(self):  # security review SEV2
        jid = self._merge_with_failing_committed_write()
        merged = memops_dump(self.a)
        capture = sqlite3.connect(self.a / "obs.db", timeout=5)
        memops_add(capture, "a capture landing after the failed journal write")
        capture.commit()
        capture.close()
        self.assertEqual(self.js("sync", "status", "--store", str(self.a))["state"], "interrupted")
        rec = self.js("sync", "recover", "--id", jid, "--target", str(self.a))  # no --restore, no --accept-loss
        self.assertEqual(rec["outcome"], "merge-confirmed")
        self.assertEqual(rec["integrity"], "ok")
        after = memops_dump(self.a)
        self.assertEqual(len(after), len(merged) + 1)
        self.assertTrue(any(r[6] == "a capture landing after the failed journal write" for r in after))
        self.assertEqual(self.journal(jid)["state"], "verified")
        self.assertEqual(self.js("sync", "status", "--store", str(self.a))["state"], "ok")

    def test_genuine_partial_mismatch_still_refuses(self):
        self.add_rows(self.b, 5)
        real = memops.sync.insert_row
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 4:
                raise sqlite3.OperationalError("injected failure after 3 rows")
            return real(*a, **k)

        with mock.patch.object(memops.sync, "insert_row", side_effect=flaky):
            self.call("sync", "merge", "--target", str(self.a), "--replica", str(self.b), "--apply")
        jid = self.journals()[0].stem
        journal = self.journal(jid)
        self.assertEqual(journal["state"], "backed_up")
        con = sqlite3.connect(self.b / "obs.db")
        first = memops.sync.fetch(con)[journal["add_ids"][0]]  # (rowid, row)
        con.close()
        w = sqlite3.connect(self.a / "obs.db", isolation_level=None)
        real(w, *first)
        w.close()
        rc, out, err = self.call("sync", "recover", "--id", jid, "--target", str(self.a))
        self.assertEqual(rc, 1, (out, err))
        self.assertEqual(self.journal(jid)["state"], "backed_up")

    def test_restore_accept_loss_reports_dropped_rows(self):  # tribune F1
        self.add_rows(self.b, 5)
        real = memops.sync.insert_row
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError("injected failure after 1 row")
            return real(*a, **k)

        with mock.patch.object(memops.sync, "insert_row", side_effect=flaky):
            self.call("sync", "merge", "--target", str(self.a), "--replica", str(self.b), "--apply")
        jid = self.journals()[0].stem
        con = sqlite3.connect(self.a / "obs.db")
        memops_add(con, "late capture to be dropped")
        con.commit()
        late = [r[0] for r in con.execute("SELECT id FROM obs WHERE summary = ?", ("late capture to be dropped",))]
        con.close()
        self.assertEqual(len(late), 1)
        rc, out, err = self.call("sync", "recover", "--id", jid, "--target", str(self.a), "--restore", "--accept-loss")
        self.assertEqual(rc, 0, (out, err))
        self.assertFalse(any(r[6] == "late capture to be dropped" for r in memops_dump(self.a)))
        res = json.loads(out)
        self.assertEqual(res["dropped_extra_rows"], 1)
        self.assertEqual(res["dropped_ids"], late)
        self.assertNotIn("preserved_extra_rows", res)
        self.assertNotIn("preserved_extra_rows", out)
        rec = [r for r in self.receipts("sync") if r["event"] == "recover"][-1]
        self.assertEqual(rec["dropped_extra_rows"], 1)
        self.assertEqual(rec["dropped_ids"], late)
        self.assertNotIn("preserved_extra_rows", rec)
        self.assertNotIn("late capture", json.dumps(rec))

    def test_restore_on_planned_journal_refuses_cleanly(self):  # security review SEV3 #1
        jid = "sync-20260101T000000Z-aaaaaa"
        sid = memops.sync.store_id(str(self.a))
        for d in (self.out, self.out / "sync"):
            d.mkdir(mode=0o700, exist_ok=True)
            d.chmod(0o700)
        (self.out / "sync" / f"{jid}.json").write_text(json.dumps({"id": jid, "state": "planned", "target_id": sid}))
        rc, out, err = self.call("sync", "recover", "--id", jid, "--target", str(self.a), "--restore", "--accept-loss")
        self.assertIn(rc, (2, 4), (out, err))
        self.assertNotIn("KeyError", err)

    def test_recover_never_restores_without_flag(self):  # AM4
        jid = self.merge(self.a, self.b)["id"]
        db = sqlite3.connect(self.a / "obs.db")
        memops_add(db, "late capture after merge")
        db.commit()
        db.close()
        journal = self.journal(jid)
        journal["state"] = "committed"
        (self.out / "sync" / f"{jid}.json").write_text(json.dumps(journal))
        before = memops_dump(self.a)
        self.js("sync", "recover", "--id", jid, "--target", str(self.a))
        self.assertEqual(memops_dump(self.a), before)
        self.assertEqual(self.journal(jid)["state"], "verified")


class A11BackupRestore(SyncCase):
    def test_restore_receipts_and_lanes(self):
        pre = memops_dump(self.a)
        self.stage()
        self.js("snapshot", "freeze", "--store", str(self.b))
        res = self.merge(self.a, self.b)
        self.assertNotEqual(memops_dump(self.a), pre)
        rc, out, err = self.call("sync", "restore", "--backup", res["backup_id"], "--expect-sha", "0" * 64, "--target", str(self.a))
        self.assertEqual(rc, 4, (out, err))
        self.js("sync", "restore", "--backup", res["backup_id"], "--expect-sha", res["backup_sha256"], "--target", str(self.a))
        self.assertEqual(memops_dump(self.a), pre)
        events = [r["event"] for r in self.receipts("sync")]
        self.assertIn("merge", events)
        self.assertIn("restore", events)
        self.assertEqual({r["event"] for r in self.receipts("promotion")}, {"stage"})
        self.assertEqual({r["event"] for r in self.receipts("snapshots")}, {"freeze"})
        self.assertEqual(sorted(p.name for p in self.out.glob("*.jsonl")), ["promotion.jsonl", "snapshots.jsonl", "sync.jsonl"])


class A12NoProcessControl(SyncCase):
    def test_no_process_control_on_any_sync_path(self):
        log = self.dir / "control.log"
        for name in ("pkill", "systemctl", "kill", "killall"):
            fake = self.dir / "bin" / name
            fake.write_text(f"#!/bin/sh\necho {name} >> {log}\n")
            fake.chmod(0o755)
        with mock.patch("os.kill", side_effect=AssertionError("os.kill")), mock.patch("os.killpg", side_effect=AssertionError("killpg")):
            first = self.merge(self.a, self.b)
            self.js("sync", "status", "--store", str(self.a))
            self.js("sync", "backup", "--store", str(self.a))
            self.merge(self.b, self.a, apply=False)
            self.js("sync", "recover", "--id", first["id"], "--target", str(self.a))
            self.js("sync", "restore", "--backup", first["backup_id"], "--expect-sha", first["backup_sha256"], "--target", str(self.a))
        self.assertEqual(self.sub("sync", "status", "--store", str(self.b))[0], 0)
        self.assertFalse(log.exists())

    def test_static_scans(self):  # AM12
        sources = sorted(MOD.glob("*.py"))
        self.assertGreaterEqual(len(sources), 5)
        for path in sources:
            text = path.read_text()
            for bad in (r"\bsignal\b", r"\bos\.kill", r"\burllib\b", r"\bhttp\.client\b", r"\bsocket\b", r"\bssh\b", r"\bscp\b",
                        r"shutil\.copy\w*\([^)]*\.db"):
                self.assertIsNone(re.search(bad, text), f"{path.name}: {bad}")


class A15Descriptor(MemopsCase):
    def test_descriptor_provenance_privacy_and_outputs(self):
        descriptor = contracts.check_module_descriptor(MOD, module="memory-operations")
        self.assertEqual(descriptor["claimed_support"], "planned")
        deps = {d["identifier"]: d["status"] for d in descriptor["dependencies"]}
        self.assertEqual(deps, {"core-contracts": "declared-untested", "memory": "declared-untested",
                                "memory-backends": "declared-untested"})
        check_collection.provenance(MOD / "PROVENANCE.md")
        self.assertEqual(privacy.scan(MOD), [])
        for doc in ("README.md", "INTEGRATION.md"):
            self.assertTrue((MOD / doc).is_file())
        outputs = []
        store, replica = self.dir / "st", self.dir / "rp"
        memops_mkstore(store, ["one fact here", "two facts here"])
        memops_mkstore(replica, ["one fact here", "three facts here"])
        cand = self.stage()
        self.approve(cand)
        for argv in (("promote", "apply", "--id", cand["id"], "--root", f"m={self.root}", "--dir", "demo/memory"),
                     ("snapshot", "freeze", "--store", str(store)), ("sync", "status", "--store", str(store)),
                     ("sync", "backup", "--store", str(store)),
                     ("sync", "merge", "--target", str(store), "--replica", str(replica), "--apply")):
            rc, out, err = self.call(*argv)
            self.assertEqual(rc, 0, (argv, out, err))
            outputs += [out, err]
        for p in list(self.out.rglob("*")) + list(self.root.rglob("*")):  # AM14: stdout, stderr, --out and the memory root
            if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".log"):
                outputs.append(p.read_text(errors="replace"))
        blob = "\n".join(outputs).replace(str(self.dir), "")
        # memcheck (outside this module) writes the short host name into its own audit log by design: that one file is
        # exempt from the host-name needle only, and is still scanned for the home directory and user name.
        no_audit = "\n".join(o for o in outputs if "fix-index" not in o).replace(str(self.dir), "")
        for needle in {pwd.getpwuid(os.getuid()).pw_dir, getpass.getuser(), socket.gethostname()}:
            if len(needle) >= 3 and needle not in ("/", "/root"):
                self.assertNotIn(needle, no_audit if needle == socket.gethostname() else blob, needle)
        audit = list(self.out.glob("**/audit*.log"))
        self.assertTrue(audit, "memcheck --audit-log must point under --out (AM14)")
        self.assertFalse(list(self.root.rglob(".memcheck-audit.log")))


if __name__ == "__main__":
    unittest.main()
