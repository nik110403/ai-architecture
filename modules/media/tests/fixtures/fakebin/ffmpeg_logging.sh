#!/bin/sh
# Stub that records its argv next to itself, then delegates to the real encoder.
printf '%s\n' "$@" >> "$(dirname "$0")/argv.log"
for d in /usr/bin /usr/local/bin /bin; do
  if [ -x "$d/ffmpeg" ]; then exec "$d/ffmpeg" "$@"; fi
done
exit 127
