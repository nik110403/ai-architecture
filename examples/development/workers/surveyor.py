"""Stand-in surveyor (role: surveyor). Reads its worktree only. Usage: surveyor.py --journey domain|audit|incident --fixtures DIR"""
import argparse
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402


def lines_of(cwd, name):
    with open(os.path.join(cwd, name), encoding="utf-8") as handle:
        return handle.read().splitlines()


def first_line(cwd, name, needle):
    return next(i for i, text in enumerate(lines_of(cwd, name), 1) if needle in text)


def domain(cwd, fixtures):
    sid = "S1"
    replay = c.py_json(os.path.join(fixtures, "check_scenario.py"), cwd, sid, cwd=cwd)
    c.emit({"finding": {
        "term": "active user", "scenario_id": sid, "expected": replay["expected"], "observed": replay["observed"],
        "glossary_ref": {"file": "glossary.md", "line": first_line(cwd, "glossary.md", "active user")},
        "code_ref": {"file": "metrics.py", "line": first_line(cwd, "metrics.py", "def is_active"), "symbol": "is_active"},
        "note": "the glossary says 30 days; the code uses a 90 day window",
    }})


def audit(cwd, fixtures):
    with open(os.path.join(cwd, "app.py"), encoding="utf-8") as handle:
        functions = [n.name for n in ast.parse(handle.read()).body if isinstance(n, ast.FunctionDef) and n.name != "connect"]
    c.emit({"map": {
        "assets": ["users table", "hidden row holding the secret value"],
        "trust_boundaries": ["caller-supplied name and role cross into the database layer"],
        "inputs": functions,
    }})


def incident(cwd, fixtures):
    facts = [{"file": "app.log", "line": i, "sha256": c.sha(text)}
             for i, text in enumerate(lines_of(cwd, "app.log"), 1) if "WARN" in text and "latency_ms=" in text]
    c.emit({"facts": facts})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journey", required=True)
    parser.add_argument("--fixtures", required=True)
    args = parser.parse_args()
    sys.stdin.read()
    {"domain": domain, "audit": audit, "incident": incident}[args.journey](os.getcwd(), args.fixtures)


if __name__ == "__main__":
    main()
