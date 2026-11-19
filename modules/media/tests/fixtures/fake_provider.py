#!/usr/bin/env python3
"""Stub illustration provider (a fixture: it draws a PNG with zlib and never generates anything real).

  fake_provider.py --describe [--network true|false] [--cost none|quota|paid] [--mode ok|garbage]
  fake_provider.py [--sentinel FILE] --prompt-file F --out OUT.png
Generation writes OUT.png, OUT.png.env (the names of its environment variables) and the sentinel file when given."""
import json
import os
import struct
import sys
import zlib


def png(width, height):
    rows = b"".join(b"\x00" + b"".join(bytes(((x * 4) & 255, (y * 4) & 255, 128)) for x in range(width))
                    for y in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def option(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    if "--describe" in sys.argv:
        if option("--mode", "ok") == "garbage":
            print("not json at all")
            return 0
        print(json.dumps({"interface": "illustration/1", "kind": "fixture", "network": option("--network", "false") == "true",
                          "cost": option("--cost", "none"), "licence": "MIT", "notice_required": False, "output": "png"}))
        return 0
    sentinel = option("--sentinel", "")
    if sentinel:
        with open(sentinel, "a") as handle:
            handle.write("generate\n")
    out = option("--out", "")
    with open(option("--prompt-file", os.devnull), "rb") as handle:
        handle.read(1 << 16)
    with open(out, "wb") as handle:
        handle.write(png(64, 64))
    with open(out + ".env", "w") as handle:
        handle.write("\n".join(sorted(os.environ)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
