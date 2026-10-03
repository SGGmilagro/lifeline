"""Live WiFi sensing in this room: the GB10's own WiFi signal through RuView's classifier.

Samples the real RSSI of the GB10's WiFi link with `iw` (5 Hz), then runs RuView's
RssiFeatureExtractor + PresenceClassifier (RuView v1 commodity-WiFi pipeline) on it.
RSSI-only sensing gives coarse presence and motion, never breathing. Writes data/live_sensor.json.

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
RATE_HZ = 5
WINDOW_S = 10


def read_iw(iface):
    out = subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=2).stdout
    sig = re.search(r"signal:\s*(-?\d+)", out)
    if not sig:
        return None
    rx = re.search(r"RX:\s*(\d+) bytes", out)
    tx = re.search(r"TX:\s*(\d+) bytes", out)
    rssi = float(sig.group(1))
    return WifiSample(timestamp=time.time(), rssi_dbm=rssi, noise_dbm=-95.0,
                      link_quality=max(0.0, min(1.0, (rssi + 90) / 60)),
                      tx_bytes=int(tx.group(1)) if tx else 0, rx_bytes=int(rx.group(1)) if rx else 0,
                      retry_count=0, interface=iface)


def write_atomic(obj):
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlP9s9")
    a = ap.parse_args()
    samples = deque(maxlen=RATE_HZ * 60)
    extractor = RssiFeatureExtractor(window_seconds=WINDOW_S)
    classifier = PresenceClassifier()
    last_write = 0.0
    print(f"live RuView RSSI sensing on {a.iface} -> {OUT}")
    while True:
        s = read_iw(a.iface)
        if s:
            samples.append(s)
        if time.time() - last_write >= 2 and len(samples) >= RATE_HZ * 4:
            r = classifier.classify(extractor.extract(list(samples)))
            write_atomic({"source": "ruview-rssi-live", "interface": a.iface, "time": time.time(),
                          "presence": bool(r.presence_detected), "motion_level": r.motion_level.value,
                          "confidence": round(float(r.confidence), 2), "rssi_dbm": samples[-1].rssi_dbm,
                          "rssi_variance": round(float(r.rssi_variance), 3), "details": r.details})
            last_write = time.time()
        time.sleep(1 / RATE_HZ)


if __name__ == "__main__":
    main()
