"""Stand-in verifier (role: verifier). Runs the recorded test in its own fresh worktree; changes nothing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402

cwd = os.getcwd()
sys.stdin.read()
code, _ = c.py(os.path.join(cwd, "check_slug.py"), cwd=cwd)
c.emit({"head": c.git(cwd, "rev-parse", "HEAD").strip(), "rc": code, "test_file": "check_slug.py"})
