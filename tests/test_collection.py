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

    def test_record_rejects_single_hash_heading_in_unit(self):
        self.corrupt(
            "opencode/bridge/PROVENANCE.md",
            "## plugin.ts\n",
            "# plugin.ts\n",
            "unit headings",
        )

    def test_record_rejects_triple_hash_heading_in_unit(self):
        self.corrupt(
            "opencode/bridge/PROVENANCE.md",
            "## plugin.ts\n",
            "### plugin.ts\n",
            "unit headings",
        )

    def test_record_requires_unit_heading_not_substring(self):
        self.corrupt(
            "opencode/bridge/PROVENANCE.md",
            "## bash-structural-hook.ts\n",
            "## the hook (bash-structural-hook.ts)\n",
            "unit heading",
        )

    def test_record_non_verbatim_needs_real_delta(self):
        self.corrupt(
            "opencode/bridge/PROVENANCE.md",
            "- wires only the gates bridge and the structural bash check; continuity, model rotation, rate-limit fallback and the other personal modules are not included",
            "- none",
            "non-'none' delta",
        )

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
            "## How the system works",
            "[escape](../outside.md)\n## How the system works",
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
            "marketplace plugin inventory",
        )

    def test_manifest_bad_version(self):
        self.corrupt(
            ".claude-plugin/plugin.json", "0.4.2-preview", "nonsense", "name/version"
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

    GOOD = (
        "# Provenance\n\nSource: ~/.claude/skills/plan-pack/SKILL.md\n"
        "Source-sha256: " + "a" * 64 + "\nState: adapted\n\n## Deltas\n\n- trimmed\n"
    )

    def ported(self, text, where="skills/ported-x"):
        root = self.copied()
        directory = root / where
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("not a normal skill\n", encoding="utf-8")
        (directory / "PROVENANCE.md").write_text(text, encoding="utf-8")
        return self.run_check(root)

    def test_ported_skill_accepted(self):
        result = self.ported(self.GOOD)
        self.assertEqual(result.returncode, 0, result.stderr)

    def ported_agent(self, record):
        root = self.copied()
        directory = root / "agents/ported-x"
        directory.mkdir(parents=True)
        (directory / "ported-x.md").write_text("not a normal agent\n", encoding="utf-8")
        (directory / record).write_text(self.GOOD, encoding="utf-8")
        return self.run_check(root)

    def test_ported_agent_txt_accepted(self):
        result = self.ported_agent("PROVENANCE.txt")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ported_agent_md_rejected(self):
        result = self.ported_agent("PROVENANCE.md")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROVENANCE.txt", result.stderr)

    def test_ported_tool_accepted(self):
        result = self.ported(self.GOOD, "tools/ported-x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ported_missing_state_rejected(self):
        result = self.ported(self.GOOD.replace("State: adapted\n", ""))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("State", result.stderr)

    def test_ported_missing_sha_rejected(self):
        result = self.ported(self.GOOD.replace("a" * 64, "abc"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Source-sha256", result.stderr)

    def test_ported_missing_deltas_rejected(self):
        result = self.ported(self.GOOD.replace("- trimmed\n", ""))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Deltas", result.stderr)

    def test_ported_home_path_rejected(self):
        home = "/" + "home" + "/x/skills/y"
        result = self.ported(self.GOOD.replace("~/.claude/skills/plan-pack", home))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("home-directory", result.stderr)

    GATES = "plugins/gates/hooks/PROVENANCE.md"
    UNIT = "State: adapted\n"

    def test_gates_provenance_missing_rejected(self):
        root = self.copied()
        (root / self.GATES).unlink()
        result = self.run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROVENANCE.md", result.stderr)

    def test_gates_provenance_missing_state_rejected(self):
        self.corrupt(self.GATES, self.UNIT, "", "State")

    def test_gates_provenance_bad_sha_rejected(self):
        root = self.copied()
        path = root / self.GATES
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("Source-sha256: ", "Source-sha256: zz", 1), encoding="utf-8"
        )
        self.assertIn("Source-sha256", self.run_check(root).stderr)

    def test_gates_provenance_without_deltas_rejected(self):
        root = self.copied()
        path = root / "plugins/gates/hooks/rotation/PROVENANCE.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("Deltas:", "Changes:"), encoding="utf-8")
        self.assertIn("Deltas", self.run_check(root).stderr)

    def test_gates_provenance_unlisted_hook_rejected(self):
        root = self.copied()
        (root / "plugins/gates/hooks/extra-hook.py").write_text(
            "pass\n", encoding="utf-8"
        )
        self.assertIn("does not account for", self.run_check(root).stderr)

    def test_opencode_provenance_malformed_rejected(self):
        self.corrupt("opencode/bridge/PROVENANCE.md", self.UNIT, "", "State")

    def test_opencode_provenance_missing_rejected(self):
        root = self.copied()
        (root / "opencode/rotation/PROVENANCE.md").unlink()
        self.assertIn("missing provenance record", self.run_check(root).stderr)

    def test_marketplace_must_list_gates(self):
        self.corrupt(
            ".claude-plugin/marketplace.json",
            '"source": "./plugins/gates"',
            '"source": "./plugins/other"',
            "marketplace plugin inventory",
        )


if __name__ == "__main__":
    unittest.main()
