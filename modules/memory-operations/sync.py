"""Database-aware sync of observation stores: status, backup, merge (dry-run by default), recover and restore.

A database file, its -wal or its -shm is never copied as bytes. Reads go through a read-only SQLite connection, copies
through the sqlite3 backup API, and a merge is one BEGIN IMMEDIATE transaction that unions rows by id and writes each
full-text row at the target's own rowid. A journal under <out>/sync steps planned, backed_up, committed, verified. Nothing
here starts, stops or signals a process.

Conflict columns (same id, any difference): project, kind, tool_name, ts, captured_at, summary. captured_at is compared
strictly: the same event captured at two instants on two hosts is a conflict to resolve by hand, never a silent winner.
"""

import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


common = _load("memops_common", HERE / "common.py")
recall = _load("memops_recall", HERE.parent / "memory-backends" / "recall.py")
Refuse = common.Refuse

SYNC_ID = r"sync-\d{8}T\d{6}Z-[0-9a-f]{6}"
BACKUP_ID = r"bk-[0-9a-f]{12}"
SHA = r"[0-9a-f]{64}"
COLUMNS = ("id", "project", "kind", "tool_name", "ts", "captured_at", "summary")
DONE = ("verified", "recovered")


def store_db(store):
    try:
        os.close(recall.open_store(store))
    except recall.Refuse as error:
        raise Refuse(str(error)) from None
    path = os.path.join(store, "obs.db")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError
    except OSError:
        raise Refuse("store has no regular obs.db") from None
    return path


def replica_db(arg):
    """A replica is a store directory or a plain obs.db file (for example a backup you transported yourself)."""
    path = os.path.join(arg, "obs.db") if os.path.isdir(arg) else arg
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError
    except OSError:
        raise Refuse("replica is not a regular database file") from None
    return path


def store_id(store):
    return common.sha256(os.path.realpath(store).encode())[:16]


def backup_to(src_uri, dst_path):
    """Copy a database through the backup API (WAL contents included); the copy is a self-contained DELETE-mode file."""
    src = sqlite3.connect(src_uri, uri=True, timeout=5)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        src.close()
        dst.close()


def fetch(con):
    """id -> (rowid, row tuple of the compared columns)."""
    return {r[1]: (r[0], r[1:]) for r in con.execute("SELECT rowid, " + ", ".join(COLUMNS) + " FROM obs")}


def diff(replica_rows, target_rows):
    add, conflicts = [], []
    for oid, (rowid, row) in sorted(replica_rows.items()):
        if oid not in target_rows:
            add.append((rowid, row))
        elif target_rows[oid][1] != row:
            conflicts.append(oid)
    return add, conflicts


def insert_row(w, replica_rowid, row):
    cur = w.execute("INSERT INTO obs(" + ", ".join(COLUMNS) + ") VALUES(?,?,?,?,?,?,?)", row)
    w.execute("INSERT INTO obs_fts(rowid, summary) VALUES(?, ?)", (cur.lastrowid, row[6]))


def check_integrity(con):
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        return False
    try:
        con.execute("INSERT INTO obs_fts(obs_fts, rank) VALUES('integrity-check', 1)")
    except sqlite3.DatabaseError:
        return False
    return True


