"""A15 (descriptor, privacy, provenance) and the static no-network scan for model-evaluation."""

import getpass
import importlib.util
import json
import os
import pwd
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meval_helpers import MOD, ROOT, Isolated, scores  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    saved = sys.modules.pop("capabilities", None)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(ROOT / "scripts"))
        sys.modules.pop("capabilities", None)
        if saved is not None:
            sys.modules["capabilities"] = saved
    return mod


class Module(Isolated):
    def test_descriptor_passes_with_planned(self):
        load("meval_contracts", "modules/contracts/contracts.py").check_module_descriptor(MOD, module="model-evaluation")
        self.assertEqual(json.loads((MOD / "descriptor.json").read_text())["claimed_support"], "planned")

    def test_privacy_scan_and_provenance(self):
        scan = load("meval_privacy", "scripts/privacy_scan.py")
        self.assertEqual(scan.scan(MOD), [])
        load("meval_check_collection", "scripts/check_collection.py").provenance(MOD / "PROVENANCE.md")
        text = (MOD / "PROVENANCE.md").read_text()
        self.assertIn("not ported", text)
        self.assertIn("private operator repository", text)

    def test_plans_and_reports_carry_no_identity(self):
        pool, ev = self.pool(), self.evidence()
        plan_file, plan = self.dry_plan(pool, ev)
        self.assertEqual(self.reweight(pool, ev, "--apply", plan_file)[0], 0)
        blob = json.dumps(plan)
        for p in list(self.out.rglob("*")):
            if p.is_file():
                blob += p.read_text()
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        for ident in (real_home, os.environ["HOME"], str(self.t), getpass.getuser(), socket.gethostname()):
            if len(ident) > 3:
                self.assertNotIn(ident, blob)

    def test_static_no_network_and_subprocess_only_in_bakeoff(self):
        for path in sorted(MOD.glob("*.py")):
            text = path.read_text()
            for bad in ("urllib", "http.client", "import socket", "requests", ".credentials"):
                self.assertNotIn(bad, text, f"{path.name}: {bad}")
            if path.name != "bakeoff.py":
                self.assertNotIn("subprocess", text, path.name)

    def test_out_dir_refuses_symlink(self):
        pool, ev = self.pool(), self.evidence()
        real = self.t / "realout"
        real.mkdir()
        link = self.t / "linkout"
        os.symlink(real, link)
        code, _ = self.cli("reweight", "--pool", pool, "--evidence", ev, "--out", link, "--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(list(real.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
