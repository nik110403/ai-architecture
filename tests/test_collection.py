import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CollectionTests(unittest.TestCase):
    def run_check(self, root):
        return subprocess.run(
            [sys.executable, "-B", str(root / "scripts/check_collection.py")],
            cwd=root.parent,
            capture_output=True,
            text=True,
        )

    def copied(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "collection"
        shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
        return root

    def corrupt(self, path, old, new, diagnostic):
        root = self.copied()
        target = root / path
        text = target.read_text(encoding="utf-8")
        self.assertIn(old, text)
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        result = self.run_check(root)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(diagnostic, result.stderr)

    def test_collection_passes_from_other_cwd(self):
        result = self.run_check(ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_template(self):
        root = self.copied()
        (root / "templates/plan.md").unlink()
        self.assertIn("template inventory", self.run_check(root).stderr)

    def test_missing_frontmatter(self):
        self.corrupt("skills/planning/SKILL.md", "---\n", "# broken\n", "frontmatter")

    def test_duplicate_frontmatter(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "name: planning",
            "name: planning\nname: planning",
            "duplicate frontmatter",
        )

    def test_malformed_frontmatter(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "name: planning",
            "name planning",
            "malformed scalar",
        )

    def test_unsupported_yaml(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "name: planning",
            "name: [planning]",
            "unsupported YAML",
        )

    def test_invalid_name(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "name: planning",
            "name: Bad_Name",
            "invalid/mismatched name",
        )

    def test_mismatched_name(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "name: planning",
            "name: execution",
            "invalid/mismatched name",
        )

    def test_long_description(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "description: ",
            "description: " + "x" * 1025,
            "description too long",
        )

    def test_catalog_missing_entry(self):
        self.corrupt(
            "CATALOG.md", "- [builder](agents/builder.md)\n", "", "catalog inventory"
        )

    def test_catalog_broken_target(self):
        self.corrupt(
            "CATALOG.md",
            "skills/planning/SKILL.md",
            "skills/planning/absent.md",
            "catalog inventory",
        )

    def test_missing_backtick_asset(self):
        root = self.copied()
        (root / "skills/continuity/scripts/journal.py").unlink()
        result = self.run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("journal.py", result.stderr)

    def test_unlisted_missing_dependency(self):
        self.corrupt(
            "skills/planning/SKILL.md",
            "## Steps",
            "Read `assets/missing.txt`.\n\n## Steps",
            "missing.txt",
        )

    def test_escaping_reference(self):
        self.corrupt(
            "README.md",
            "# AI Architecture Collection",
            "[escape](../outside.md)\n# AI Architecture Collection",
            "escaping/redirected",
        )

    def test_symlink_dependency(self):
        root = self.copied()
        path = root / "skills/continuity/scripts/journal.py"
        path.unlink()
        path.symlink_to(root / "scripts/check_collection.py")
        self.assertIn("symlink", self.run_check(root).stderr)

    def test_extra_role(self):
        root = self.copied()
        (root / "agents/extra.md").write_text("extra", encoding="utf-8")
        self.assertIn("agents inventory", self.run_check(root).stderr)

    def test_missing_command(self):
        root = self.copied()
        (root / "commands/plan.md").unlink()
        self.assertIn("commands inventory", self.run_check(root).stderr)

    def test_unbound_command(self):
        self.corrupt(
            "commands/plan.md", "${CLAUDE_PLUGIN_ROOT}/", "", "unbound/unnamespaced"
        )

    def test_manifest_bad_owner(self):
        self.corrupt(
            ".claude-plugin/marketplace.json",
            '"name": "Nik"',
            '"name": "Someone"',
            "attribution",
        )

    def test_manifest_escaping_source(self):
        self.corrupt(
            ".claude-plugin/marketplace.json",
            '"source": "./"',
            '"source": "../"',
            "source/name/version",
        )

    def test_manifest_bad_version(self):
        self.corrupt(
            ".claude-plugin/plugin.json", "0.2.0-preview", "nonsense", "name/version"
        )

    def test_manifest_duplicate_key(self):
        self.corrupt(
            ".claude-plugin/plugin.json",
            '"name": "ai-architecture",',
            '"name": "ai-architecture", "name": "other",',
            "duplicate JSON",
        )

    def test_manifest_missing_author(self):
        root = self.copied()
        path = root / ".claude-plugin/plugin.json"
        data = json.loads(path.read_text())
        del data["author"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("plugin schema", self.run_check(root).stderr)

    def test_catalog_wrong_label(self):
        self.corrupt(
            "CATALOG.md", "[planning]", "[wrong-name]", "catalog name mismatch"
        )

    def test_catalog_missing_skill(self):
        self.corrupt(
            "CATALOG.md",
            "[planning](skills/planning/SKILL.md)",
            "planning",
            "catalog inventory",
        )

    def test_unbound_health_script(self):
        self.corrupt(
            "commands/health.md",
            "${CLAUDE_PLUGIN_ROOT}/scripts/",
            "scripts/",
            "unbound/unnamespaced",
        )

    def test_positive_generic_placeholders(self):
        root = self.copied()
        path = root / "README.md"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                "\nGeneric placeholders: `assets/<name>.txt`, `--target PATH`, `python3 -m unittest recipient.tests`, and [external](https://example.org/absent.md).\n"
            )
        result = self.run_check(root)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
