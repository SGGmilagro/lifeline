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

from engine import CURRENT_REGION, DATA, REGIONS, SENSORS_FILE, STATE_FILE, Engine, region_list

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
app.mount("/regions", StaticFiles(directory=REGIONS), name="regions")
RUVIEW = "http://127.0.0.1:3000/api/v1"
RUVIEW_TOKEN = ROOT / "ruview.token"


@app.get("/")
def index():
    return FileResponse(ROOT / "map" / "index.html")


@app.get("/sites")
def sites():
    return {"source": engine.meta["source"] + " (replay)", "region": engine.region_name,
            "sites": list(engine.sites.values())}


@app.get("/regions")
def regions():
    return {"current": engine.region, "regions": region_list()}


@app.post("/region")
def set_region(name: str):
    """Switch the whole incident to another pre-loaded region. Restarts the replay at 04:17."""
    global engine
    slug = name.strip().lower().replace("ş", "s").replace("ı", "i").replace("İ", "i")
    known = {r["slug"]: r for r in region_list()}
    if slug not in known:
        return {"ok": False, "result": f"Unknown region {name}. Available: {', '.join(r['name'] for r in known.values())}"}
    CURRENT_REGION.write_text(slug)
    # Survivor signals belong to the old region. The live venue links its building to the room sensor.
    live = json.loads((REGIONS / slug / "sites.json").read_text()).get("live")
    ctrl = {"B-01": {"start": __import__("time").time(), "moving": False, "live": True}} if live else {}
    (DATA / "sensor_control.json").write_text(json.dumps(ctrl))
    new = Engine(speed=engine.speed, region=slug)
    new.reset()
    new._alert("region", None, f"Incident switched to {new.region_name}. Replay restarted at 04:17.")
    new._save()
    engine = new
    return {"ok": True, "result": f"Now working on {new.region_name}: {len(new.sites)} sites ranked."}


@app.get("/alerts")
def alerts():
    return {"alerts": engine.alert_log()}


@app.get("/site")
def site(site_id: str):
    s = engine.sites.get(site_id.upper())
    if not s:
        raise HTTPException(404, f"unknown site {site_id}")
    img = engine.region_dir / "img"
    has = (img / f"before_{s['id']}.png").exists()
    ov = json.loads((img / "overview.json").read_text()) if (img / "overview.json").exists() else {}
    return {**s, "state": engine.state["sites"][s["id"]],
            "before_img": f"/regions/{engine.region}/img/before_{s['id']}.png" if has else None,
            "after_img": f"/regions/{engine.region}/img/after_{s['id']}.png" if has else None,
            "before_date": ov.get("before_date"), "after_date": ov.get("after_date")}


@app.get("/ruview")
def ruview():
    """Live RuView reading for the dashboard. The token stays on the host, never in the browser."""
    try:
        h = {"Authorization": f"Bearer {RUVIEW_TOKEN.read_text().strip()}"}
        with httpx.Client(timeout=2) as c:
            sl = c.get(f"{RUVIEW}/sensing/latest", headers=h).json()
            vs = c.get(f"{RUVIEW}/vital-signs", headers=h).json()
        cls = sl.get("classification", {})
        return {"ok": True, "source": sl.get("source", "simulated"), "presence": cls.get("presence"),
                "motion_level": cls.get("motion_level"), "confidence": cls.get("confidence"),
                "estimated_persons": sl.get("estimated_persons"),
                "breathing_bpm": vs.get("vital_signs", {}).get("breathing_rate_bpm"),
                "vitals_status": vs.get("authority"), "vitals_note": vs.get("abstention_reason"),
                "bound_site": next((sid for sid, v in _control().items() if v.get("ruview")), None),
                "live": _live()}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


# ---------- live sensor proof: a scored 60-second protocol ----------
LIVETEST = {"state": "idle"}
PHASES = [("still", 20), ("walk", 20), ("still", 20)]


def _livetest_run():
    import threading, time as _t
    def run():
        t0 = _t.time()
        LIVETEST.update(state="running", started=t0, samples=[], result=None)
        end = t0 + sum(d for _, d in PHASES)
        while _t.time() < end:
            el = _t.time() - t0
            acc, phase = 0, PHASES[-1][0]
            for ph, d in PHASES:
                if el < acc + d:
                    phase = ph
                    break
                acc += d
            live = _live() or {}
            LIVETEST["samples"].append({"t": round(el, 1), "phase": phase, "moving": bool(live.get("presence")),
                                        "variance": live.get("rssi_variance")})
            _t.sleep(1)
        sm = LIVETEST["samples"]
        walk = [x for x in sm if x["phase"] == "walk" and x["t"] >= 25]      # sensor window is 10 s: allow 5 s to react
        still = [x for x in sm if x["phase"] == "still" and (x["t"] < 20 or x["t"] >= 50)]
        hit = sum(x["moving"] for x in walk)
        fa = sum(x["moving"] for x in still)
        first = next((x["t"] for x in sm if x["phase"] == "walk" and x["moving"]), None)
        LIVETEST.update(state="done", result={
            "walk_detected_s": hit, "walk_window_s": len(walk), "false_alarm_s": fa, "still_window_s": len(still),
            "detection_rate": round(hit / len(walk), 2) if walk else None,
            "false_alarm_rate": round(fa / len(still), 2) if still else None,
            "first_detection_after_walk_start_s": round(first - 20, 1) if first is not None else None,
            "sensor": "GB10 WiFi RSSI, 2 antennas, RuView classifier", "threshold": (_live() or {}).get("threshold")})
        (DATA / "validation_live.json").write_text(json.dumps(LIVETEST, indent=1))
    threading.Thread(target=run, daemon=True).start()


@app.post("/livetest")
def livetest_start():
    if LIVETEST.get("state") == "running":
        return {"ok": False, "result": "already running"}
    _livetest_run()
    return {"ok": True}


@app.get("/livetest")
def livetest():
    out = {k: v for k, v in LIVETEST.items()}
    if out.get("state") == "running":
        el = __import__("time").time() - out["started"]
        acc = 0
        for ph, d in PHASES:
            if el < acc + d:
                out.update(phase=ph, remaining=round(acc + d - el))
                break
            acc += d
    return out


@app.get("/proof")
def proof():
    f = DATA / "validation_vision.json"
    v = json.loads(f.read_text()) if f.exists() else None
    if v:
        v = {k: x for k, x in v.items() if k != "rows"}
    return {"vision": v, "live": LIVETEST.get("result") or (json.loads((DATA / "validation_live.json").read_text()).get("result")
                                                           if (DATA / "validation_live.json").exists() else None)}


def _live():
    f = DATA / "live_sensor.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    d["fresh"] = __import__("time").time() - d["time"] < 15
    d["bound_site"] = next((sid for sid, v in _control().items() if v.get("live")), None)
    return d


def _control():
    f = DATA / "sensor_control.json"
    return json.loads(f.read_text()) if f.exists() else {}


@app.get("/hazard")
def hazard():
    meta = engine.meta
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
    img = engine.region_dir / "img"
    before, after = img / f"before_{s['id']}.png", img / f"after_{s['id']}.png"
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
    ov = engine.region_dir / "img" / "overview.json"
    if ov.exists():                     # Maxar post-event overlay, served locally
        o = json.loads(ov.read_text())
        out.update(bounds=o["bounds"], overlay_img=o["image"])
    tj = engine.region_dir / "tiles" / "tiles.json"
    if tj.exists():                     # zoomable before/after tiles, served locally
        out["tiles"] = json.loads(tj.read_text())
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
