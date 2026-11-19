"""A15 (descriptor, privacy, provenance) for the observability module."""

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

from obs_helpers import MOD, PRICES, ROOT, Isolated  # noqa: E402


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
        contracts = load("obs_contracts", "modules/contracts/contracts.py")
        contracts.check_module_descriptor(MOD, module="observability")
        self.assertEqual(json.loads((MOD / "descriptor.json").read_text())["claimed_support"], "planned")

    def test_privacy_scan_and_provenance(self):
        scan = load("obs_privacy", "scripts/privacy_scan.py")
        for rel in ("modules/observability", "opencode/rotation", "plugins/gates/hooks/rotation"):
            self.assertEqual(scan.scan(ROOT / rel), [], rel)
        check = load("obs_check_collection", "scripts/check_collection.py")
        check.provenance(MOD / "PROVENANCE.md")
        text = (MOD / "PROVENANCE.md").read_text()
        self.assertIn("not ported", text)
        self.assertIn("private operator repository", text)

    def test_reports_carry_no_identity(self):
        from modules.observability import reports
        rows = [{"harness": "h", "role": "r", "model": "provider/model-a", "source": "dispatch",
                 "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0, "reasoning": None},
                 "usage_class": "usage_known", "reported_cost_usd": None}]
        blob = json.dumps(reports.spend_report(rows, PRICES))
        code, res = self.cli("resources", "--pid", os.getpid())
        blob += json.dumps(res)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        for ident in (real_home, os.environ["HOME"], getpass.getuser(), socket.gethostname()):
            if len(ident) > 3:
                self.assertNotIn(ident, blob)


if __name__ == "__main__":
    unittest.main()
