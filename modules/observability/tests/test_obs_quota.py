"""A12 (+AM1, AM7, AM10): quota is never enabled on unsupported providers; failure cells never block."""

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from obs_helpers import Isolated  # noqa: E402
from modules.observability import quota  # noqa: E402


def boom(*a, **k):
    raise AssertionError("network or process use is forbidden in quota code")


class QuotaGate(Isolated):
    def snap(self, name, provider, used=95, fetched=None, windows=None, raw=None):
        path = self.t / name
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(__import__("json").dumps({
                "provider": provider, "fetched_at": time.time() if fetched is None else fetched,
                "windows": windows if windows is not None else [{"name": "5h", "used_percent": used, "resets_at": None}]}))
        return path

    def entry(self, path, adapter="snapshot-file", gate=True):
        return {"adapter": adapter, "path": str(path), "gate": gate, "threshold_percent": 90, "max_age_seconds": 900}

    def gate(self, providers, name):
        cfg = self.write_json("quota.json", {"version": 1, "providers": providers})
        with mock.patch.object(socket.socket, "connect", boom), mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "getaddrinfo", boom), mock.patch.object(subprocess, "Popen", boom):
            return self.cli("quota", "gate", "--config", cfg, "--provider", name)

    def test_unsupported_unconfigured_and_ungated_never_apply(self):
        over = self.snap("over.json", "x")
        providers = {
            "uns": self.entry(over, adapter="claude-statusline"),   # registered, supported: false
            "ghost": self.entry(over, adapter="no-such-adapter"),   # absent from the registry
            "nogate": self.entry(over, gate=False),
        }
        for name in ("uns", "ghost", "nogate", "missing"):
            code, out = self.gate(providers, name)
            self.assertEqual((code, out["gate"]), (0, "not_applicable"), name)
        self.assertEqual(self.gate(providers, "uns")[1]["state"], "unsupported")
        self.assertEqual(self.gate(providers, "ghost")[1]["state"], "unsupported")

    def test_unusable_snapshots_are_unavailable_and_never_block(self):
        now = time.time()
        (self.t / "real.json").write_text("{}")
        os.symlink(self.t / "real.json", self.t / "link.json")
        (self.t / "big.json").write_text(" " * (1_000_001))
        cases = {
            "stale": self.snap("a.json", "stale", fetched=now - 5000),
            "malformed": self.snap("b.json", "malformed", raw="{not json"),
            "future": self.snap("c.json", "future", fetched=now + 3600),
            "mismatch": self.snap("d.json", "other-name"),
            "symlink": self.t / "link.json",
            "oversize": self.t / "big.json",
            "nan": self.snap("e.json", "nan", raw='{"provider":"nan","fetched_at":%s,"windows":[{"name":"w","used_percent":NaN}]}' % now),
            "range": self.snap("f.json", "range", used=140),
            "negative": self.snap("g.json", "negative", used=-1),
            "empty": self.snap("h.json", "empty", windows=[]),
            "missing": self.t / "does-not-exist.json",
        }
        providers = {k: self.entry(v) for k, v in cases.items()}
        for name in cases:
            code, out = self.gate(providers, name)
            self.assertEqual((code, out["gate"]), (0, "unavailable"), name)
        self.assertEqual(self.gate(providers, "stale")[1]["state"], "stale")

    def test_only_supported_configured_fresh_over_threshold_blocks(self):
        providers = {"hot": self.entry(self.snap("hot.json", "hot", used=95)),
                     "cool": self.entry(self.snap("cool.json", "cool", used=10))}
        code, out = self.gate(providers, "hot")
        self.assertEqual((code, out["gate"], out["state"]), (3, "blocked", "available"))
        code, out = self.gate(providers, "cool")
        self.assertEqual((code, out["gate"], out["state"]), (0, "ok", "available"))

    def test_status_lists_every_configured_provider_with_support(self):
        providers = {"hot": self.entry(self.snap("hot.json", "hot")),
                     "uns": self.entry(self.snap("u.json", "uns"), adapter="codex-sidecar")}
        cfg = self.write_json("quota.json", {"version": 1, "providers": providers})
        code, out = self.cli("quota", "status", "--config", cfg)
        self.assertEqual(code, 0)
        states = {p["provider"]: p["state"] for p in out["providers"]}
        self.assertEqual(states, {"hot": "available", "uns": "unsupported"})

    def test_no_adapter_is_supported_except_the_generic_snapshot_file(self):
        self.assertEqual({k for k, v in quota.ADAPTERS.items() if v["supported"]}, {"snapshot-file"})
        self.assertFalse(quota.is_supported("no-such-adapter"))


if __name__ == "__main__":
    unittest.main()
