"""Simulated life-sign sensors for every site. Deterministic, labeled "replay".

Run the feed (one reading per sensor every 10 s):
    python replay_sensors.py
Control a running feed from another shell:
    python replay_sensors.py --trigger B-07 --bpm 14 --moving false
    python replay_sensors.py --decline B-07 --to 8 --over 60
    python replay_sensors.py --reset
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).parent / "data"
SITES = DATA / "sites.json"
OUT = DATA / "sensors.json"
CONTROL = DATA / "sensor_control.json"
TICK_S = 10
SECOND_SENSOR_DELAY_S = 20      # sensor b picks the signal up 20 s after sensor a
CONFIDENCE = {"a": 0.81, "b": 0.74}  # fixed simulated values, not measured


def write_atomic(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def load_control():
    return json.loads(CONTROL.read_text()) if CONTROL.exists() else {}


def bpm_now(sig, now):
    """Breathing rate for a triggered site, following any decline linearly."""
    d = sig.get("decline")
    if not d:
        return sig["bpm"]
    frac = min(1.0, max(0.0, (now - d["start"]) / d["over"]))
    return round(d["from"] + (d["to"] - d["from"]) * frac, 1)


def make_readings(site_ids, control, now, tick):
    iso = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    readings = []
    for sid in site_ids:
        sig = control.get(sid)
        for s in ("a", "b"):
            r = {"site_id": sid, "sensor_id": f"{sid}-{s}", "presence": False,
                 "breathing_bpm": None, "moving": False, "confidence": None,
                 "time": iso, "tick": tick, "source": "replay"}
            delay = 0 if s == "a" else SECOND_SENSOR_DELAY_S
            if sig and now >= sig["start"] + delay:
                r.update(presence=True, breathing_bpm=bpm_now(sig, now),
                         moving=sig["moving"], confidence=CONFIDENCE[s])
            readings.append(r)
    return {"tick": tick, "time": iso, "source": "replay", "readings": readings}


def run():
    site_ids = [s["id"] for s in json.loads(SITES.read_text())["sites"]]
    tick = 0
    print(f"replay sensors: {len(site_ids)} sites x 2 sensors, every {TICK_S} s -> {OUT}")
    while True:
        tick += 1
        write_atomic(OUT, make_readings(site_ids, load_control(), time.time(), tick))
        time.sleep(TICK_S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", metavar="SITE")
    ap.add_argument("--bpm", type=float, default=14)
    ap.add_argument("--moving", choices=["true", "false"], default="false")
    ap.add_argument("--decline", metavar="SITE")
    ap.add_argument("--to", type=float, default=8)
    ap.add_argument("--over", type=float, default=60)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.reset:
        write_atomic(CONTROL, {})
        print("all survivor signals cleared")
    elif a.trigger:
        c = load_control()
        c[a.trigger] = {"start": time.time(), "bpm": a.bpm, "moving": a.moving == "true"}
        write_atomic(CONTROL, c)
        print(f"signal started at {a.trigger}: {a.bpm} bpm, moving={a.moving}")
    elif a.decline:
        c = load_control()
        sig = c.get(a.decline)
        if not sig:
            raise SystemExit(f"no signal at {a.decline}; --trigger it first")
        now = time.time()
        sig["decline"] = {"start": now, "from": bpm_now(sig, now), "to": a.to, "over": a.over}
        write_atomic(CONTROL, c)
        print(f"{a.decline}: breathing falling to {a.to} bpm over {a.over} s")
    else:
        run()


if __name__ == "__main__":
    main()
