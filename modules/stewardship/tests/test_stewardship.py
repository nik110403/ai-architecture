import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STEWARD = ROOT / "modules" / "stewardship" / "steward.py"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def listing(root):
    return sorted(
        (p.relative_to(root).as_posix(), p.is_symlink() or p.is_dir() or sha(p))
        for p in Path(root).rglob("*")
    )


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.registry = self.tmp / "reg" / "registry.json"
        self.registry.parent.mkdir()

    def steward(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(STEWARD), *map(str, args)],
            capture_output=True, text=True, cwd=self.tmp, timeout=60,
        )

    def project(self, name="p", instructions="AGENTS.md", text="# Rules\n"):
        root = self.tmp / name
        root.mkdir()
        if instructions:
            (root / instructions).write_text(text)
        return root

    def bootstrap(self, root):
        return self.steward("bootstrap", "--project", root, "--registry", self.registry)

    def lint(self, *roots, fmt="json"):
        args = ["lint", "--format", fmt]
        for r in roots:
            args += ["--root", r]
        return self.steward(*args)

    def findings(self, *roots):
        result = self.lint(*roots)
        return result, json.loads(result.stdout)["findings"]

    def copy_of(self, root, name):
        dest = self.tmp / name
        shutil.copytree(root, dest, symlinks=True)
        return dest


class BootstrapTests(Base):
    def test_a1_existing_files_untouched_and_pack_passes(self):
        root = self.project()
        (root / "plans" / "main").mkdir(parents=True)
        strategy = root / "plans" / "main" / "STRATEGY.md"
        strategy.write_text("mine\n")
        before = (sha(root / "AGENTS.md"), sha(strategy))
        result = self.bootstrap(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, (sha(root / "AGENTS.md"), sha(strategy)))
        self.assertTrue((root / "plans/main/TRACKER.md").is_file())
        self.assertTrue((root / "plans/main/SPEC.md").is_file())
        self.assertIn("kept plans/main/STRATEGY.md", result.stdout)
        check = subprocess.run(
            ["bash", str(ROOT / "tools/plancheck/plancheck"), "--dir", str(root / "plans/main")],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout)

    def test_a2_refusals_write_nothing(self):
        bare = self.project("bare", instructions=None)
        reg_before = listing(self.tmp / "reg")
        tree_before = listing(bare)
        result = self.bootstrap(bare)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(tree_before, listing(bare))
        self.assertEqual(reg_before, listing(self.tmp / "reg"))

        outside = self.tmp / "outside.txt"
        outside.write_text("secret\n")
        root = self.project("linked")
        (root / "plans" / "main").mkdir(parents=True)
        (root / "plans/main/SPEC.md").symlink_to(outside)
        tree_before = listing(root)
        result = self.bootstrap(root)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(outside.read_text(), "secret\n")
        self.assertEqual(tree_before, listing(root))
        self.assertEqual(reg_before, listing(self.tmp / "reg"))

        outdir = self.tmp / "outdir"
        outdir.mkdir()
        root = self.project("linkdir")
        (root / "plans").mkdir()
        (root / "plans" / "main").symlink_to(outdir)
        result = self.bootstrap(root)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(listing(outdir), [])

        real = self.project("real")
        link = self.tmp / "linkroot"
        link.symlink_to(real)
        result = self.bootstrap(link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(listing(real), [("AGENTS.md", sha(real / "AGENTS.md"))])
        self.assertEqual(reg_before, listing(self.tmp / "reg"))

    def test_a3_second_run_keeps_everything(self):
        root = self.project()
        self.assertEqual(self.bootstrap(root).returncode, 0)
        before = listing(root)
        second = self.bootstrap(root)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(before, listing(root))
        self.assertNotIn("created", second.stdout)
        self.assertEqual(second.stdout.count("kept "), 3)
        data = json.loads(self.registry.read_text())
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["root"], str(root))

    def test_a4_verify(self):
        root = self.project()
        self.bootstrap(root)
        ok = self.steward("verify", "--project", root, "--registry", self.registry)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        fresh = self.tmp / "fresh.json"
        res = self.steward("verify", "--project", root, "--registry", fresh)
        self.assertEqual(res.returncode, 1)
        self.assertIn("not registered", res.stdout)
        (root / "plans/main/SPEC.md").unlink()
        res = self.steward("verify", "--project", root, "--registry", self.registry)
        self.assertEqual(res.returncode, 1)
        self.assertIn("plans/main/SPEC.md", res.stdout)


