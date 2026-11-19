import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CLI = os.environ.get("AIARCH_RECEIPT_CLI") or str(ROOT / "modules" / "review" / "receipt.py")  # mutation seam


def cli(*args):
    return subprocess.run([sys.executable, "-B", CLI, *map(str, args)], capture_output=True, text=True, timeout=60)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.a, self.b = self.tmp / "a.py", self.tmp / "b.py"
        self.a.write_text("alpha\n")
        self.b.write_text("beta\n")
        self.rc = self.tmp / "r.json"

    def issue(self, *files, verdict="PASS", reviewer="security-reviewer", out=None):
        arts = []
        for f in files:
            arts += ["--artifact", f]
        return cli("issue", *arts, "--reviewer", reviewer, "--verdict", verdict, "--out", out or self.rc)

    def test_issue_then_check_fresh_and_schema(self):
        r = self.issue(self.a, self.b)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(self.rc.read_text())
        self.assertEqual(sorted(doc), ["artifacts", "issued", "reviewer", "schema_version", "verdict"])
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual([x["path"] for x in doc["artifacts"]], [str(self.a), str(self.b)])  # absolute realpaths
        c = cli("check", "--receipt", self.rc)
        self.assertEqual((c.returncode, c.stdout.strip()), (0, "fresh"))

    def test_one_changed_byte_names_exactly_that_file(self):
        self.issue(self.a, self.b)
        st = self.a.stat()
        self.a.write_text("alpHa\n")
        os.utime(self.a, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size, same mtime: only the hash can tell
        c = cli("check", "--receipt", self.rc)
        self.assertEqual(c.returncode, 1)
        self.assertIn(str(self.a), c.stdout)
        self.assertNotIn(str(self.b), c.stdout)

    def test_deleted_file_is_stale_and_named(self):
        self.issue(self.a, self.b)
        self.b.unlink()
        c = cli("check", "--receipt", self.rc)
        self.assertEqual(c.returncode, 1)
        self.assertIn(str(self.b), c.stdout)
        self.assertNotIn(str(self.a), c.stdout)

    def test_symlink_to_identical_copy_is_stale(self):
        self.issue(self.a)
        twin = self.tmp / "twin.py"
        twin.write_text("alpha\n")
        self.a.unlink()
        self.a.symlink_to(twin)
        c = cli("check", "--receipt", self.rc)
        self.assertEqual(c.returncode, 1, c.stdout)
        self.assertIn(str(self.a), c.stdout)

    def test_check_json_format(self):
        self.issue(self.a)
        self.a.write_text("changed\n")
        c = cli("check", "--receipt", self.rc, "--format", "json")
        self.assertEqual(c.returncode, 1)
        self.assertEqual(json.loads(c.stdout)["stale"][0]["path"], str(self.a))

    def test_issue_refuses_symlink_duplicate_missing_and_bad_input(self):
        link = self.tmp / "link.py"
        link.symlink_to(self.a)
        for args in ((link,), (self.a, self.a), (self.a, self.tmp / ".." / self.tmp.name / "a.py"), (self.tmp / "nope.py",), (self.tmp,)):
            r = self.issue(*args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
        self.assertFalse(self.rc.exists())
        self.assertEqual(self.issue(self.a, verdict="MAYBE").returncode, 2)
        self.assertEqual(self.issue(self.a, reviewer="bad role!").returncode, 2)

    def test_check_refuses_unknown_schema_duplicate_keys_and_junk(self):
        self.issue(self.a)
        doc = json.loads(self.rc.read_text())
        bad = {"v2": json.dumps({**doc, "schema_version": 2}), "junk": "{not json", "str-version": json.dumps({**doc, "schema_version": "1"}),
               "dup": self.rc.read_text().replace('"verdict": "PASS"', '"verdict": "PASS", "verdict": "FAIL"'),
               "no-artifacts": json.dumps({**doc, "artifacts": []})}
        for name, text in bad.items():
            p = self.tmp / f"{name}.json"
            p.write_text(text)
            r = cli("check", "--receipt", p)
            self.assertEqual(r.returncode, 2, (name, r.stdout, r.stderr))
        self.assertEqual(cli("check", "--receipt", self.tmp / "absent.json").returncode, 2)

    def test_atomic_write_leaves_no_temp_files(self):
        self.issue(self.a)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.py", "b.py", "r.json"])

    def test_usage_error_exits_2(self):
        self.assertEqual(cli("issue").returncode, 2)
        self.assertEqual(cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
