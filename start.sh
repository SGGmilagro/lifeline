#!/usr/bin/env bash
# Start Lifeline: sensor replay, toolbox (port 8090) and the agent loop. Map: http://localhost:8090/
#   ./start.sh            continue the current replay
#   ./start.sh --reset    restart the replay from 04:17
# Environment: LIFELINE_CHAT_ID (Telegram group id), LIFELINE_SPEED (default 120 = 2 replay min per second)
set -e
cd "$(dirname "$0")"
# Rule: no cloud API keys in any process the product runs.
for v in $(env | grep -oE '^[A-Z0-9_]*(API_KEY|_TOKEN)[A-Z0-9_]*'); do unset "$v"; done
./stop.sh >/dev/null 2>&1 || true
mkdir -p demo
# Keep the model pinned on the GPU (no-op if already loaded).
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen3.5:9b-128k","keep_alive":-1}' >/dev/null || true

nohup .venv/bin/python replay_sensors.py > demo/sensors.log 2>&1 & echo $! > demo/sensors.pid
nohup .venv/bin/python toolbox.py --speed "${LIFELINE_SPEED:-120}" > demo/toolbox.log 2>&1 & echo $! > demo/toolbox.pid
for i in $(seq 20); do curl -sf http://127.0.0.1:8090/map >/dev/null && break; sleep 0.5; done
if [ "$1" = "--reset" ]; then ./reset.sh; fi
nohup ./run_loop.sh > demo/loop.log 2>&1 & echo $! > demo/loop.pid
echo "Lifeline running. Map: http://localhost:8090/   Logs: demo/*.log"
[ -z "$LIFELINE_CHAT_ID" ] && echo "Note: LIFELINE_CHAT_ID not set, so heartbeats are not posted to Telegram."
exit 0
