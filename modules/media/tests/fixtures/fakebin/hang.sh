#!/bin/sh
# Stub that hangs: a grandchild holds stdout open and its pid is written next to this script.
( sleep 60 ) &
echo $! > "$(dirname "$0")/grandchild.pid"
sleep 60
