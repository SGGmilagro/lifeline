#!/usr/bin/env bash
# Restart the replay from 2023-02-06 04:17: clear survivor signals, engine state and pending commands.
cd "$(dirname "$0")"
.venv/bin/python replay_sensors.py --reset
curl -s -X POST http://127.0.0.1:8090/reset && echo
nemoclaw "${LIFELINE_SANDBOX:-my-assistant}" exec -- sh -c ': > /sandbox/.openclaw/workspace/skills/lifeline/commands.txt' >/dev/null 2>&1
rm -f data/commands_offset
echo "replay reset to 04:17"
