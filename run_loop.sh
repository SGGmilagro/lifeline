#!/usr/bin/env bash
# Lifeline loop: every 30 s, sync the toolbox brief into the sandbox and wake the agent on new alerts.
# Runs with no cloud API keys in its environment (rule: no keys anywhere in the product).
cd "$(dirname "$0")"
for v in $(env | grep -oE '^[A-Z0-9_]*(API_KEY|_TOKEN)[A-Z0-9_]*' ); do unset "$v"; done
while true; do
  .venv/bin/python sync.py || echo "sync failed, retrying"
  sleep "${LIFELINE_LOOP_S:-30}"
done
