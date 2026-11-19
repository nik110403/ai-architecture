"""Frozen observation snapshots, abstaining evaluation and descriptive comparison.

A snapshot is a sqlite3 backup of obs.db taken from a read-only connection, stored 0444 with a pin of its sha256. Every
read verifies the pin over the whole file, then loads the bytes into memory (deserialize); the live store is never opened
by query, eval or compare.
"""

import importlib.util
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
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
curated = _load("memops_curated", HERE.parent / "memory" / "curated.py")
Refuse = common.Refuse

SHA = r"[0-9a-f]{64}"
MAX_SNAPSHOT = 512 << 20
MAX_TERMS = 64
TOP = 8
COLUMNS = "id, project, kind, tool_name, ts, captured_at, summary"
SUMMARY_KEYS = ("questions", "answered", "hits", "misses", "abstained")


def store_db(store):
    """Validate the store (0700, owned, OPT-IN, not a symlink) and return its obs.db path."""
    try:
        os.close(recall.open_store(store))
    except recall.Refuse as error:
        raise Refuse(str(error)) from None
    db = os.path.join(store, "obs.db")
    try:
        if not stat.S_ISREG(os.lstat(db).st_mode):
            raise OSError
    except OSError:
        raise Refuse("store has no regular obs.db") from None
    return db


def terms(text):
    seen = []
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        token = token[:64]
        if token not in seen:
            seen.append(token)
        if len(seen) >= MAX_TERMS:
            break
    return seen


# ---- freeze ---------------------------------------------------------------------------------------------------

def freeze(args, outfd, out):
    db_path = store_db(args.store)
    sdir = common.subdir(outfd, "snapshots")
    temp = f".tmp-{secrets.token_hex(4)}.db"
    temp_path = os.path.join(out, "snapshots", temp)
    try:
        src = sqlite3.connect(common.ro_uri(db_path), uri=True, timeout=5)
        dst = sqlite3.connect(temp_path)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise Refuse("the copy failed its integrity check", 1)
            rows = dst.execute("SELECT count(*) FROM obs").fetchone()[0]
        finally:
            src.close()
            dst.close()
        data = common.read_at(sdir, temp, MAX_SNAPSHOT)
        sha = common.sha256(data)
        name = f"obs-{sha[:12]}"
        if os.path.exists(os.path.join(out, "snapshots", name + ".db")):
            os.unlink(temp, dir_fd=sdir)
        else:
            os.replace(temp, name + ".db", src_dir_fd=sdir, dst_dir_fd=sdir)
            os.chmod(name + ".db", 0o444, dir_fd=sdir)
        pin = {"sha256": sha, "rows": rows, "source_store_id": common.sha256(os.path.realpath(args.store).encode())[:16],
               "created_at": common.now()}
        try:
            common.write_new_at(sdir, name + ".pin.json", (json.dumps(pin, indent=2, sort_keys=True) + "\n").encode())
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temp, dir_fd=sdir)
        except FileNotFoundError:
            pass
        os.close(sdir)
    common.append_receipt(outfd, "snapshots", {"event": "freeze", "sha256": sha, "rows": rows, "snapshot": name + ".db"})
    base = os.path.join(out, "snapshots", name)
    return {"snapshot": base + ".db", "pin": base + ".pin.json", "sha256": sha, "rows": rows}


# ---- reading a pinned snapshot --------------------------------------------------------------------------------

def read_snapshot(snapshot, pin_path):
    """Verify the whole file against the pin, then load it into memory. Never opens the live store."""
    try:
        pin = json.loads(common.read_file(pin_path, 1 << 16))
        sha = pin["sha256"]
    except (ValueError, KeyError, TypeError):
        raise Refuse("pin is missing or malformed") from None
    common.check_pattern(sha, SHA, "pin sha256")
    data = common.read_file(snapshot, MAX_SNAPSHOT)
    if common.sha256(data) != sha:
        raise Refuse("sha-mismatch: the snapshot bytes do not match the pin", 4)
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(data)
        con.execute("SELECT count(*) FROM obs").fetchone()
    except sqlite3.Error:
        con.close()
        raise Refuse("the snapshot is not a readable observation database", 1) from None
    return con, sha


def matches(con, query_terms):
    """Distinct-term matches per id, kept when at least ceil(THRESHOLD * n) of the query terms match (curated's rule)."""
    need = math.ceil(curated.THRESHOLD * len(query_terms))
    counts = {}
    for term in query_terms:
        for (oid,) in con.execute("SELECT o.id FROM obs_fts JOIN obs o ON o.rowid = obs_fts.rowid WHERE obs_fts MATCH ?",
                                  (f'"{term}"',)):
            counts[oid] = counts.get(oid, 0) + 1
    return sorted((i for i, c in counts.items() if c >= need), key=lambda i: (-counts[i], i))


def query(args, outfd, out):
    con, sha = read_snapshot(args.snapshot, args.pin)
    try:
        query_terms = terms(args.q)
        if not query_terms:
            raise Refuse("--q has no searchable terms")
        ids = matches(con, query_terms)[:max(1, min(args.limit, 50))]
        cands = []
        for oid in ids:
            row = con.execute("SELECT project, ts, summary FROM obs WHERE id = ?", (oid,)).fetchone()
            cands.append(recall.candidate(oid, *row))
    finally:
        con.close()
    return {"snapshot_sha256": sha, "candidates": cands, "abstained": not cands}


