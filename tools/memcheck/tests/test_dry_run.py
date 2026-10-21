"""Dry-run must not mutate the tree or invoke filesystem-writing helpers."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class DryRunTests(unittest.TestCase):
    def test_no_creation_or_writes(self):
        for fixture in ("scan", "shorten"):
            for options in ([], ["--fix-index"], ["--fix-index", "--shorten"]):
                with (
                    self.subTest(fixture=fixture, options=options),
                    tempfile.TemporaryDirectory() as td,
                ):
                    root = Path(td) / "root"
                    shutil.copytree(HERE / "fixtures" / fixture, root)
                    shims = Path(td) / "bin"
                    shims.mkdir()
                    for command in ("mktemp", "flock", "mkdir", "mv", "rm"):
                        shim = shims / command
                        shim.write_text(
                            "#!/bin/sh\necho unexpected mutation helper >&2\nexit 99\n"
                        )
                        shim.chmod(0o755)

                    def state():
                        return {
                            str(p.relative_to(root)): (
                                p.stat().st_mtime_ns,
                                p.stat().st_ctime_ns,
                                p.read_bytes() if p.is_file() else None,
                            )
                            for p in (root, *root.rglob("*"))
                        }

                    before = state()
                    result = subprocess.run(
                        [str(HERE.parent / "memcheck"), "--root", str(root), *options],
                        env={
                            **os.environ,
                            "PATH": str(shims) + ":" + os.environ["PATH"],
                        },
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(state(), before)
                    if "--fix-index" in options and (
                        fixture == "scan" or "--shorten" in options
                    ):
                        self.assertIn("MEMORY.md ::", result.stdout)


if __name__ == "__main__":
    unittest.main()
