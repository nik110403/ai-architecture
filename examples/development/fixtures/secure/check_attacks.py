"""Replay one request against fileserver.py in a fresh tree. Usage: SOURCE_DIR ID INPUT (INPUT may hold {secret})."""
import importlib.util
import json
import os
import sys
import tempfile

source, request_id, raw = sys.argv[1:4]
with tempfile.TemporaryDirectory() as tmp:
    served = os.path.join(tmp, "served")
    outside = os.path.join(tmp, "outside")
    os.makedirs(os.path.join(served, "docs"))
    os.makedirs(outside)
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w", encoding="utf-8") as handle:
        handle.write("SECRET")
    with open(os.path.join(served, "docs", "readme.txt"), "w", encoding="utf-8") as handle:
        handle.write("hello")
    os.symlink(outside, os.path.join(served, "escape"))  # runtime symlink: never committed
    spec = importlib.util.spec_from_file_location("fileserver", os.path.join(source, "fileserver.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        data = module.read_file(served, raw.replace("{secret}", secret))
    except Exception as error:  # noqa: BLE001 - only the guard's own refusal counts as refused
        outcome = "refused" if type(error).__name__ == "Refused" else "error"
    else:
        outcome = "leaked" if data == b"SECRET" else "served"
print(json.dumps({"request": request_id, "outcome": outcome}))