# ---- eval -----------------------------------------------------------------------------------------------------

def load_goldset(path):
    try:
        data = json.loads(common.read_file(path, 1 << 20))
        questions = data["questions"]
        ok = isinstance(questions, list) and questions
        ids = [q["id"] for q in questions]
        ok = ok and len(set(ids)) == len(ids) and all(
            isinstance(q["id"], str) and isinstance(q["q"], str) and isinstance(q["expect"], list)
            and all(isinstance(e, str) for e in q["expect"]) for q in questions)
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise Refuse("goldset must be {questions:[{id,q,expect:[ids]}]} with unique ids")
    return questions, common.sha256(common.read_file(path, 1 << 20))


def label(con, question):
    query_terms = terms(question["q"])
    if not query_terms:
        return "abstain"  # nothing to search for: abstain, never a hit
    found = matches(con, query_terms)
    if not found:
        return "abstain"
    return "hit" if set(question["expect"]) & set(found[:TOP]) else "miss"


def evaluate(args, outfd, out):
    if args.min_questions < 1:
        raise Refuse("--min-questions must be at least 1")
    con, sha = read_snapshot(args.snapshot, args.pin)
    questions, gold_sha = load_goldset(args.goldset)
    try:
        labels = [{"id": q["id"], "label": label(con, q)} for q in questions]
    finally:
        con.close()
    hits = sum(1 for q in labels if q["label"] == "hit")
    misses = sum(1 for q in labels if q["label"] == "miss")
    summary = {"questions": len(labels), "answered": hits + misses, "hits": hits, "misses": misses,
               "abstained": sum(1 for q in labels if q["label"] == "abstain")}
    run = {"snapshot_sha256": sha, "goldset_sha256": gold_sha, "min_questions": args.min_questions,
           "status": "ok" if summary["answered"] >= args.min_questions else "insufficient",
           "summary": summary, "questions": labels}
    blob = (json.dumps(run, indent=2, sort_keys=True) + "\n").encode()
    name = f"eval-{common.sha256(blob)[:12]}.json"
    sdir = common.subdir(outfd, "snapshots")
    try:
        try:
            common.write_new_at(sdir, name, blob)
        except FileExistsError:
            pass
    finally:
        os.close(sdir)
    common.append_receipt(outfd, "snapshots", {"event": "eval", "snapshot_sha256": sha, "goldset_sha256": gold_sha,
                                               "status": run["status"], "summary": summary, "run": name})
    return {**run, "run": os.path.join(out, "snapshots", name)}


# ---- compare --------------------------------------------------------------------------------------------------

def load_run(path, snapshot_sha):
    try:
        run = json.loads(common.read_file(path, 1 << 20))
        ok = run["snapshot_sha256"] == snapshot_sha and run["status"] in ("ok", "insufficient")
        ids = [q["id"] for q in run["questions"]]
        [run["summary"][k] for k in SUMMARY_KEYS]
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise Refuse("eval run is malformed or belongs to another snapshot")
    return run


def rows_by_id(con):
    return {r[0]: r[1:] for r in con.execute(f"SELECT {COLUMNS} FROM obs")}


def compare(args, outfd, out):
    con_a, sha_a = read_snapshot(args.a, args.a_pin)
    con_b, sha_b = read_snapshot(args.b, args.b_pin)
    try:
        rows_a, rows_b = rows_by_id(con_a), rows_by_id(con_b)
    finally:
        con_a.close()
        con_b.close()
    result = {"basis": "descriptive", "note": "descriptive comparison of two frozen snapshots; no ranking, no winner",
              "a_sha256": sha_a, "b_sha256": sha_b, "added": sorted(set(rows_b) - set(rows_a)),
              "removed": sorted(set(rows_a) - set(rows_b)),
              "changed": sorted(i for i in set(rows_a) & set(rows_b) if rows_a[i] != rows_b[i])}
    if bool(args.eval_a) != bool(args.eval_b):
        raise Refuse("--eval-a and --eval-b go together")
    if args.eval_a:
        run_a, run_b = load_run(args.eval_a, sha_a), load_run(args.eval_b, sha_b)
        labels_a = {q["id"]: q["label"] for q in run_a["questions"]}
        labels_b = {q["id"]: q["label"] for q in run_b["questions"]}
        if set(labels_a) != set(labels_b) or "insufficient" in (run_a["status"], run_b["status"]):
            result["eval"] = {"status": "insufficient", "reason": "different question ids or too few answered questions"}
        else:
            result["eval"] = {"status": "ok",
                              "summary_delta": {k: run_b["summary"][k] - run_a["summary"][k] for k in SUMMARY_KEYS},
                              "changed_questions": sorted(i for i in labels_a if labels_a[i] != labels_b[i])}
    else:
        result["eval"] = {"status": "not-requested"}
    common.append_receipt(outfd, "snapshots", {"event": "compare", "a_sha256": sha_a, "b_sha256": sha_b,
                                               "added": len(result["added"]), "removed": len(result["removed"]),
                                               "changed": len(result["changed"]), "eval": result["eval"]["status"]})
    return result
