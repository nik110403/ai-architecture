"""Replay one scenario: what the glossary implies versus what the code answers. Usage: ROOT SCENARIO."""
import importlib.util
import json
import os
import re
import sys

root, scenario = sys.argv[1], sys.argv[2]
with open(os.path.join(root, "scenarios.json"), encoding="utf-8") as handle:
    days = json.load(handle)[scenario]["days_since_login"]
with open(os.path.join(root, "glossary.md"), encoding="utf-8") as handle:
    line = next(text for text in handle if "active user" in text)
window = int(re.search(r"within the last (\d+) days", line).group(1))
spec = importlib.util.spec_from_file_location("metrics", os.path.join(root, "metrics.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
expected, observed = days <= window, bool(module.is_active(days))
print(json.dumps({"scenario": scenario, "expected": expected, "observed": observed, "contradiction": expected != observed}))
