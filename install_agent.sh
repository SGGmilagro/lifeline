#!/usr/bin/env bash
# (Re)install the Lifeline agent files into the OpenClaw sandbox. Safe to re-run, e.g. after a rebuild.
cd "$(dirname "$0")"
SB="${LIFELINE_SANDBOX:-my-assistant}"
WS=/sandbox/.openclaw/workspace
q() { "$@" 2>&1 | grep -E 'Upload complete|rror' ; }
q nemoclaw "$SB" upload skill/AGENTS.md    "$WS/"
q nemoclaw "$SB" upload skill/HEARTBEAT.md "$WS/"
q nemoclaw "$SB" upload skill/SKILL.md     "$WS/skills/lifeline/"
q nemoclaw "$SB" upload skill/playbook.md  "$WS/skills/lifeline/"
# The 9B model sometimes prefixes paths with "sandbox/.openclaw/workspace/"; sync.py keeps real copies there.
nemoclaw "$SB" exec -- sh -c "mkdir -p $WS/sandbox/.openclaw/workspace/skills/lifeline && touch $WS/skills/lifeline/commands.txt $WS/sandbox/.openclaw/workspace/skills/lifeline/commands.txt" >/dev/null 2>&1
rm -f data/commands_offset
nemoclaw "$SB" exec -- openclaw skills info lifeline 2>&1 | grep -m1 lifeline
