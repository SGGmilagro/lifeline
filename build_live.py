"""Build-time only: the "live" region, our own venue, for the live-sensor drill.

Uses the venue's OpenStreetMap footprint (fetched once via Nominatim into data/raw/live/nom.json)
and MassGIS 2025 statewide aerial imagery tiles, stored locally so the map works offline.
There is no disaster here: the site is labeled a live drill, and its sensor is the real WiFi
in this room (ruview_live.py).

    python build_live.py
"""
import json
import math
import time
from pathlib import Path

import httpx

RAW = Path("data/raw/live/nom.json")
OUT = Path("data/regions/live")
TILE_URL = ("https://tiles.arcgis.com/tiles/hGdibHYSPO59RG1h/arcgis/rest/services/"
            "Massachusetts_Aerial_Imagery_2025/MapServer/tile/{z}/{y}/{x}")
ZOOMS = range(15, 21)
PAD_M = 300


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def main():
    nom = json.loads(RAW.read_text())[0]
    lat, lon = float(nom["lat"]), float(nom["lon"])
    ring = nom["geojson"]["coordinates"][0]
    kx, ky = 111320 * math.cos(math.radians(lat)), 110540
    xy = [((p[0] - lon) * kx, (p[1] - lat) * ky) for p in ring]
    area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))) / 2
    extra = nom.get("extratags") or {}
    levels = extra.get("building:levels")

    OUT.mkdir(parents=True, exist_ok=True)
    site = {
        "id": "B-01", "name": "Our venue (live drill)", "lat": round(lat, 6), "lon": round(lon, 6),
        "grade": 0, "grade_label": "No disaster (live drill)",
        "building_use": "Education", "use_detail": extra.get("operator", ""),
        "kind": f"our venue{f' ({levels} floors)' if levels else ''}, live WiFi sensor",
        "footprint_m2": round(area), "outline": ring, "notation": "OpenStreetMap footprint",
        "det_method": "Live WiFi sensing (RuView RSSI), no damage data", "source": "openstreetmap+live-wifi",
    }
    meta = {
        "region": "Live venue", "aoi": "LIVE", "live": True,
        "event": "Live drill at our venue: no disaster, real WiFi sensor",
        "source": "OpenStreetMap footprint; MassGIS 2025 aerial imagery; live WiFi via RuView",
        "attribution": "Imagery: MassGIS 2025 aerial imagery. Footprint: (c) OpenStreetMap contributors",
        "aoi_summary": {"buildings_graded": 0, "by_grade": {}, "method": "No damage grading: live drill"},
        "selection": "Our venue building only", "sites": [site],
    }
    (OUT / "sites.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))

    dlat, dlon = PAD_M / ky, PAD_M / kx
    n = 0
    with httpx.Client(timeout=20, headers={"User-Agent": "lifeline-hackathon-build/1.0"}) as c:
        for z in ZOOMS:
            x0, y0 = tile_xy(lat + dlat, lon - dlon, z)
            x1, y1 = tile_xy(lat - dlat, lon + dlon, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    f = OUT / "tiles" / "after" / str(z) / str(x) / f"{y}.jpg"
                    if f.exists():
                        continue
                    r = c.get(TILE_URL.format(z=z, x=x, y=y))
                    if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_bytes(r.content)
                        n += 1
                    time.sleep(0.02)
    (OUT / "tiles" / "tiles.json").write_text(json.dumps({
        "bounds": [[lat - dlat, lon - dlon], [lat + dlat, lon + dlon]],
        "minzoom": min(ZOOMS), "maxzoom": max(ZOOMS), "before_date": None, "after_date": "2025 aerial",
        "url": "/regions/live/tiles/{layer}/{z}/{x}/{y}.jpg",
        "attribution": "Imagery: MassGIS 2025 aerial imagery"}))
    print(f"live venue: footprint {round(area)} m2, {levels} floors, {n} imagery tiles")


if __name__ == "__main__":
    main()
