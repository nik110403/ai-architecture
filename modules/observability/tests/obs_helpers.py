"""Shared helpers for the observability tests: isolated HOME/XDG, in-process CLI runner, tree hashing."""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
ROOT = MOD.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.observability import observe  # noqa: E402


class Isolated(unittest.TestCase):
    """Every test runs with HOME, XDG_STATE_HOME and XDG_CONFIG_HOME in a temporary directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.t = Path(self._tmp.name).resolve()
        for name in ("home", "state", "config"):
            (self.t / name).mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.t / "home"), "XDG_STATE_HOME": str(self.t / "state"),
                                             "XDG_CONFIG_HOME": str(self.t / "config")})
        patch.start()
        self.addCleanup(patch.stop)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = observe.main([str(a) for a in argv])
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None)

    def write_json(self, name, doc):
        path = self.t / name
        path.write_text(json.dumps(doc))
        return path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_state(root):
    """(relative path, sha256 or None, mtime_ns, mode) for every entry below root, sorted."""
    out = []
    for p in sorted(Path(root).rglob("*")):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None
        out.append((p.relative_to(root).as_posix(), digest, st.st_mtime_ns, st.st_mode))
    return out


PRICES = {"provider/model-a": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
          "provider/model-nocache": {"input": 1, "output": 2}}
