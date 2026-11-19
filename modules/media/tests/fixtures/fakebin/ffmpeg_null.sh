#!/bin/sh
# Stub encoder: answers the version probe, logs its argv next to itself, exits 0 and writes nothing.
case "$1" in -version) echo "ffmpeg version 6.1.1"; exit 0;; esac
printf '%s\n' "$@" > "$(dirname "$0")/argv.log"
exit 0
