"""Lifeline toolbox: FastAPI on the host, port 8090. Everything local.

    python toolbox.py [--speed 120] [--reset]

GET  /sites /hazard /sensors /assess?site_id= /brief /map
POST /ack?site=B-07|all   /scan?site=B-07   /delivered?ids=1,2   /reset
GET  /  -> live map (map/index.html), /vendor/*, /img/*
"""
import argparse
import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine import DATA, SENSORS_FILE, STATE_FILE, Engine

ROOT = Path(__file__).parent
OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3.5:9b-128k"     # the only model this product uses
ASSESS_PROMPT = ("Two satellite images of the same building, before and after an earthquake. "
                 "Answer one word, collapsed, damaged or standing, then one sentence why.")

engine: Engine = None


async def ticker():
    while True:
        await asyncio.to_thread(engine.tick)
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(title="Lifeline toolbox", lifespan=lifespan)
app.mount("/vendor", StaticFiles(directory=ROOT / "map" / "vendor"), name="vendor")
(DATA / "img").mkdir(exist_ok=True)
app.mount("/img", StaticFiles(directory=DATA / "img"), name="img")


@app.get("/")
def index():
    return FileResponse(ROOT / "map" / "index.html")


@app.get("/sites")
def sites():
    return {"source": "Copernicus EMS EMSR648 AOI04 grading (replay)",
            "sites": list(engine.sites.values())}


@app.get("/hazard")
def hazard():
    meta = json.loads((DATA / "sites.json").read_text())
    return {"event": meta["event"], "source": meta["source"], "aoi_summary": meta["aoi_summary"],
            "hours_since_collapse": round(engine.hours_since_collapse(), 1),
            "replay_clock": engine.replay_clock()}


@app.get("/sensors")
def sensors():
    if not SENSORS_FILE.exists():
        return {"source": "no sensor feed", "readings": []}
    return json.loads(SENSORS_FILE.read_text())


@app.get("/assess")
async def assess(site_id: str):
    s = engine.sites.get(site_id.upper())
    if not s:
        raise HTTPException(404, f"unknown site {site_id}")
    before, after = DATA / "img" / f"before_{s['id']}.png", DATA / "img" / f"after_{s['id']}.png"
    base = {"site_id": s["id"], "copernicus_grade": s["grade_label"]}
    if before.exists() and after.exists():
        imgs = [base64.b64encode(p.read_bytes()).decode() for p in (before, after)]
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(OLLAMA, json={"model": MODEL, "prompt": ASSESS_PROMPT, "images": imgs,
                                               "stream": False, "think": False, "keep_alive": -1})
                r.raise_for_status()
                ans = r.json()["response"].strip()
                word = ans.split()[0].strip(".,").lower() if ans else ""
                agrees = {"collapsed": "Destroyed", "damaged": "Damaged"}.get(word) == s["grade_label"] or (word == "standing" and s["grade"] == 0)
                return {**base, "method": f"local-vision ({MODEL}), second opinion only, not used in ranking",
                        "answer": ans, "agrees_with_copernicus": agrees}
        except Exception as e:
            base["vision_error"] = str(e)[:200]
    return {**base, "method": "copernicus-grading", "answer": s["grade_label"]}


@app.get("/brief")
def brief():
    return engine.brief()


@app.get("/map")
def map_view():
    out = engine.map_view()
    ov = DATA / "img" / "overview.json"
    if ov.exists():                     # Maxar post-event overlay, served locally
        o = json.loads(ov.read_text())
        out.update(bounds=o["bounds"], overlay_img=o["image"])
    return out


@app.post("/ack")
def ack(site: str):
    return {"result": engine.ack(site)}


@app.post("/scan")
def scan(site: str):
    return {"result": engine.request_scan(site)}


@app.post("/delivered")
def delivered(ids: str):
    engine.mark_delivered({int(i) for i in ids.split(",") if i.strip()})
    return {"ok": True}


@app.post("/reset")
def reset():
    engine.reset()
    return {"ok": True, "replay_clock": engine.replay_clock()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=120, help="replay seconds per real second")
    ap.add_argument("--reset", action="store_true", help="restart the replay from 04:17")
    a = ap.parse_args()
    engine = Engine(speed=a.speed)
    if a.reset or not STATE_FILE.exists():
        engine.reset()
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="warning")
