"""Build data/regions/<slug>/sites.json from a Copernicus EMS EMSR648 grading product.

Finds the densest cluster of destroyed buildings in the area of interest, then keeps a
realistic mix of graded buildings around it. Real coordinates, real grades, real building
use. Labels are generic (B-01...), never real names or addresses.

Run once per region at build time:
    python build_sites.py --aoi aoi04 --name Kahramanmaras
    python build_sites.py --all
"""
import argparse
import glob
import json
import math
from pathlib import Path

RAW = Path("data/raw")
REGIONS = Path("data/regions")
GRADES = {"Destroyed": 3, "Damaged": 2, "Possibly damaged": 1, "No visible damage": 0}
RADIUS_M = 2500
CLUSTER_M = 600
QUOTA = {3: 10, 2: 3, 1: 3}   # Destroyed, Damaged, Possibly damaged: a realistic mix
N_SITES = 16
ALL = {"aoi04": "Kahramanmaras", "aoi05": "Malatya", "aoi02": "Adiyaman",
       "aoi11": "Antakya", "aoi01": "Gaziantep"}


def centroid_and_area(geom):
    """Centroid (lat, lon) and footprint in m2. Point features have no footprint (None)."""
    if geom["type"] == "Point":
        return geom["coordinates"][1], geom["coordinates"][0], None
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


def build(aoi, name):
    src = glob.glob(str(RAW / aoi / "*builtUp*.json"))[0]
    feats = json.loads(Path(src).read_text())["features"]
    graded = []
    for i, f in enumerate(feats):
        p = f["properties"]
        grade = GRADES.get(p.get("damage_gra"), 0)
        if grade:
            lat, lon, area = centroid_and_area(f["geometry"])
            graded.append((grade, i, lat, lon, area, p))

    # Centre = the most-damaged building with the most damage weight around it (densest cluster).
    top = max(g[0] for g in graded)

    def weight(c):
        return sum(g * g for g, _, la, lo, _, _ in graded if dist_m((c[2], c[3]), (la, lo)) <= CLUSTER_M)
    best = max((c for c in graded if c[0] == top), key=weight)
    centre = (best[2], best[3])

    cands = [(dist_m((c[2], c[3]), centre),) + c for c in graded]
    cands = sorted((c for c in cands if c[0] <= RADIUS_M), key=lambda c: (-c[1], c[0]))
    picked = []
    for grade, n in QUOTA.items():
        picked += [c for c in cands if c[1] == grade][:n]
    for c in cands:   # top up if a grade is scarce here
        if len(picked) >= N_SITES:
            break
        if c not in picked:
            picked.append(c)

    sites = []
    for n, (d, grade, i, lat, lon, area, p) in enumerate(picked[:N_SITES], start=1):
        sites.append({
            "id": f"B-{n:02d}",
            "name": f"Block B-{n:02d}, {name}",
            "lat": round(lat, 6), "lon": round(lon, 6),
            "grade": grade, "grade_label": p["damage_gra"],
            "building_use": p.get("obj_type", "Unknown").split("-", 1)[-1],
            "use_detail": p.get("info", "").split("-", 1)[-1],
            "footprint_m2": round(area) if area is not None else None,
            "notation": p.get("notation"),
            "det_method": p.get("det_method"),
            "source": f"copernicus-ems-EMSR648-{aoi.upper()}",
            "source_feature_index": i,
        })
    counts = {}
    for f in feats:
        g = f["properties"].get("damage_gra")
        counts[g] = counts.get(g, 0) + 1
    out = REGIONS / name.lower()
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "region": name, "aoi": aoi.upper(),
        "event": "Kahramanmaras earthquake, 6 Feb 2023 04:17 local",
        "source": f"Copernicus EMS Rapid Mapping EMSR648, {aoi.upper()} ({name}) grading product",
        "attribution": "Damage grading: Copernicus EMS, (c) European Union",
        "aoi_summary": {"buildings_graded": len(feats), "by_grade": counts,
                        "method": "Photo-interpretation of post-event satellite imagery"},
        "selection": f"{len(sites)} graded buildings within {RADIUS_M} m of the densest damage cluster",
        "sites": sites,
    }
    (out / "sites.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    mix = {}
    for s in sites:
        mix[s["grade_label"]] = mix.get(s["grade_label"], 0) + 1
    print(f"{name}: {len(sites)} sites {mix}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aoi")
    ap.add_argument("--name")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    for aoi, name in (ALL.items() if a.all else [(a.aoi, a.name)]):
        build(aoi, name)
