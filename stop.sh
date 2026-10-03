#!/usr/bin/env bash
# Stop the Lifeline processes started by start.sh.
cd "$(dirname "$0")"
for p in loop toolbox sensors live; do
  [ -f demo/$p.pid ] && kill "$(cat demo/$p.pid)" 2>/dev/null && echo "stopped $p"
  rm -f demo/$p.pid
done
# Any leftovers from manual runs.
pid=$(ss -ltnp 2>/dev/null | grep -oP ':8090\b.*pid=\K\d+' | head -1); [ -n "$pid" ] && kill "$pid"
pgrep -f "[r]eplay_sensors.py$" | xargs -r kill
exit 0
