"""Proof for the satellite half: score the local AI against Copernicus expert grading.

For each region, samples buildings of every grade INCLUDING undamaged ones (controls) that the
ranking never used, cuts before/after Maxar chips, asks the local qwen3.5 (on the GB10) to judge
each pair blind, and compares with the Copernicus grade. Writes data/validation_vision.json.

    python validate_vision.py [--per-grade 4]
"""
import argparse
import base64
import io
import json
import random
import time
from pathlib import Path

import httpx
import rasterio
from PIL import Image
from rasterio.warp import transform

from build_sites import ALL, GRADES, centroid_and_area
from make_chips import HALF_M, CHIP_PX, pick_tiles, read_box

RAW = Path("data/raw")
OUT = Path("data/validation_vision.json")
OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3.5:9b-128k"
PROMPT = ("Two satellite images of the same building, before and after an earthquake. "
          "Answer one word, collapsed, damaged or standing, then one sentence why.")


def png_b64(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-grade", type=int, default=4)
    a = ap.parse_args()
    rng = random.Random(42)                      # fixed seed: the sample is reproducible
    rows = []
    for aoi, region in ALL.items():
        feats = json.loads(next((RAW / aoi).glob("*builtUp*.json")).read_text())["features"]
        used = {s["source_feature_index"] for s in
                json.loads((Path("data/regions") / region.lower() / "sites.json").read_text())["sites"]}
        by_grade = {}
        for i, f in enumerate(feats):
            g = f["properties"].get("damage_gra")
            if g in GRADES and i not in used:
                by_grade.setdefault(g, []).append(i)
        sample = []
        for g, idx in by_grade.items():
            rng.shuffle(idx)
            sample += [(g, i) for i in idx[:a.per_grade * 3]]    # extra, some fall outside imagery
        pts = []
        for g, i in sample:
            lat, lon, _ = centroid_and_area(feats[i]["geometry"])
            pts.append({"grade": g, "idx": i, "lat": lat, "lon": lon})
        tiles = pick_tiles(pts)
        if not tiles:
            continue
        pre_r, post_r = tiles
        epsg = f"EPSG:{post_r['proj:epsg']}"
        xs, ys = transform("EPSG:4326", epsg, [p["lon"] for p in pts], [p["lat"] for p in pts])
        env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
        done = {}
        with env, rasterio.open("/vsicurl/" + pre_r["visual"]) as pre, \
                rasterio.open("/vsicurl/" + post_r["visual"]) as post, httpx.Client(timeout=120) as c:
            for p, x, y in zip(pts, xs, ys):
                if done.get(p["grade"], 0) >= a.per_grade:
                    continue
                chips = [read_box(src, x - HALF_M, y - HALF_M, x + HALF_M, y + HALF_M, CHIP_PX) for src in (pre, post)]
                if any((ch.max(axis=2) == 0).mean() > 0.2 for ch in chips):
                    continue
                t0 = time.time()
                r = c.post(OLLAMA, json={"model": MODEL, "prompt": PROMPT, "images": [png_b64(ch) for ch in chips],
                                         "stream": False, "think": False, "keep_alive": -1}).json()
                ans = r["response"].strip()
                word = ans.split()[0].strip(".,:*").lower() if ans else ""
                rows.append({"region": region, "feature_index": p["idx"], "copernicus": p["grade"],
                             "ai": word, "answer": ans, "seconds": round(time.time() - t0, 2)})
                done[p["grade"]] = done.get(p["grade"], 0) + 1
                print(f"{region:14} {p['grade']:18} -> {word:9} ({rows[-1]['seconds']} s)", flush=True)

    # Score: did the AI flag structural damage (collapsed/damaged) where experts graded it?
    def damaged(g):
        return g in ("Destroyed", "Damaged")
    tp = sum(1 for r in rows if damaged(r["copernicus"]) and r["ai"] in ("collapsed", "damaged"))
    fn = sum(1 for r in rows if damaged(r["copernicus"]) and r["ai"] not in ("collapsed", "damaged"))
    tn = sum(1 for r in rows if r["copernicus"] == "No visible damage" and r["ai"] == "standing")
    fp = sum(1 for r in rows if r["copernicus"] == "No visible damage" and r["ai"] != "standing")
    destroyed = [r for r in rows if r["copernicus"] == "Destroyed"]
    matrix = {}
    for r in rows:
        matrix.setdefault(r["copernicus"], {}).setdefault(r["ai"], 0)
        matrix[r["copernicus"]][r["ai"]] += 1
    summary = {
        "model": MODEL, "device": "Dell Pro Max GB10 (local)", "n": len(rows),
        "ground_truth": "Copernicus EMS EMSR648 expert grading (photo-interpretation)",
        "imagery": "Maxar Open Data before/after, CC BY-NC 4.0",
        "sample": f"random, seed 42, up to {a.per_grade} per grade per region, buildings not used in the demo",
        "damage_recall": round(tp / (tp + fn), 2) if tp + fn else None,
        "destroyed_flagged_collapsed": round(sum(r["ai"] == "collapsed" for r in destroyed) / len(destroyed), 2) if destroyed else None,
        "undamaged_correctly_standing": round(tn / (tn + fp), 2) if tn + fp else None,
        "counts": {"tp": tp, "fn": fn, "tn": tn, "fp": fp},
        "matrix": matrix,
        "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2] if rows else None,
        "rows": rows,
    }
    OUT.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1))


if __name__ == "__main__":
    main()
