"""Build-time only: local zoomable satellite tiles (before and after) for each region.

Reads Maxar Open Data COGs with windowed requests, reprojects to Web Mercator and writes
standard XYZ PNG tiles to data/regions/<slug>/tiles/{before,after}/{z}/{x}/{y}.png.
The dashboard serves them locally, so the map stays sharp with the internet off.
Imagery (c) Maxar, CC BY-NC 4.0.

    python make_tiles.py --all
"""
import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
import rasterio.transform

from make_chips import pick_tiles

REGIONS = Path("data/regions")
ZOOMS = range(14, 19)          # z18 is about 0.47 m/pixel here, close to Maxar's native 0.5 m
PAD_M = 600
R = 6378137.0


def merc(lon, lat):
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def tile_range(bounds, z):
    (lon0, lat0), (lon1, lat1) = bounds
    def xy(lon, lat):
        n = 2 ** z
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
        return x, y
    x0, y1 = xy(lon0, lat0)
    x1, y0 = xy(lon1, lat1)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def tile_bounds(z, x, y):
    size = 2 * math.pi * R / 2 ** z
    minx = -math.pi * R + x * size
    maxy = math.pi * R - y * size
    return minx, maxy - size, minx + size, maxy


def make(region):
    rdir = REGIONS / region
    sites = json.loads((rdir / "sites.json").read_text())["sites"]
    picked = pick_tiles(sites)
    if not picked:
        print(f"{region}: no imagery")
        return
    lats = [s["lat"] for s in sites]
    lons = [s["lon"] for s in sites]
    dlat = PAD_M / 110540
    dlon = PAD_M / (111320 * math.cos(math.radians(sum(lats) / len(lats))))
    bounds = ((min(lons) - dlon, min(lats) - dlat), (max(lons) + dlon, max(lats) + dlat))
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                       GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE", VSI_CACHE_SIZE=200_000_000)
    count = 0
    for label, row in zip(("before", "after"), picked):
        with env, rasterio.open("/vsicurl/" + row["visual"]) as src:
            jobs = [(z, x, y) for z in ZOOMS for x, y in tile_range(bounds, z)]

            def one(job):
                z, x, y = job
                out = rdir / "tiles" / label / str(z) / str(x) / f"{y}.png"
                if out.exists():
                    return 0
                b = tile_bounds(z, x, y)
                tf = rasterio.transform.from_bounds(*b, 256, 256)
                with WarpedVRT(src, crs="EPSG:3857", transform=tf, width=256, height=256,
                               resampling=Resampling.bilinear, src_nodata=0, nodata=0) as vrt:
                    arr = vrt.read([1, 2, 3])
                rgb = np.moveaxis(arr, 0, -1)
                if rgb.max() == 0:
                    return 0
                alpha = np.where(rgb.max(axis=2) < 12, 0, 255).astype(np.uint8)
                out.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(np.dstack([rgb, alpha]), "RGBA").save(out, optimize=True)
                return 1

            # One VRT is not thread-safe for reads; keep it simple and sequential per image.
            count += sum(map(one, jobs))
    meta = {"bounds": [[bounds[0][1], bounds[0][0]], [bounds[1][1], bounds[1][0]]],
            "minzoom": min(ZOOMS), "maxzoom": max(ZOOMS),
            "before_date": picked[0]["datetime"][:10], "after_date": picked[1]["datetime"][:10],
            "url": f"/regions/{region}/tiles/{{layer}}/{{z}}/{{x}}/{{y}}.png",
            "attribution": "Imagery (c) Maxar, CC BY-NC 4.0"}
    (rdir / "tiles" / "tiles.json").write_text(json.dumps(meta))
    print(f"{region}: {count} tiles written")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    regions = sorted(p.name for p in REGIONS.iterdir()) if a.all else [a.region]
    with ThreadPoolExecutor(max_workers=len(regions)) as ex:   # regions in parallel, separate datasets
        list(ex.map(make, regions))
