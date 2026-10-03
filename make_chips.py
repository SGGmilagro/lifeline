"""Build-time only: cut before/after satellite chips per site from Maxar Open Data COGs.

Windowed HTTP range reads, no full downloads. Writes data/img/before_<id>.png,
data/img/after_<id>.png and data/img/overview_after.png + data/img/overview.json.
Imagery (c) Maxar, CC BY-NC 4.0 (Maxar Open Data, Kahramanmaras-turkey-earthquake-23).
"""
import csv
import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import transform
from rasterio.windows import from_bounds

DATA = Path("data")
IMG = DATA / "img"
TSV = DATA / "raw" / "maxar.tsv"
QUADKEY = "031131233233"
PRE_ID, POST_ID = "10300100D797E100", "10300100E291D100"   # 2022-07-26 and 2023-02-11
HALF_M = 75          # 150 m chip around each site
CHIP_PX = 320
OVERVIEW_PX = 1600


def cog_url(catalog_id):
    for r in csv.DictReader(TSV.open(), delimiter="\t"):
        if r["catalog_id"] == catalog_id and r["quadkey"] == QUADKEY:
            return "/vsicurl/" + r["visual"]
    raise SystemExit(f"{catalog_id} not in index")


def read_box(src, x0, y0, x1, y1, px):
    win = from_bounds(x0, y0, x1, y1, src.transform)
    arr = src.read([1, 2, 3], window=win, out_shape=(3, px, px), boundless=True, fill_value=0)
    return np.moveaxis(arr, 0, -1)


def main():
    IMG.mkdir(parents=True, exist_ok=True)
    sites = json.loads((DATA / "sites.json").read_text())["sites"]
    xs, ys = transform("EPSG:4326", "EPSG:32637", [s["lon"] for s in sites], [s["lat"] for s in sites])
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
    with env, rasterio.open(cog_url(PRE_ID)) as pre, rasterio.open(cog_url(POST_ID)) as post:
        made = []
        for s, x, y in zip(sites, xs, ys):
            chips = [read_box(src, x - HALF_M, y - HALF_M, x + HALF_M, y + HALF_M, CHIP_PX) for src in (pre, post)]
            if any((c.max(axis=2) == 0).mean() > 0.5 for c in chips):
                print(f"{s['id']}: outside imagery, skipped")
                continue
            Image.fromarray(chips[0]).save(IMG / f"before_{s['id']}.png")
            Image.fromarray(chips[1]).save(IMG / f"after_{s['id']}.png")
            made.append(s["id"])
        print(f"chips: {len(made)} sites {made}")

        # One post-event overview around the covered sites, for the map overlay.
        cx = [x for s, x in zip(sites, xs) if s["id"] in made]
        cy = [y for s, y in zip(sites, ys) if s["id"] in made]
        pad = 250
        x0, x1, y0, y1 = min(cx) - pad, max(cx) + pad, min(cy) - pad, max(cy) + pad
        side = max(x1 - x0, y1 - y0)
        x1, y1 = x0 + side, y0 + side
        Image.fromarray(read_box(post, x0, y0, x1, y1, OVERVIEW_PX)).save(IMG / "overview_after.jpg", quality=85)
        lons, lats = transform("EPSG:32637", "EPSG:4326", [x0, x1], [y0, y1])
        (IMG / "overview.json").write_text(json.dumps({
            "bounds": [[lats[0], lons[0]], [lats[1], lons[1]]],
            "image": "/img/overview_after.jpg", "date": "2023-02-11",
            "attribution": "Imagery (c) Maxar, CC BY-NC 4.0"}))
        print("overview written")


if __name__ == "__main__":
    main()
