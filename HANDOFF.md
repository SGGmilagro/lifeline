# Lifeline: handoff

Status as of 3 Oct 2026, ~18:00 Boston. Built in one day on the Dell Pro Max GB10 for the hackathon.
Repo: https://github.com/SGGmilagro/lifeline (public) · Demo video: https://youtu.be/dO4nQzA0oh4

## 1. What it is

An earthquake rescue agent that runs entirely on the GB10. It ranks damaged buildings from
satellite grading, estimates how many people are inside, routes three rescue teams with OR-Tools,
takes signs of life from Telegram reports and WiFi sensing, and asks a human to approve every move.
The only network traffic at runtime is Telegram delivery.

> Satellites tell you which building. The building tells you how many. The optimiser tells you
> who goes first. People and sensors tell you who is alive.

## 2. Run it

```bash
cd ~/lifeline
LIFELINE_CHAT_ID=8716887888 ./start.sh --reset   # sensors, toolbox (:8090), live WiFi sensor, agent loop
./stop.sh
./reset.sh                                        # replay back to 04:17
./leak_test.sh                                    # security proof (agent refuses + OpenShell DENIED)
./install_agent.sh                                # re-upload agent files after a sandbox rebuild
```

Screens (on the GB10, offline-capable):
- `http://localhost:8090/app`: Lifeline Command front end (designed as a Claude artifact, wired to the backend)
- `http://localhost:8090/`: operations dashboard (Leaflet, zoomable satellite tiles, swipe, proof panel, live radar)
- `http://localhost:3000/ui/index.html`: RuView's own UI (Docker container `ruview`)

Telegram: bot **@Eye_viewbot**. The owner's Telegram user (ID 8716887888) is paired and is the
current `LIFELINE_CHAT_ID` (direct chat). A group "Lifeline Command" was planned; to use a group,
add the bot, send a message, find the group ID in the OpenClaw session list, restart with that ID.

Commands (Telegram or the /app chat or the dashboard box): `help`, `status`, `who is alive`,
`ack 5`, `ack all`, `scan 7`, `report 8 tapping heard`, `report 8 silence`, `go Malatya`, `go live`.
Free questions go to the local qwen agent.

## 3. Architecture

```
Copernicus EMSR648 grading -> build_sites.py -> data/regions/<r>/sites.json
Maxar COGs (windowed reads) -> make_chips.py / make_tiles.py -> chips + XYZ tiles (tiles git-ignored)
OSM + MassGIS 2025 aerial -> build_live.py -> data/regions/live (our venue)
replay_sensors.py (10 s) + ruview_live.py (GB10 WiFi RSSI, RuView classifier) -> sensors
engine.py: ranking, occupancy estimate, reports, survivor logic, escalation, alerts, Telegram text
routing.py: OR-Tools VRP (our own model), re-solved on every tick and report
toolbox.py: FastAPI :8090, serves /app, /, all API endpoints
run_loop.sh -> sync.py every 30 s: read commands from the Telegram chat transcript, push brief/status/
  alerts/help files into the sandbox, wake the agent on new alerts, export chat for the screens
OpenClaw agent (qwen3.5:9b-128k via Ollama) inside the NemoClaw/OpenShell sandbox "my-assistant"
```

Key endpoints: `/map /brief /alerts /chat /site /assess /livesignal /regions /proof /livetest`
and POST `/command /ack /scan /report /region /delivered /reset /livetest /calibrate`.

## 4. Data (real, labeled)

| Region | Source | Notes |
|---|---|---|
| Malatya, Adiyaman, Kahramanmaras, Antakya, Gaziantep | Copernicus EMSR648 AOI05/02/04/11/01 grading + Maxar Open Data before/after | 16 sites each (Gaziantep 14), densest destroyed cluster |
| live | OSM footprint of 1 Education St, Cambridge (Hult, 9 floors) + MassGIS 2025 aerial | live drill, no damage data |

Occupancy = footprint x built share (0.5 for blocks) x floors (4 assumed) / 30 m2 per person x
night factor. Labeled "estimate, not a count". Routing weight = occupancy x trapped share
(50/15/3% by grade), decaying with time; a reported/sensed survivor outranks everything.

## 5. Measured results (shown on the dashboard Proof panel)

- Local AI vision vs Copernicus, blind, 60 buildings: 89% of damaged flagged, but 89% false alarms
  on undamaged controls; 1.39 s per pair. So the ranking uses expert grades, never the AI.
- Live WiFi test (uncalibrated run): walking caught 15/15 s, false alarms 26/30 s still.
  The test now calibrates on its first 20 s; a calibrated run has not been recorded yet.
- Leak test: agent refuses; direct curl from the sandbox DENIED by OpenShell policy.

## 6. Box configuration changed today (all with the owner's OK)

- NemoClaw policy presets removed: openclaw-pricing, huggingface, brew, npm, pypi. Active now:
  `local-inference`, `telegram` only.
- Telegram channel added (`channels add telegram`, `TELEGRAM_REQUIRE_MENTION=0`), sandbox rebuilt.
  Snapshot `pre-telegram` exists. Token lives only in the OpenShell credential store.
- OpenClaw: `tools.deny = ["message"]` (stops "Message failed"; replies still delivered).
- Sandbox workspace `AGENTS.md` and `HEARTBEAT.md` replaced; originals in `demo/backup/`.
- User `dell` added to the docker group (use `sg docker -c "..."` in old shells).
- RuView container: localhost-only, API token in `ruview.token` (git-ignored), cloud registry
  and mDNS disabled.
- GitHub CLI installed in `~/.local/bin/gh`, logged in as SGGmilagro.

## 7. Known limits and gotchas

- The 9B model fumbles tool calls (path prefixes, message tool, empty replies after exec). The design
  works around it: the agent only reads files and replies; commands are read from the chat
  transcript by sync.py; all numbers are pre-rendered by the engine.
- WiFi RSSI sees movement only: no counting, no still people, no breathing. Real CSI needs an
  ESP32-S3. iPhone UWB is not usable for this (no radar API).
- Telegram confirmation uses "two reports 20 s apart": the transcript carries no sender ID.
- NemoClaw snapshots fail on symlinks in the workspace; sync.py writes real file copies instead.
- `nemoclaw status`/snapshot need docker group access.
- The venue WiFi is slow (~180 KB/s up); large pushes take minutes.
- /app labels overlap where buildings are close; only its Routes tab was checked in a browser.
- The claude.ai artifacts (walkthrough "Lifeline Command", and the owner's "demo MVP") are private
  and cannot reach the GB10; the real front end is /app.

## 8. Not done

- Telegram group set-up and the final demo rehearsal runs (Run 1 done on the older flow).
- Calibrated live WiFi test result.
- README demo script v3 is the current one; earlier versions remain below it.

## 9. Owner preferences

- No Claude co-author or session trailers in commits (history was rewritten to remove them).
- Plain-language, first-time-user wording in anything the commander sees.
- Honest labels everywhere: simulated vs real, estimate vs count, no signal is not "empty".
