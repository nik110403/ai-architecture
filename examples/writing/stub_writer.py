#!/usr/bin/env python3
"""Deterministic template writer used by the example and the tests. No model, no network.

`--describe` prints what it supports. Otherwise it reads a source from stdin and renders
it in the shape of `--register`. Test options: --sentinel FILE (touched when it renders),
--registers CSV / --destinations CSV (override what --describe reports).
"""

import argparse
import json
import sys
from pathlib import Path

REGISTERS = ["correspondence", "chat", "essay", "personal"]
DESTINATIONS = ["file", "letter"]


def render(register, text):
    title, body, intent = "", [], ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("Intent:"):
            intent = line[7:].strip()
        elif line.strip():
            body.append(line.strip())
    if register == "correspondence":
        parts = ["Dear reader,", "Regarding " + (title or "this matter") + ":", *body, intent, "Kind regards,\nThe author"]
        return "\n\n".join(p for p in parts if p) + "\n"
    if register == "essay":
        parts = ["# " + title, *body, "In short: " + intent]
        return "\n\n".join(parts) + "\n"
    if register == "personal":
        parts = ["I want to say this plainly.", *["I noticed that " + b[0].lower() + b[1:] for b in body],
                 "What I hope for: " + intent]
        return "\n".join(parts) + "\n"
    return " ".join([title + ":", *body, intent]) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--describe", action="store_true")
    p.add_argument("--register", default="essay")
    p.add_argument("--destination", default="file")
    p.add_argument("--sentinel")
    p.add_argument("--registers")
    p.add_argument("--destinations")
    a = p.parse_args()
    if a.describe:
        print(json.dumps({
            "name": "stub",
            "registers": a.registers.split(",") if a.registers else REGISTERS,
            "destinations": a.destinations.split(",") if a.destinations else DESTINATIONS,
        }))
        return 0
    if a.sentinel:
        Path(a.sentinel).write_text("spawned\n", encoding="utf-8")
    sys.stdout.write(render(a.register, sys.stdin.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