def journal_write(sdir, journal):
    common.replace_at(sdir, journal["id"] + ".json", (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode())


def journal_read(sdir, jid):
    try:
        return json.loads(common.read_at(sdir, jid + ".json"))
    except (OSError, ValueError):
        raise Refuse("no such journal") from None


def _pause_for_test(count):
    """Test-only hook: with both variables set, announce readiness after `count` inserts and wait to be killed."""
    after, ready = os.environ.get("AI_ARCH_MEMOPS_TEST_PAUSE_AFTER"), os.environ.get("AI_ARCH_MEMOPS_TEST_READY")
    if after and ready and count == int(after):
        Path(ready).write_text("ready")
        while True:
            time.sleep(0.2)


# ---- status, backup -------------------------------------------------------------------------------------------

def open_journals(outfd, sid):
    try:
        sdir = os.open("sync", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=outfd)
    except OSError:
        return []
    try:
        pending = []
        for name in sorted(os.listdir(sdir)):
            if name.endswith(".json") and not name.endswith(".plan.json") and re.fullmatch(SYNC_ID + r"\.json", name):
                journal = json.loads(common.read_at(sdir, name))
                if journal.get("target_id") == sid and journal.get("state") not in DONE:
                    pending.append(journal["id"])
        return pending
    finally:
        os.close(sdir)


def status(args, outfd, out):
    db = store_db(args.store)
    result = {"integrity": "unreadable", "rows": None}
    try:
        con = sqlite3.connect(common.ro_uri(db), uri=True, timeout=5)
        try:
            result["rows"] = con.execute("SELECT count(*) FROM obs").fetchone()[0]
            result["integrity"] = "ok" if con.execute("PRAGMA quick_check").fetchone()[0] == "ok" else "damaged"
        finally:
            con.close()
    except sqlite3.Error:
        pass
    pending = open_journals(outfd, store_id(args.store))
    result.update(state="interrupted" if pending else "ok", interrupted=pending)
    return result


def backup_file(outfd, out, src_uri, label_dir="backups"):
    """Backup-API copy into <out>/sync/backups/bk-<sha12>.db; returns (id, sha256, rows)."""
    bdir = common.subdir(outfd, "sync", label_dir)
    temp = f".tmp-{secrets.token_hex(4)}.db"
    try:
        backup_to(src_uri, os.path.join(out, "sync", label_dir, temp))
        data = common.read_at(bdir, temp, 1 << 30)
        sha = common.sha256(data)
        con = sqlite3.connect(common.ro_uri(os.path.join(out, "sync", label_dir, temp)), uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            rows = con.execute("SELECT count(*) FROM obs").fetchone()[0]
        finally:
            con.close()
        if not ok:
            raise Refuse("the backup failed its integrity check", 1)
        name = "bk-" + sha[:12]
        os.replace(temp, name + ".db", src_dir_fd=bdir, dst_dir_fd=bdir)
        return name, sha, rows
    finally:
        try:
            os.unlink(temp, dir_fd=bdir)
        except FileNotFoundError:
            pass
        os.close(bdir)


def backup(args, outfd, out):
    db = store_db(args.store)
    bid, sha, rows = backup_file(outfd, out, common.ro_uri(db))
    common.append_receipt(outfd, "sync", {"event": "backup", "backup_id": bid, "sha256": sha, "rows": rows,
                                          "target_id": store_id(args.store)})
    return {"backup_id": bid, "sha256": sha, "rows": rows}


# ---- merge ----------------------------------------------------------------------------------------------------

def merge(args, outfd, out):
    tdb, rdb = store_db(args.target), replica_db(args.replica)
    jid = f"sync-{common.now().replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
    sdir = common.subdir(outfd, "sync")
    work = tempfile.mkdtemp(prefix=".tmp-", dir=os.path.join(out, "sync"))
    try:
        replica_copy = os.path.join(work, "replica.db")
        backup_to(common.ro_uri(rdb), replica_copy)
        rcon = sqlite3.connect(common.ro_uri(replica_copy), uri=True)
        try:
            if [r[1] for r in rcon.execute("PRAGMA table_info(obs)")] != list(COLUMNS):
                raise Refuse("replica is not an observation database")
            replica_rows = fetch(rcon)
        finally:
            rcon.close()
        replica_sha = common.sha256(common.read_file(replica_copy, 1 << 30))
        sid = store_id(args.target)
        if not args.apply:
            return plan_only(args, outfd, sdir, jid, tdb, replica_rows, replica_sha, sid)
        return apply_merge(outfd, out, sdir, jid, tdb, replica_rows, replica_sha, sid)
    finally:
        os.close(sdir)
        shutil.rmtree(work, ignore_errors=True)


def plan_only(args, outfd, sdir, jid, tdb, replica_rows, replica_sha, sid):
    con = sqlite3.connect(common.ro_uri(tdb), uri=True, timeout=5)
    try:
        add, conflicts = diff(replica_rows, fetch(con))
    finally:
        con.close()
    plan = {"id": jid, "apply": False, "target_id": sid, "replica_sha256": replica_sha, "add": [r[1][0] for r in add],
            "conflicts": conflicts}
    common.write_new_at(sdir, jid + ".plan.json", (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode())
    common.append_receipt(outfd, "sync", {"event": "plan", "id": jid, "add": len(add), "conflicts": len(conflicts)})
    if conflicts:
        raise Refuse(f"conflict: {len(conflicts)} id(s) differ between the stores; nothing would be written", 4)
    return {"id": jid, "apply": False, "add": len(add), "conflicts": 0}


def apply_merge(outfd, out, sdir, jid, tdb, replica_rows, replica_sha, sid):
    journal = {"id": jid, "state": "planned", "target_id": sid, "replica_sha256": replica_sha, "created_at": common.now()}
    journal_write(sdir, journal)
    w = sqlite3.connect(tdb, timeout=5, isolation_level=None)
    in_tx = False
    try:
        w.execute("PRAGMA busy_timeout=5000")
        w.execute("BEGIN IMMEDIATE")  # the lock is held from here until COMMIT or ROLLBACK
        in_tx = True
        target_rows = fetch(w)
        add, conflicts = diff(replica_rows, target_rows)
        if conflicts:
            w.execute("ROLLBACK")
            in_tx = False
            os.unlink(jid + ".json", dir_fd=sdir)
            common.append_receipt(outfd, "sync", {"event": "conflict", "id": jid, "conflicts": len(conflicts)})
            raise Refuse(f"conflict: {len(conflicts)} id(s) differ between the stores; nothing was written", 4)
        bid, bsha, brows = backup_file(outfd, out, common.ro_uri(tdb))  # pre-sync copy, taken under the same lock
        journal.update(state="backed_up", backup_id=bid, backup_sha256=bsha, pre_rows=brows,
                       add_ids=[r[1][0] for r in add])
        journal_write(sdir, journal)
        for count, (rowid, row) in enumerate(add, 1):
            insert_row(w, rowid, row)
            _pause_for_test(count)
        w.execute("COMMIT")
        in_tx = False
        journal["state"] = "committed"
        journal_write(sdir, journal)
        expected = set(target_rows) | {r[1][0] for r in add}
        if not check_integrity(w) or {r[0] for r in w.execute("SELECT id FROM obs")} != expected:
            raise Refuse("post-merge verification failed; run recover", 1)
        journal["state"] = "verified"
        journal_write(sdir, journal)
        common.append_receipt(outfd, "sync", {"event": "merge", "id": jid, "added": len(add), "backup_id": bid,
                                              "backup_sha256": bsha, "replica_sha256": replica_sha, "state": "verified"})
        return {"id": jid, "apply": True, "state": "verified", "added": len(add), "backup_id": bid, "backup_sha256": bsha}
    except Refuse:
        if in_tx:
            _rollback(w)
        raise
    except BaseException as error:
        if in_tx:
            _rollback(w)
        try:
            common.append_receipt(outfd, "sync", {"event": "merge-failed", "id": jid, "error": type(error).__name__})
        except OSError:
            pass
        raise
    finally:
        w.close()


def _rollback(w):
    try:
        w.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---- recover, restore -----------------------------------------------------------------------------------------

def recover(args, outfd, out):
    common.check_pattern(args.id, SYNC_ID, "--id")
    tdb = store_db(args.target)
    sdir = common.subdir(outfd, "sync")
    try:
        journal = journal_read(sdir, args.id)
        if journal.get("target_id") != store_id(args.target):
            raise Refuse("the journal belongs to another store")
        if journal["state"] in DONE:
            return {"id": args.id, "state": journal["state"], "outcome": "nothing-to-do", "integrity": "ok",
                    "preserved_extra_rows": 0}
        pre_ids, backup_path, dropped = set(), None, None
        if journal.get("backup_id"):
            common.check_pattern(journal["backup_id"], BACKUP_ID, "journal backup id")
            backup_path = os.path.join(out, "sync", "backups", journal["backup_id"] + ".db")
            if common.sha256(common.read_file(backup_path, 1 << 30)) != journal["backup_sha256"]:
                raise Refuse("sha-mismatch: the pre-sync backup does not match its journal", 4)
        w = sqlite3.connect(tdb, timeout=5, isolation_level=None)  # a read-write open lets SQLite roll a hot journal back
        try:
            integrity = check_integrity(w)
            target = {r[0]: r[1:] for r in w.execute("SELECT id, project, kind, tool_name, ts, captured_at, summary FROM obs")}
        finally:
            w.close()
        pre_rows = {}
        if backup_path:
            bcon = sqlite3.connect(common.ro_uri(backup_path), uri=True)
            try:
                pre_rows = {r[0]: r[1:] for r in bcon.execute("SELECT id, project, kind, tool_name, ts, captured_at, summary FROM obs")}
            finally:
                bcon.close()
        pre_ids, add_ids = set(pre_rows), set(journal.get("add_ids", []))
        extra = [i for i in target if i not in pre_ids and i not in add_ids]  # rows from neither the backup nor the replica
        missing = [i for i in pre_ids if i not in target or target[i] != pre_rows[i]]
        applied = add_ids & set(target)
        state = journal["state"]
        if state == "committed":
            ok = integrity and not missing and applied == add_ids
            outcome = "merge-confirmed"
        elif add_ids and applied == add_ids:  # COMMIT landed but the `committed` journal write did not (kill, ENOSPC)
            ok = integrity and not missing
            outcome = "merge-confirmed"
        else:
            ok = integrity and not missing and not applied
            outcome = "nothing-applied"
        if getattr(args, "restore", False):
            if not journal.get("backup_id"):
                raise Refuse("this journal has no backup yet (state planned); there is nothing to restore", 4)
            if extra and not args.accept_loss:
                raise Refuse(f"restoring would drop {len(extra)} row(s) captured since; pass --accept-loss to proceed", 4)
            restore_backup(outfd, out, tdb, journal["backup_id"], journal["backup_sha256"])
            ok, outcome, dropped = True, "restored", sorted(extra)
        if not ok:
            raise Refuse(f"recovery check failed (integrity={'ok' if integrity else 'bad'}, missing={len(missing)}, "
                         f"partially applied={len(applied) if outcome != 'merge-confirmed' else 0}); the journal is left as it was", 1)
        journal.update(state="verified" if outcome == "merge-confirmed" else "recovered", outcome=outcome)
        journal_write(sdir, journal)
    finally:
        os.close(sdir)
    fields = {"dropped_extra_rows": len(dropped), "dropped_ids": dropped} if dropped is not None else {"preserved_extra_rows": len(extra)}
    common.append_receipt(outfd, "sync", {"event": "recover", "id": args.id, "outcome": outcome, **fields})
    return {"id": args.id, "state": journal["state"], "outcome": outcome, "integrity": "ok", **fields}


def restore_backup(outfd, out, tdb, bid, expect_sha):
    common.check_pattern(bid, BACKUP_ID, "--backup")
    common.check_pattern(expect_sha, SHA, "--expect-sha")
    path = os.path.join(out, "sync", "backups", bid + ".db")
    if common.sha256(common.read_file(path, 1 << 30)) != expect_sha:
        raise Refuse("sha-mismatch: the backup does not match --expect-sha", 4)
    src = sqlite3.connect(common.ro_uri(path), uri=True)
    dst = sqlite3.connect(tdb, timeout=5)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Refuse("the restored database failed its integrity check", 1)
        return dst.execute("SELECT count(*) FROM obs").fetchone()[0]
    finally:
        src.close()
        dst.close()


def restore(args, outfd, out):
    tdb = store_db(args.target)
    rows = restore_backup(outfd, out, tdb, args.backup, args.expect_sha)
    common.append_receipt(outfd, "sync", {"event": "restore", "backup_id": args.backup, "sha256": args.expect_sha, "rows": rows,
                                          "target_id": store_id(args.target)})
    return {"restored": args.backup, "rows": rows}
