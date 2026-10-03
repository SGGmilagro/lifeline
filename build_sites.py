"""Build data/sites.json from the Copernicus EMS EMSR648 AOI04 grading product.

Picks the densest cluster of graded buildings in Kahramanmaras and keeps
real coordinates, real damage grades and real building use. Labels are
generic (B-01...), never real names or addresses.

Run once at build time:  python build_sites.py
"""
import json
import math
from pathlib import Path

SRC = Path("data/raw/aoi04/EMSR648_AOI04_GRA_PRODUCT_builtUpA_r1_v1.json")
OUT = Path("data/sites.json")

GRADES = {"Destroyed": 3, "Damaged": 2, "Possibly damaged": 1, "No visible damage": 0}
CENTER = (37.5770, 36.9180)   # lat, lon of the densest Destroyed cluster in AOI04
RADIUS_M = 2500
QUOTA = {3: 10, 2: 3, 1: 3}   # Destroyed, Damaged, Possibly damaged: a realistic mix


def centroid_and_area(geom):
    ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
    lat0 = sum(p[1] for p in ring) / len(ring)
    lon0 = sum(p[0] for p in ring) / len(ring)
    # Shoelace area in metres, using a local flat projection (fine at building scale).
    kx = 111320 * math.cos(math.radians(lat0))
    ky = 110540
    xy = [((p[0] - lon0) * kx, (p[1] - lat0) * ky) for p in ring]
    area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))) / 2
    return lat0, lon0, area


def dist_m(a, b):
    dy = (a[0] - b[0]) * 110540
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def main():
    feats = json.loads(SRC.read_text())["features"]
    cands = []
    for i, f in enumerate(feats):
        p = f["properties"]
        grade = GRADES.get(p["damage_gra"], 0)
        if grade == 0:
            continue
        lat, lon, area = centroid_and_area(f["geometry"])
        d = dist_m((lat, lon), CENTER)
        if d <= RADIUS_M:
            cands.append((grade, d, i, lat, lon, area, p))

    # Most damaged first, then nearest to the cluster centre.
    cands.sort(key=lambda c: (-c[0], c[1]))
    picked = []
    for grade, n in QUOTA.items():
        picked += [c for c in cands if c[0] == grade][:n]
    sites = []
    for n, (grade, d, i, lat, lon, area, p) in enumerate(picked, start=1):
        sites.append({
            "id": f"B-{n:02d}",
            "name": f"Block B-{n:02d}, Kahramanmaras AOI04",
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "grade": grade,
            "grade_label": p["damage_gra"],
            "building_use": p["obj_type"].split("-", 1)[1],
            "use_detail": p["info"].split("-", 1)[1],
            "footprint_m2": round(area),
            "notation": p["notation"],
            "det_method": p["det_method"],
            "source": "copernicus-ems-EMSR648-AOI04",
            "source_feature_index": i,
        })
    aoi_counts = {}
    for f in feats:
        g = f["properties"]["damage_gra"]
        aoi_counts[g] = aoi_counts.get(g, 0) + 1
    meta = {
        "aoi_summary": {"buildings_graded": len(feats), "by_grade": aoi_counts,
                        "method": "Photo-interpretation of post-event satellite imagery"},
        "event": "Kahramanmaras earthquake, 6 Feb 2023 04:17 local",
        "source": "Copernicus EMS Rapid Mapping EMSR648, AOI04 grading product r1 v1",
        "attribution": "Damage grading: Copernicus EMS, (c) European Union",
        "selection": f"{len(sites)} graded buildings within {RADIUS_M} m of {CENTER}",
        "sites": sites,
    }
    OUT.write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    counts = {}
    for s in sites:
        counts[s["grade_label"]] = counts.get(s["grade_label"], 0) + 1
    print(f"wrote {OUT}: {len(sites)} sites {counts}")


if __name__ == "__main__":
    main()
