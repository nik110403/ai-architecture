#!/bin/sh
# Stub that delegates to the real encoder: lives inside the collection tree, so a receipt using it is a fixture.
for d in /usr/bin /usr/local/bin /bin; do
  if [ -x "$d/ffmpeg" ]; then exec "$d/ffmpeg" "$@"; fi
done
exit 127
