#!/usr/bin/env python3
"""Test connector for the documents and productivity modules. Local only: no network, no real provider.

`--describe` prints the operations it offers (nothing on stdin). `--op NAME` reads one JSON request from
stdin and prints a result whose ids are derived from the request (a hash), never an echo. Test options:
--sentinel FILE (touched when an operation runs), --env-dump FILE (writes the names of its environment
variables), --operations CSV (override what --describe offers).
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

OPERATIONS = ["read", "write", "create", "mail.send", "calendar.add", "calendar.move", "calendar.delete",
              "drive.upload", "drive.share"]


def derive(*parts):
    return "x-" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--describe", action="store_true")
    p.add_argument("--op")
    p.add_argument("--sentinel")
    p.add_argument("--env-dump")
    p.add_argument("--operations")
    a = p.parse_args()
    if a.env_dump:
        Path(a.env_dump).write_text("\n".join(sorted(os.environ)) + "\n", encoding="utf-8")
    if a.describe:
        ops = a.operations.split(",") if a.operations else OPERATIONS
        print(json.dumps({"name": "stub", "operations": ops}))
        return 0
    if not a.op:
        p.error("--op or --describe is required")
    if a.sentinel:
        Path(a.sentinel).write_text("spawned\n", encoding="utf-8")
    request = json.load(sys.stdin)
    doc = request.get("doc") or request.get("target", "")
    if a.op == "read":
        result = {"revision": derive("read", doc), "content": "stub content for " + str(doc)}
    elif a.op == "write":
        body = hashlib.sha256(request.get("content", "").encode()).hexdigest()
        result = {"revision": derive("write", doc, request.get("base_revision"), body)}
    elif a.op == "create":
        result = {"revision": derive("create", doc, request.get("title"), request.get("content"))}
    else:
        ident = derive(a.op, doc, request.get("payload_sha256"), request.get("base_revision"))
        result = {"id": ident, "revision": ident}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
