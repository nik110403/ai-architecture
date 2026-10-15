import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCAN = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"


class PrivacyScanTests(unittest.TestCase):
    def run_scan(self, files):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name, text in files.items():
            path = Path(temp.name) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-B", str(SCAN), "--root", temp.name],
            capture_output=True,
            text=True,
        )

    def test_clean_tree(self):
        result = self.run_scan({"a.md": "Nothing private here.\nAIer and layout are fine.\n"})
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_planted_path_reported(self):
        result = self.run_scan({"sub/a.md": "ok\nsee " + "/" + "home" + "/x/file\n"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("sub/a.md:2:", result.stdout)

    def test_token_prefixes_and_case(self):
        for text in ("ses" + "_abc123", "chg" + "-0f3a", "AI" + "ER", "La" + "ya"):
            self.assertEqual(self.run_scan({"a.txt": text}).returncode, 1, text)

    def test_binary_skipped(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "b.bin").write_bytes(b"\0" + ("/" + "home" + "/x").encode())
        result = subprocess.run(
            [sys.executable, "-B", str(SCAN), "--root", temp.name], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
