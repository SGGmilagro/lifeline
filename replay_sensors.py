"""Simulated life-sign sensors for every site. Deterministic, labeled "replay".

Run the feed (one reading per sensor every 10 s):
    python replay_sensors.py
Control a running feed from another shell:
    python replay_sensors.py --trigger B-07 --bpm 14 --moving false
    python replay_sensors.py --decline B-07 --to 8 --over 60
    python replay_sensors.py --ruview B-06     # sensor a at B-06 reads RuView (simulated CSI)
    python replay_sensors.py --reset
"""
import argparse
import json
import urllib.request
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
RUVIEW = "http://127.0.0.1:3000/api/v1"
RUVIEW_TOKEN = Path(__file__).parent / "ruview.token"   # local-only bearer token for the container


def ruview_get(path):
    req = urllib.request.Request(f"{RUVIEW}/{path}",
                                 headers={"Authorization": f"Bearer {RUVIEW_TOKEN.read_text().strip()}"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def ruview_reading():
    """RuView's own presence/motion classification. Breathing only if RuView does not abstain."""
    s = ruview_get("sensing/latest")
    cls = s["classification"]
    bpm = None
    try:
        v = ruview_get("vital-signs")
        if v.get("authority") != "abstained":
            bpm = v["vital_signs"]["breathing_rate_bpm"]
    except Exception:
        pass
    return {"presence": bool(cls["presence"]), "moving": cls["motion_level"] not in ("present_still", "absent"),
            "confidence": round(cls["confidence"], 2), "breathing_bpm": bpm,
            "source": "ruview-sim" if s.get("source", "simulated") in ("simulated", None) else "ruview"}


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
            if sig and sig.get("ruview") and s == "a":
                try:
                    r.update(ruview_reading())
                except Exception as e:
                    r["source"] = f"ruview-unreachable ({type(e).__name__})"
            elif sig and "bpm" in sig and now >= sig["start"] + delay:
                r.update(presence=True, breathing_bpm=bpm_now(sig, now),
                         moving=sig["moving"], confidence=CONFIDENCE[s])
            readings.append(r)
    sources = sorted({r["source"] for r in readings})
    return {"tick": tick, "time": iso, "source": "+".join(sources), "readings": readings}


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
    ap.add_argument("--ruview", metavar="SITE")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.reset:
        write_atomic(CONTROL, {})
        print("all survivor signals cleared")
    elif a.trigger:
        c = load_control()
        c[a.trigger] = {**c.get(a.trigger, {}), "start": time.time(), "bpm": a.bpm, "moving": a.moving == "true"}
        write_atomic(CONTROL, c)
        print(f"signal started at {a.trigger}: {a.bpm} bpm, moving={a.moving}")
    elif a.ruview:
        c = load_control()
        c.setdefault(a.ruview, {"start": time.time(), "moving": False})["ruview"] = True
        write_atomic(CONTROL, c)
        print(f"sensor {a.ruview}-a now reads RuView (simulated CSI)")
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
