"""Live WiFi sensing in this room: the GB10's own WiFi signal through RuView's classifier.

Samples the real RSSI of the GB10's WiFi link per antenna (2 chains) with `iw station dump`
at 10 Hz, while pinging the router so every sample is a fresh measurement. Each antenna is a
separate signal path through the room; RuView's RssiFeatureExtractor + PresenceClassifier
(RuView v1 commodity-WiFi pipeline) runs on each, and movement on either path counts.
RSSI-only sensing detects movement near the GB10. It cannot count people, cannot see people
who are sitting still, and never measures breathing. Writes data/live_sensor.json.

    python ruview_live.py [--iface wlP9s9]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path.home() / "RuView" / "archive"))
from v1.src.sensing.classifier import PresenceClassifier  # noqa: E402
from v1.src.sensing.feature_extractor import RssiFeatureExtractor  # noqa: E402
from v1.src.sensing.rssi_collector import WifiSample  # noqa: E402

OUT = Path(__file__).parent / "data" / "live_sensor.json"
CALIB = Path(__file__).parent / "data" / "live_threshold.json"   # written by the dashboard calibration
RATE_HZ = 10
WINDOW_S = 10
# RSSI variance sees MOVEMENT, not people: four people sitting still next to the GB10 gave
# 0.9-1.4 dB^2. RuView's default threshold (0.5 dB^2) fires on that constantly, so we set the
# alarm above it to mean "someone is moving nearby". Still people are not detected.
VAR_THRESH = float(os.environ.get("LIFELINE_LIVE_VAR", "2.0"))


def sample(rssi, iface, chain):
    return WifiSample(timestamp=time.time(), rssi_dbm=rssi, noise_dbm=-95.0,
                      link_quality=max(0.0, min(1.0, (rssi + 90) / 60)),
                      tx_bytes=0, rx_bytes=0, retry_count=0, interface=f"{iface}/{chain}")


def read_chains(iface):
    """Per-antenna RSSI of the current link, e.g. 'signal: -61 [-66, -62] dBm' -> [-66, -62]."""
    out = subprocess.run(["iw", "dev", iface, "station", "dump"], capture_output=True, text=True, timeout=2).stdout
    m = re.search(r"\n\s*signal:\s*(-?\d+)\s*\[([-\d, ]+)\]", out)
    if not m:
        return None
    return [float(v) for v in m.group(2).split(",")]


def gateway():
    out = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout
    m = re.search(r"default via (\S+)", out)
    return m.group(1) if m else None


def write_atomic(obj):
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlP9s9")
    a = ap.parse_args()
    gw = gateway()
    # Small steady traffic to our own router keeps the per-antenna RSSI fresh (local network only).
    pinger = subprocess.Popen(["ping", "-i", "0.05", "-q", gw], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) if gw else None
    chains = None
    extractor = RssiFeatureExtractor(window_seconds=WINDOW_S)
    classifier = PresenceClassifier(presence_variance_threshold=VAR_THRESH)
    last_write = 0.0
    print(f"live RuView RSSI sensing on {a.iface}, per antenna, router {gw} -> {OUT}")
    try:
        while True:
            vals = read_chains(a.iface)
            if vals:
                if chains is None:
                    chains = [deque(maxlen=RATE_HZ * 60) for _ in vals]
                for buf, v, i in zip(chains, vals, range(len(vals))):
                    buf.append(sample(v, a.iface, i))
            if chains and time.time() - last_write >= 0.5 and len(chains[0]) >= RATE_HZ * 4:
                thresh = json.loads(CALIB.read_text())["threshold"] if CALIB.exists() else VAR_THRESH
                classifier = PresenceClassifier(presence_variance_threshold=thresh)
                results = [classifier.classify(extractor.extract(list(buf))) for buf in chains]
                best = max(results, key=lambda r: r.rssi_variance)
                moving = any(r.presence_detected for r in results)
                write_atomic({"source": "ruview-rssi-live", "interface": a.iface, "time": time.time(),
                              "presence": moving, "motion_level": best.motion_level.value if moving else "absent",
                              "threshold": thresh, "calibrated": CALIB.exists(),
                              "confidence": round(float(best.confidence), 2),
                              "trace": [[round(x.timestamp, 1), x.rssi_dbm] for x in list(chains[0])[-150:]],
                              "trace2": [x.rssi_dbm for x in list(chains[1])[-150:]] if len(chains) > 1 else [],
                              "rssi_dbm": [buf[-1].rssi_dbm for buf in chains],
                              "rssi_variance": round(float(best.rssi_variance), 3),
                              "per_antenna_variance": [round(float(r.rssi_variance), 3) for r in results],
                              "details": best.details})
                last_write = time.time()
            time.sleep(1 / RATE_HZ)
    finally:
        if pinger:
            pinger.terminate()


if __name__ == "__main__":
    main()
