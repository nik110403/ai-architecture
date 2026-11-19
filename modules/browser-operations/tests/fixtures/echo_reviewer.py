#!/usr/bin/env python3
"""Test-only reviewer: approves whatever page hash the survey carries (for pages whose DOM only a browser builds)."""
import json
import os
import sys

req = json.load(sys.stdin)
print(json.dumps({"verdict": "APPROVE", "reasons": [], "dom_sha256_seen": req["survey"]["dom_sha256"],
                  "pid": os.getpid()}))