class ResurveyTests(Base):
    def test_a5_statuses_and_explicit_inputs(self):
        x, y, z, w = (self.project(n) for n in "xyzw")
        for r in (x, y, w):
            self.bootstrap(r)
        shutil.rmtree(x)
        (y / "AGENTS.md").write_text("edited\n")
        (w / "plans/main/STRATEGY.md").unlink()
        result = self.steward("resurvey", "--registry", self.registry, "--format", "json")
        self.assertEqual(result.returncode, 1)
        by_root = {e["root"]: e["status"] for e in json.loads(result.stdout)["entries"]}
        self.assertEqual(by_root[str(x)], "missing")
        self.assertEqual(by_root[str(y)], "changed")
        self.assertEqual(by_root[str(w)], "scaffold-missing")
        self.assertNotIn(str(z), result.stdout)
        self.assertEqual(self.steward("resurvey").returncode, 2)
        self.assertEqual(self.steward("lint").returncode, 2)

    def test_a5_all_ok_exits_zero(self):
        root = self.project()
        self.bootstrap(root)
        result = self.steward("resurvey", "--registry", self.registry)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("ok", result.stdout)


class LintTests(Base):
    def setUp(self):
        super().setUp()
        self.base = self.project("base")
        self.bootstrap(self.base)

    def test_a6_baseline_clean(self):
        result, found = self.findings(self.base)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(found, [])

    def assert_plant(self, root, code, fragment):
        result, found = self.findings(root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual({f["code"] for f in found}, {code}, found)
        self.assertTrue(any(fragment in f["path"] for f in found), found)

    def test_a6_dead_link(self):
        root = self.copy_of(self.base, "c1")
        (root / "AGENTS.md").write_text("see [guide](docs/missing.md)\n")
        self.assert_plant(root, "instructions-dead-link", "AGENTS.md")

    def test_a6_agent_name_mismatch(self):
        root = self.copy_of(self.base, "c2")
        (root / "agents").mkdir()
        (root / "agents/worker.md").write_text("---\nname: other\ndescription: does things\n---\nbody\n")
        self.assert_plant(root, "agent-frontmatter", "agents/worker.md")

    def test_a6_workflow_without_jobs(self):
        root = self.copy_of(self.base, "c3")
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/ci.yml").write_text("name: ci\non: push\n")
        self.assert_plant(root, "workflow-shape", ".github/workflows/ci.yml")

    def test_a6_plan_dangling_needs(self):
        root = self.copy_of(self.base, "c4")
        spec = root / "plans/main/SPEC.md"
        spec.write_text(spec.read_text() + "- Needs: S9\n")
        self.assert_plant(root, "plan", "plans/main")

    def test_a6_good_agent_and_workflow_pass(self):
        root = self.copy_of(self.base, "c5")
        (root / ".claude/agents").mkdir(parents=True)
        (root / ".claude/agents/ok.md").write_text("---\nname: ok\ndescription: fine\n---\nx\n")
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/ci.yaml").write_text("name: ci\non: push\njobs:\n  a:\n    runs-on: x\n")
        result, found = self.findings(root)
        self.assertEqual((result.returncode, found), (0, []))

    def test_a6_missing_instructions(self):
        root = self.tmp / "noinst"
        root.mkdir()
        self.assert_plant(root, "instructions-missing", "noinst")

    def test_a7_read_only_and_explicit_roots(self):
        a = self.copy_of(self.base, "a")
        b = self.copy_of(self.base, "b")
        c = self.copy_of(self.base, "c")
        (b / "AGENTS.md").write_text("[x](nope.md)\n")
        (c / "AGENTS.md").write_text("[x](nope.md)\n")
        before = (listing(a), listing(b), listing(c))
        result = self.lint(a, b)
        self.assertEqual(before, (listing(a), listing(b), listing(c)))
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(b), result.stdout)
        self.assertNotIn(str(c), result.stdout)
        self.assertNotIn(str(a), json.dumps([f for f in json.loads(result.stdout)["findings"]]))


class DescriptorAndPrivacyTests(unittest.TestCase):
    def test_a11_descriptor_valid(self):
        spec = importlib.util.spec_from_file_location("contracts_u14", ROOT / "modules/contracts/contracts.py")
        contracts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contracts)
        contracts.check_module_descriptor(ROOT / "modules/stewardship", module="stewardship")
        data = json.loads((ROOT / "modules/stewardship/descriptor.json").read_text())
        self.assertEqual(data["claimed_support"], "planned")

    def test_a12_privacy_clean(self):
        spec = importlib.util.spec_from_file_location("privacy_u14", ROOT / "scripts/privacy_scan.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.scan(ROOT / "modules/stewardship"), [])


if __name__ == "__main__":
    unittest.main()
