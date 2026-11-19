#!/bin/sh
# Stub for a renderer that must never be started: leaves a sentinel next to itself when it runs.
touch "$(dirname "$0")/ran"
exit 0
