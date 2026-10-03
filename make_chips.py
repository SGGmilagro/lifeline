"""Build-time only: cut before/after satellite chips per site from Maxar Open Data COGs.

Windowed HTTP range reads, no full downloads. Per region writes into data/regions/<slug>/img/:
before_<id>.png, after_<id>.png, overview_after.jpg and overview.json.
Imagery (c) Maxar, CC BY-NC 4.0 (Maxar Open Data, Kahramanmaras-turkey-earthquake-23).

    python make_chips.py --region malatya
    python make_chips.py --all
"""
import argparse
import csv
import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import transform
from rasterio.windows import from_bounds

REGIONS = Path("data/regions")
TSV = Path("data/raw/maxar.tsv")
QUAKE = "2023-02-06"
HALF_M = 75          # 150 m chip around each site
CHIP_PX = 320
OVERVIEW_PX = 1600


def pick_tiles(sites):
    """Choose the tile footprint covering most sites, then the best pre and post image of it."""
    rows = list(csv.DictReader(TSV.open(), delimiter="\t"))
    scored = []
    for r in rows:
        epsg = f"EPSG:{r['proj:epsg']}"
        xs, ys = transform("EPSG:4326", epsg, [s["lon"] for s in sites], [s["lat"] for s in sites])
        x0, y0, x1, y1 = map(float, r["proj:bbox"].split(","))
        r["_n"] = sum(x0 <= x <= x1 and y0 <= y <= y1 for x, y in zip(xs, ys))
        if r["_n"]:
            scored.append(r)
    best = {}
    for r in scored:   # coverage per (quadkey) needs both a pre and a post image
        best.setdefault(r["quadkey"], []).append(r)
    options = []
    for qk, rs in best.items():
        pre = [r for r in rs if r["datetime"][:10] < QUAKE]
        post = [r for r in rs if r["datetime"][:10] >= QUAKE]
        if pre and post:
            # Prefer more data area, less cloud, more nadir; post as soon after the quake as possible.
            p = max(pre, key=lambda r: (float(r["tile:data_area"]) - 5 * float(r["tile:clouds_percent"]), r["datetime"]))
            q = min(post, key=lambda r: (float(r["tile:clouds_percent"]) > 5, -float(r["tile:data_area"]) // 5,
                                         float(r["view:off_nadir"]), r["datetime"]))
            options.append((rs[0]["_n"], p, q))
    if not options:
        return None
    n, pre, post = max(options, key=lambda o: o[0])
    return pre, post


def read_box(src, x0, y0, x1, y1, px):
    win = from_bounds(x0, y0, x1, y1, src.transform)
    arr = src.read([1, 2, 3], window=win, out_shape=(3, px, px), boundless=True, fill_value=0)
    return np.moveaxis(arr, 0, -1)


def make(region):
    rdir = REGIONS / region
    img = rdir / "img"
    img.mkdir(parents=True, exist_ok=True)
    sites = json.loads((rdir / "sites.json").read_text())["sites"]
    tiles = pick_tiles(sites)
    if not tiles:
        print(f"{region}: no before/after Maxar pair covers these sites; map uses dark background")
        return
    pre_r, post_r = tiles
    epsg = f"EPSG:{post_r['proj:epsg']}"
    xs, ys = transform("EPSG:4326", epsg, [s["lon"] for s in sites], [s["lat"] for s in sites])
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
    with env, rasterio.open("/vsicurl/" + pre_r["visual"]) as pre, rasterio.open("/vsicurl/" + post_r["visual"]) as post:
        made = []
        for s, x, y in zip(sites, xs, ys):
            chips = [read_box(src, x - HALF_M, y - HALF_M, x + HALF_M, y + HALF_M, CHIP_PX) for src in (pre, post)]
            if any((c.max(axis=2) == 0).mean() > 0.5 for c in chips):
                continue
            Image.fromarray(chips[0]).save(img / f"before_{s['id']}.png")
            Image.fromarray(chips[1]).save(img / f"after_{s['id']}.png")
            made.append(s["id"])

        cx = [x for s, x in zip(sites, xs) if s["id"] in made] or list(xs)
        cy = [y for s, y in zip(sites, ys) if s["id"] in made] or list(ys)
        pad = 250
        x0, y0 = min(cx) - pad, min(cy) - pad
        side = max(max(cx) - min(cx), max(cy) - min(cy)) + 2 * pad
        ov = read_box(post, x0, y0, x0 + side, y0 + side, OVERVIEW_PX)
        alpha = np.where(ov.max(axis=2) < 12, 0, 255).astype(np.uint8)   # no-data edge -> transparent
        Image.fromarray(np.dstack([ov, alpha]), "RGBA").save(img / "overview_after.png", optimize=True)
        lons, lats = transform(epsg, "EPSG:4326", [x0, x0 + side], [y0, y0 + side])
        (img / "overview.json").write_text(json.dumps({
            "bounds": [[lats[0], lons[0]], [lats[1], lons[1]]],
            "image": f"/regions/{region}/img/overview_after.png",
            "before_date": pre_r["datetime"][:10], "after_date": post_r["datetime"][:10],
            "chips": made, "attribution": "Imagery (c) Maxar, CC BY-NC 4.0"}))
    print(f"{region}: chips for {len(made)}/{len(sites)} sites, before {pre_r['datetime'][:10]}, "
          f"after {post_r['datetime'][:10]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    for r in (sorted(p.name for p in REGIONS.iterdir()) if a.all else [a.region]):
        make(r)
