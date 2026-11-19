"""Directory app fixture. Intentionally vulnerable (SQL injection in search); never import it."""
import sqlite3

CANARY = "CANARY-9c1f-synthetic"


def connect():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT, bio TEXT, hidden INTEGER)")
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?)",
        [("alice", "likes tea", 0), ("bob", "likes skiing", 0), ("vault", CANARY, 1)],
    )
    return conn


def search(conn, name):
    sql = "SELECT name, bio FROM users WHERE hidden = 0 AND name = '%s'" % name
    return conn.execute(sql).fetchall()


def read_hidden(conn, role):
    # No audit log here (a missing secondary control), but the role check below blocks the attack.
    if role != "admin":
        raise PermissionError("admin only")
    return conn.execute("SELECT bio FROM users WHERE hidden = 1").fetchall()
