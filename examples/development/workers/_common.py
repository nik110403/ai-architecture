"""Shared helpers for the authored stand-in workers. They read fixtures only: no network, no model."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXED_DATE = "2026-01-01T00:00:00Z"
HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "gc.auto=0"]


def git_env():
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE,
    }


def git(cwd, *argv, binary=False):
    proc = subprocess.run(["git", "-C", cwd, *HARDEN, *argv], capture_output=True, env=git_env(), check=True)
    return proc.stdout if binary else proc.stdout.decode("utf-8")


def commit_all(cwd, message):
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD").strip()


def py(script, *args, cwd):
    """Run a python script isolated; return (returncode, stdout)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-B", "-I", script, *map(str, args)], cwd=cwd, env=env, capture_output=True,
                          text=True)
    return proc.returncode, proc.stdout


def py_json(script, *args, cwd):
    return json.loads(py(script, *args, cwd=cwd)[1])


def extract(cwd, commit):
    """The commit's tree in a new temp dir (caller removes it with TemporaryDirectory semantics)."""
    tmp = tempfile.TemporaryDirectory(prefix="worker-")
    data = git(cwd, "archive", "--format=tar", commit, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(tmp.name, filter="data")
    return tmp


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def emit(obj):
    print(json.dumps(obj, sort_keys=True))


def prompt():
    return json.loads(sys.stdin.read())


def load_contract(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
