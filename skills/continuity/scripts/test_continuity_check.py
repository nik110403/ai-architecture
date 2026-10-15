import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOOL = HERE / "continuity_check.py"


def run(tracker, receipt, *args):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(TOOL),
            "--tracker",
            str(tracker),
            "--receipt",
            str(receipt),
            *args,
        ],
        capture_output=True,
        text=True,
    )


class ContinuityCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tracker = self.root / "TRACKER.md"
        self.receipt = self.root / "receipt.json"
        self.tracker.write_text(
            "# Topic\n\n## Resume here\n\nState: saved.\n- **Next:** run tests.\n",
            encoding="utf-8",
        )

    def write_receipt(self, **updates):
        raw = self.tracker.read_bytes()
        value = {
            "schema_version": 1,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime_ns": self.tracker.stat().st_mtime_ns,
            "edit_observed": True,
            "active_jobs": [],
            "arrivals": [],
            "banked_arrivals": [],
            "usage_known": True,
        }
        value.update(updates)
        self.receipt.write_text(json.dumps(value), encoding="utf-8")

    def report(self, *args):
        result = run(self.tracker, self.receipt, "--format", "json", *args)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result, json.loads(result.stdout)

    def test_positive_json_text_and_no_mutation(self):
        self.write_receipt()
        before = (self.tracker.read_bytes(), self.receipt.read_bytes())
        result, value = self.report()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(value["status"], "CONSISTENT")
        self.assertIn("not authenticated facts", value["declaration"])
        text = run(self.tracker, self.receipt)
        self.assertEqual(text.returncode, 0)
        self.assertIn("continuity-check: CONSISTENT", text.stdout)
        self.assertEqual(before, (self.tracker.read_bytes(), self.receipt.read_bytes()))

    def test_hash_and_mtime_mismatch(self):
        self.write_receipt(sha256="0" * 64, mtime_ns=0)
        result, value = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(value["reasons"], sorted(value["reasons"]))
        self.assertIn("tracker SHA-256 does not match receipt", value["reasons"])
        self.assertIn("tracker mtime_ns does not match receipt", value["reasons"])

    def test_resume_and_next_are_bounded_to_actual_section(self):
        cases = {
            "# Topic\n": "missing ## Resume here heading",
            "## Resume here\n\n# Later\n- **Next:** history\n": "Resume here block is empty",
            "## Resume here\n\nState only.\n# Later\n- **Next:** history\n": "Resume here block has no nonempty Next",
            "## Resume here\n\n- **Next:**    \n": "Resume here block has no nonempty Next",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.tracker.write_text(text, encoding="utf-8")
                self.write_receipt()
                _, value = self.report()
                self.assertIn(expected, value["reasons"])

    def test_snapshot_blockers(self):
        self.write_receipt(
            edit_observed=False,
            usage_known=False,
            active_jobs=["job-b", "job-a"],
            arrivals=["a", "b"],
            banked_arrivals=["a"],
        )
        result, value = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertIn("tracker edit was not observed", value["reasons"])
        self.assertIn("usage is unknown", value["reasons"])
        self.assertIn("active jobs remain: job-a, job-b", value["reasons"])
        self.assertIn("unbanked arrivals remain: b", value["reasons"])

    def test_fenced_examples_do_not_supply_resume_or_next(self):
        cases = [
            (
                "# Topic\n```markdown\n## Resume here\n- **Next:** old example\n```\n",
                "missing ## Resume here heading",
            ),
            (
                "~~~markdown\n## Resume here\n- **Next:** old example\n~~~\n",
                "missing ## Resume here heading",
            ),
            (
                "## Resume here\nState\n```markdown\n- **Next:** example\n```\n",
                "Resume here block has no nonempty Next",
            ),
            (
                "## Resume here\nState\n   ~~~~markdown\n- **Next:** example\n   ~~~~\n",
                "Resume here block has no nonempty Next",
            ),
            (
                "## Resume here\nState\n````\n```\n- **Next:** still code\n````\n",
                "Resume here block has no nonempty Next",
            ),
            (
                "## Resume here\nState\n~~~~\n```\n~~~\n~~~~ trailing text\n- **Next:** still code\n",
                "Resume here block has no nonempty Next",
            ),
            (
                "## Resume here\nState\n  ## History\n- **Next:** old action\n",
                "Resume here block has no nonempty Next",
            ),
        ]
        for text, reason in cases:
            with self.subTest(text=text):
                self.tracker.write_bytes(text.encode("utf-8"))
                self.write_receipt()
                result, value = self.report()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(value["status"], "UNREADY")
                self.assertIn(reason, value["reasons"])

    def test_markdown_whitespace_fence_boundaries_and_nested_sections(self):
        cases = [
            "## Resume here\r\nState\r\n- **Next:** go\r\n",
            "   ##\tResume here\t##  \nState\n- **Next:** go\n",
            "## Resume here\nState\n### Detail\n- **Next:** go\n",
            "## Resume here\n```markdown\n## Fake boundary\n```\n- **Next:** go\n",
            "```\n## Resume here\n```\n## Resume here\n- **Next:** go\n",
            "## Resume here\n   ~~~markdown\n## Fake boundary\n  ~~~~~\t\n- **Next:** go\n",
            "## Resume here\n````\nexample\n`````  \n- **Next:** go\n",
            "    ```\n## Resume here\n- **Next:** go\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                raw = text.encode("utf-8")
                self.tracker.write_bytes(raw)
                self.write_receipt()
                result, value = self.report()
                self.assertEqual(result.returncode, 0, value)
                self.assertEqual(value["status"], "CONSISTENT")
                self.assertEqual(self.tracker.read_bytes(), raw)

    def test_crlf_hash_is_not_normalized(self):
        raw = b"## Resume here\r\nState\r\n- **Next:** go\r\n"
        self.tracker.write_bytes(raw)
        self.write_receipt(
            sha256=hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        )
        result, value = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(value["reasons"], ["tracker SHA-256 does not match receipt"])

    def test_invalid_schema_duplicates_bool_integer_malformed_and_deep(self):
        invalids = [
            "{}",
            '{"schema_version": 1, "schema_version": 1}',
            json.dumps(
                {
                    "schema_version": True,
                    "sha256": "0" * 64,
                    "mtime_ns": 0,
                    "edit_observed": True,
                    "active_jobs": [],
                    "arrivals": [],
                    "banked_arrivals": [],
                    "usage_known": True,
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "sha256": "0" * 64,
                    "mtime_ns": True,
                    "edit_observed": True,
                    "active_jobs": [],
                    "arrivals": [],
                    "banked_arrivals": [],
                    "usage_known": True,
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "sha256": "0" * 64,
                    "mtime_ns": 0,
                    "edit_observed": True,
                    "active_jobs": ["x", "x"],
                    "arrivals": [],
                    "banked_arrivals": [],
                    "usage_known": True,
                }
            ),
            "{",
            "[" * 10000,
        ]
        for raw in invalids:
            with self.subTest(raw=raw[:20]):
                self.receipt.write_text(raw, encoding="utf-8")
                result = run(self.tracker, self.receipt)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("invalid input:", result.stderr)

    def test_symlink_is_rejected(self):
        self.write_receipt()
        link = self.root / "link.json"
        link.symlink_to(self.receipt)
        result = run(self.tracker, link)
        self.assertEqual(result.returncode, 2)
        self.assertIn("regular non-symlink", result.stderr)

    def test_copied_local_script_and_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recipient"
            shutil.copytree(
                HERE.parent,
                destination,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "test_continuity_check.py"
                ),
            )
            tracker = destination / "fixtures" / "tracker.md"
            receipt = destination / "receipt.json"
            raw = tracker.read_bytes()
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "mtime_ns": tracker.stat().st_mtime_ns,
                        "edit_observed": True,
                        "active_jobs": [],
                        "arrivals": [],
                        "banked_arrivals": [],
                        "usage_known": True,
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(destination / "scripts" / "continuity_check.py"),
                    "--tracker",
                    str(tracker),
                    "--receipt",
                    str(receipt),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("continuity-check: CONSISTENT", result.stdout)


if __name__ == "__main__":
    unittest.main()
