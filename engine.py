"""Lifeline engine: rank sites, propose dispatches, read scans, flag survivors, escalate.

score = grade_weight x occupancy_factor x time_factor
  grade_weight     Copernicus grade: Destroyed 1.0, Damaged 0.6, Possibly damaged 0.3, none 0
  occupancy_factor use_factor x size_factor
                   use_factor: residential at 04:17 = 1.0 (night, people at home),
                               non-residential at night = 0.3
                   size_factor: sqrt(footprint_m2 / 20000), clamped to 0.3..1.0
                               (bigger block, more people likely inside)
  time_factor      0.5 ** (hours_since_collapse / 48). A simple ranking weight
                   that falls with time, not a medical survival estimate.

Order: escalated survivors, confirmed, possible, everything else by score,
then sites with no signal after 2 scans. "No signal" never means "nobody".
"""
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).parent / "data"
REGIONS = DATA / "regions"
CURRENT_REGION = DATA / "current_region"
DEFAULT_REGION = "kahramanmaras"


def current_region():
    return CURRENT_REGION.read_text().strip() if CURRENT_REGION.exists() else DEFAULT_REGION


def region_list():
    out = []
    for d in sorted(REGIONS.iterdir()):
        if (d / "sites.json").exists():
            m = json.loads((d / "sites.json").read_text())
            imagery = (d / "img" / "overview.json").exists() or (d / "tiles" / "tiles.json").exists()
            out.append({"slug": d.name, "name": m["region"], "sites": len(m["sites"]),
                        "buildings_graded": m["aoi_summary"]["buildings_graded"],
                        "destroyed": m["aoi_summary"]["by_grade"].get("Destroyed", 0),
                        "live": bool(m.get("live")), "imagery": imagery})
    return out
SENSORS_FILE = DATA / "sensors.json"
STATE_FILE = DATA / "state.json"

COLLAPSE_LOCAL = datetime(2023, 2, 6, 4, 17)   # Turkey local time (UTC+3)
GRADE_WEIGHT = {3: 1.0, 2: 0.6, 1: 0.3, 0: 0.0}
TEAMS = ["T1", "T2", "T3"]
CONFIRM_GAP_S = 20          # two scans this far apart also confirm
ESCALATE_BPM = 10           # breathing below this escalates
ESCALATE_DROP = 0.30        # or a drop of 30% ...
ESCALATE_WINDOW_S = 120     # ... within 2 minutes
HELP_TEXT = """👋 LIFELINE · earthquake rescue desk

I rank collapsed buildings using satellite damage maps, track your 3 rescue teams, and alert you when sensors pick up someone alive. You decide every move.

Send me:
• status → where to dig now (top 5 buildings)
• who is alive → people detected so far
• ack 5 → approve what I proposed for Building 5
• ack all → approve everything waiting
• scan 7 → ask for a team at Building 7
• go Malatya → switch city (Kahramanmaras, Malatya, Adiyaman, Antakya, Gaziantep)\n• go live → our own building, with the live WiFi sensor in this room

🟢 survivor confirmed · 🟡 possible survivor · 🚨 getting weaker · ⚪ no signal yet (people may still be inside)

Real satellite damage data (Copernicus) from the 6 Feb 2023 Turkey earthquake, replayed fast. Sensors simulated plus live WiFi. Everything runs on this machine."""

STATUS_ORDER = {"confirmed_survivor": 1, "possible_survivor": 2, "unsearched": 3,
                "dispatched": 3, "scanning": 3, "no_signal_2_scans": 4}


def dist_m(a, b):
    dy = (a[0] - b[0]) * 110540
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def norm_site(t):
    """'5', 'b5', 'B-05', 'building 5' -> 'B-05'. Anything else passes through upper-cased."""
    m = re.fullmatch(r"(?i)\s*(?:building\s*|b-?)?(\d{1,2})\s*", str(t))
    return f"B-{int(m.group(1)):02d}" if m else str(t).strip().upper()


def friendly(text):
    """Plain words for first-time users: B-05 -> Building 5, Team T1 -> Team 1, ack B-05 -> ack 5."""
    text = re.sub(r"\b(ack|scan) B-(\d{2})\b", lambda m: f"{m.group(1)} {int(m.group(2))}", text)
    text = re.sub(r"\bB-(\d{2})\b", lambda m: f"Building {int(m.group(1))}", text)
    return re.sub(r"\bTeam T(\d)\b", r"Team \1", text)


def breath(p):
    if p["breathing_bpm"] is None:
        return "breathing not measured (sensor abstained)"
    return f"breathing {p['breathing_bpm']} bpm ({p['trend']})"


def write_atomic(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


class Engine:
    def __init__(self, speed=120, region=None):
        self.lock = threading.Lock()
        self.region = region or current_region()
        self.region_dir = REGIONS / self.region
        meta = json.loads((self.region_dir / "sites.json").read_text())
        self.meta = meta
        self.region_name = meta["region"]
        self.event = meta["event"]
        self.sites = {s["id"]: s for s in meta["sites"]}
        lats = [s["lat"] for s in self.sites.values()]
        lons = [s["lon"] for s in self.sites.values()]
        self.staging = (sum(lats) / len(lats), sum(lons) / len(lons))
        self.speed = speed
        saved = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        self.state = saved if saved.get("region") == self.region else self.fresh_state()

    # ---------- state ----------
    def fresh_state(self):
        return {
            "region": self.region,
            "start_real": time.time(), "speed": self.speed, "last_tick": 0, "next_alert": 1,
            "teams": {t: {"site": None, "lat": self.staging[0], "lon": self.staging[1]} for t in TEAMS},
            "sites": {sid: {"status": "unsearched", "team": None, "scans": 0, "detections": [],
                            "bpm_hist": [], "moving": None, "source": None, "escalated": False}
                      for sid in self.sites},
            "proposals": {},        # site_id -> team awaiting the commander's ack
            "survivor_ack": [],     # site_ids whose survivor/escalation alert awaits ack
            "alerts": [],
        }

    def reset(self):
        with self.lock:
            self.state = self.fresh_state()
            self._propose()
            self._save()

    def _save(self):
        write_atomic(STATE_FILE, self.state)

    def _alert(self, type_, site_id, text, **data):
        st = self.state
        st["alerts"].append({"id": st["next_alert"], "type": type_, "site_id": site_id, "data": data,
                             "text": friendly(text), "delivered": False, "real_time": time.time()})
        st["next_alert"] += 1

    # ---------- clock ----------
    def hours_since_collapse(self):
        return (time.time() - self.state["start_real"]) * self.state["speed"] / 3600

    def replay_clock(self):
        t = COLLAPSE_LOCAL + timedelta(hours=self.hours_since_collapse())
        return t.strftime("%Y-%m-%d %H:%M") + " local"

    # ---------- ranking ----------
    def score(self, sid, hours):
        s = self.sites[sid]
        grade_w = GRADE_WEIGHT[s["grade"]]
        residential = s.get("building_use", "").lower().startswith("residential")
        use_f = 1.0 if residential else 0.3
        fp = s.get("footprint_m2")
        size_f = 0.5 if fp is None else min(1.0, max(0.3, math.sqrt(fp / 20000)))
        time_f = 0.5 ** (hours / 48)
        score = round(grade_w * use_f * size_f * time_f, 3)
        reasons = [f"Satellite grade {s['grade_label']} (Copernicus, not an inspection)"]
        if "building_use" in s:
            reasons.append("Residential block, collapse at 04:17: people likely home" if residential
                           else "Non-residential, collapse at night: fewer people likely inside")
        else:
            reasons.append("Use unknown, assumed residential")
        reasons.append(f"Block footprint {fp:,} m2" if fp is not None else "Footprint not mapped (point record)")
        return score, reasons

    def ranked(self):
        hours = self.hours_since_collapse()
        rows = []
        for sid, ss in self.state["sites"].items():
            score, reasons = self.score(sid, hours)
            tier = 0 if ss["escalated"] else STATUS_ORDER[ss["status"]]
            if ss["detections"]:
                reasons = [f"Signs of life: {len(ss['detections'])} sensor detections"] + reasons[:2]
            rows.append((tier, -score, sid, score, reasons))
        rows.sort()
        return [{"id": sid, "priority": i + 1, "score": score, "reasons": reasons}
                for i, (_, _, sid, score, reasons) in enumerate(rows)]

    # ---------- dispatch ----------
    def _propose(self):
        """Greedy: highest-priority unsearched site gets the nearest free team. Needs ack."""
        st = self.state
        busy = {t for t in st["proposals"].values()} | {t for t, v in st["teams"].items() if v["site"]}
        free = [t for t in TEAMS if t not in busy]
        for row in self.ranked():
            if not free:
                break
            sid = row["id"]
            if st["sites"][sid]["status"] != "unsearched" or sid in st["proposals"]:
                continue
            site = self.sites[sid]
            team = min(free, key=lambda t: dist_m((st["teams"][t]["lat"], st["teams"][t]["lon"]),
                                                   (site["lat"], site["lon"])))
            free.remove(team)
            st["proposals"][sid] = team
            self._alert("dispatch_proposal", sid,
                        f"Proposed: Team {team} to {sid} (priority {row['priority']}, "
                        f"{site['grade_label']}). Reply \"ack {sid}\" to dispatch.")

    def ack(self, target):
        """Commander approval for one site or 'all'. Returns what changed."""
        with self.lock:
            st = self.state
            targets = list(dict.fromkeys(list(st["proposals"]) + st["survivor_ack"])) \
                if target.lower() == "all" else [norm_site(target)]
            done = []
            for sid in targets:
                if sid in st["proposals"]:
                    team = st["proposals"].pop(sid)
                    site = self.sites[sid]
                    st["teams"][team].update(site=sid, lat=site["lat"], lon=site["lon"])
                    st["sites"][sid].update(status="dispatched", team=team)
                    done.append(f"Team {team} dispatched to {sid}")
                    self._alert("dispatched", sid, f"Commander ack received: Team {team} dispatched to {sid}.", team=team)
                if sid in st["survivor_ack"]:
                    st["survivor_ack"].remove(sid)
                    done.append(f"Survivor alert at {sid} acknowledged")
                    self._alert("ack_survivor", sid, f"Commander ack received for survivor alert at {sid}.")
            self._save()
            return [friendly(d) for d in done] or [f"Nothing waiting for your ack at {target}"]

    def request_scan(self, sid):
        """Commander asks for a site: propose the nearest team that is free or on a no-signal site."""
        with self.lock:
            sid = norm_site(sid)
            st = self.state
            if sid not in st["sites"]:
                return f"Unknown site {sid}"
            if st["sites"][sid]["team"] or sid in st["proposals"]:
                return f"{sid} already has a team or a pending dispatch"
            site = self.sites[sid]
            candidates = [t for t, v in st["teams"].items()
                          if t not in st["proposals"].values()
                          and (v["site"] is None or st["sites"][v["site"]]["status"] in ("dispatched", "scanning"))]
            if not candidates:
                return "No team available: all teams are on survivor sites"
            team = min(candidates, key=lambda t: dist_m((st["teams"][t]["lat"], st["teams"][t]["lon"]),
                                                         (site["lat"], site["lon"])))
            old = st["teams"][team]["site"]
            if old:   # pull the team off a site with no detection yet; it goes back in the queue
                st["sites"][old].update(status="unsearched", team=None, scans=0)
                st["teams"][team]["site"] = None
            st["proposals"][sid] = team
            self._alert("dispatch_proposal", sid,
                        f"Commander requested {sid}. Proposed: Team {team}"
                        f"{f' (moving off {old})' if old else ''}. Reply \"ack {sid}\" to dispatch.")
            self._save()
            return f"Proposed Team {team} to {sid}, awaiting ack"

    # ---------- sensors ----------
    def tick(self):
        """Process a new sensor tick if there is one. Called every 2 s by the toolbox."""
        with self.lock:
            if not SENSORS_FILE.exists():
                return
            try:
                feed = json.loads(SENSORS_FILE.read_text())
            except json.JSONDecodeError:
                return
            st = self.state
            if feed["tick"] == st["last_tick"]:
                return
            st["last_tick"] = feed["tick"]
            now = time.time()
            by_site = {}
            for r in feed["readings"]:
                by_site.setdefault(r["site_id"], []).append(r)

            for sid, ss in st["sites"].items():
                if not ss["team"]:
                    continue        # sensors only count where a team is on site
                ss["scans"] += 1
                hits = [r for r in by_site.get(sid, []) if r["presence"]]
                if hits:
                    self._detect(sid, ss, hits, now)
                elif ss["status"] in ("dispatched", "scanning"):
                    if ss["scans"] >= 2 and not self.meta.get("live"):   # live drill: keep watching the room
                        team = ss["team"]
                        ss.update(status="no_signal_2_scans", team=None)
                        st["teams"][team]["site"] = None
                        self._alert("no_signal", sid,
                                    f"{sid}: no signal after 2 scans. This does not mean nobody is inside. "
                                    f"Team {team} free for the next site.", team=team)
                    else:
                        ss["status"] = "scanning"
            self._propose()
            self._save()

    def _detect(self, sid, ss, hits, now):
        st = self.state
        for r in hits:
            ss["detections"].append({"sensor_id": r["sensor_id"], "tick": r["tick"], "real_time": now,
                                     "bpm": r["breathing_bpm"], "confidence": r["confidence"]})
        rates = [r["breathing_bpm"] for r in hits if r["breathing_bpm"] is not None]
        bpm = min(rates) if rates else None      # RuView may abstain on breathing
        if bpm is not None:
            ss["bpm_hist"].append([now, bpm])
        ss["moving"] = any(r["moving"] for r in hits)
        ss["source"] = "+".join(sorted({r["source"] for r in hits} | set(filter(None, [ss["source"]]))))

        sensors = {d["sensor_id"] for d in ss["detections"]}
        times = [d["real_time"] for d in ss["detections"]]
        confirmed = len(sensors) >= 2 or (max(times) - min(times) >= CONFIRM_GAP_S)
        new_status = "confirmed_survivor" if confirmed else "possible_survivor"
        if new_status != ss["status"]:
            ss["status"] = new_status
            label = "CONFIRMED survivor" if confirmed else "Possible survivor"
            self._alert(new_status, sid,
                        f"{label} at {sid}: breathing {f'{bpm} bpm' if bpm is not None else 'not measured'}, "
                        f"{'moving' if ss['moving'] else 'not moving'}, "
                        f"{len(ss['detections'])} detections (sensor: {ss['source']}). "
                        f"Reply \"ack {sid}\".", bpm=bpm, moving=ss["moving"],
                        detections=len(ss["detections"]), source=ss["source"])
            if sid not in st["survivor_ack"]:
                st["survivor_ack"].append(sid)

        if not ss["escalated"] and bpm is not None:
            recent = [b for t, b in ss["bpm_hist"] if now - t <= ESCALATE_WINDOW_S]
            peak = max(recent)
            if bpm < ESCALATE_BPM or (peak - bpm) / peak >= ESCALATE_DROP:
                ss["escalated"] = True
                self._alert("escalation", sid,
                            f"ESCALATED to priority 1: {sid} breathing fell to {bpm} bpm "
                            f"(from {peak} in the last 2 min). Reply \"ack {sid}\" to send medical support.", bpm=bpm, peak=peak)
                if sid not in st["survivor_ack"]:
                    st["survivor_ack"].append(sid)

    # ---------- outputs ----------
    def mark_delivered(self, ids):
        with self.lock:
            for a in self.state["alerts"]:
                if a["id"] in ids:
                    a["delivered"] = True
            self._save()

    def people(self):
        out = []
        for sid, ss in self.state["sites"].items():
            if not ss["detections"]:
                continue
            hist = ss["bpm_hist"]
            if hist:
                last = hist[-1][1]
                past = [b for t, b in hist if hist[-1][0] - t <= 60]
                trend = "falling" if past[0] - last >= 1 else "rising" if last - past[0] >= 1 else "steady"
                t_last = hist[-1][0]
            else:
                last, trend, t_last = None, "not measured", ss["detections"][-1]["real_time"]
            out.append({"site_id": sid, "breathing_bpm": last, "trend": trend, "moving": ss["moving"],
                        "confidence": max(d["confidence"] or 0 for d in ss["detections"][-2:]),
                        "detections": len(ss["detections"]), "status": ss["status"],
                        "source": ss["source"],
                        "time": datetime.fromtimestamp(t_last, timezone.utc).isoformat(timespec="seconds")})
        return out

    def counters(self):
        st = self.state
        statuses = [ss["status"] for ss in st["sites"].values()]
        return {"sites_ranked": len(statuses),
                "teams_deployed": sum(1 for v in st["teams"].values() if v["site"]),
                "survivors_confirmed": statuses.count("confirmed_survivor"),
                "possible_survivors": statuses.count("possible_survivor"),
                "no_signal_2_scans": statuses.count("no_signal_2_scans"),
                "hours_since_collapse": round(self.hours_since_collapse(), 1),
                "baseline_first_official_map_hours": 40}

    def brief(self):
        with self.lock:
            t0 = time.perf_counter()
            st = self.state
            sites = []
            for row in self.ranked():
                sid = row["id"]
                ss = st["sites"][sid]
                sites.append({**row, "name": self.sites[sid]["name"],
                              "grade_label": self.sites[sid]["grade_label"], "status": ss["status"],
                              "escalated": ss["escalated"],
                              "team": ss["team"] or st["proposals"].get(sid),
                              "team_state": "on_site" if ss["team"] else
                                            "awaiting_ack" if sid in st["proposals"] else None})
            out = {
                "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "replay_clock": self.replay_clock(),
                "hours_since_collapse": round(self.hours_since_collapse(), 1),
                "event": self.event,
                "region": self.region_name,
                "counters": self.counters(),
                "sites_ranked": sites,
                "people": self.people(),
                "new_alerts": [{k: a.get(k, {}) for k in ("id", "type", "site_id", "text", "data")}
                               for a in st["alerts"] if not a["delivered"]],
                "awaiting_ack": list(dict.fromkeys(list(st["proposals"]) + st["survivor_ack"])),
                "compute_device": "Dell Pro Max GB10 (local)",
                "honesty": ["Damage grading: Copernicus EMS replay (EMSR648 AOI04), satellite not inspection",
                            f"Sensors: simulated today (source: {self._sensor_source()})",
                            "All inference on this GB10"],
            }
            out["status_text"] = friendly(self._render_status(out))
            out["people_text"] = friendly(self._render_people(out))
            out["heartbeat_text"] = friendly(self._render_heartbeat(out)) if out["new_alerts"] else "HEARTBEAT_OK"
            out["help_text"] = HELP_TEXT
            out["compute_seconds"] = round(time.perf_counter() - t0, 4)
            return out

    def kind(self, sid):
        s = self.sites[sid]
        if s.get("kind"):
            return s["kind"]
        if s.get("building_use", "").lower().startswith("residential"):
            return "apartment block"
        detail = (s.get("use_detail") or "").lower()
        for word in ("school", "hospital", "industrial", "museum", "retail", "military"):
            if word in detail:
                return f"{word} building"
        return "building"

    # Pre-rendered Telegram text, so every number the agent posts comes straight from the engine.
    # Written for a commander reading on a phone: short lines, one emoji per kind of event.
    FOOTER = "Simulated sensors · Copernicus satellite damage data · runs only on this machine"

    @property
    def footer(self):
        if self.meta.get("live"):
            return "Live drill, no disaster · real WiFi sensor in this room (RuView) · runs only on this machine"
        return self.FOOTER

    def _clock(self, b):
        if self.meta.get("live"):
            return "live drill · " + time.strftime("%H:%M")
        return f"{b['replay_clock'][5:16].replace('02-06', '6 Feb')} · {b['hours_since_collapse']}h after quake"

    @staticmethod
    def _src(source):
        names = {"replay": "replay", "ruview-sim": "RuView sim", "ruview-rssi-live": "live WiFi in this room"}
        return " + ".join(names.get(x, x) for x in (source or "").split("+") if x)

    def _decisions(self, b):
        st = self.state
        L = []
        for sid in b["awaiting_ack"]:
            if sid in st["proposals"]:
                L.append(f"ack {sid} → send Team {st['proposals'][sid]} to {sid}")
            elif st["sites"][sid]["escalated"]:
                L.append(f"ack {sid} → send medical support to {sid}")
            else:
                L.append(f"ack {sid} → confirm survivor at {sid}, start rescue")
        if not L:
            return []
        if len(L) > 1:
            L.append("ack all → approve everything above")
        return ["", "👉 YOUR DECISION (reply with one line)"] + L

    def _site_line(self, s):
        st = self.state["sites"][s["id"]]
        team = s["team"]
        if st["escalated"]:
            what = f"🚨 survivor weakening, Team {team} there"
        elif s["status"] == "confirmed_survivor":
            what = f"🟢 survivor confirmed, Team {team} there"
        elif s["status"] == "possible_survivor":
            what = f"🟡 possible survivor, Team {team} checking"
        elif s["status"] in ("dispatched", "scanning"):
            what = f"Team {team} searching"
        elif s["status"] == "no_signal_2_scans":
            what = "no signal after 2 scans (people may still be inside)"
        elif s["team_state"] == "awaiting_ack":
            what = f"Team {team} ready, needs your ack"
        else:
            what = "no team yet"
        if self.meta.get("live"):
            return f"{s['priority']}. {s['id']} · {self.kind(s['id'])} · {what}"
        return f"{s['priority']}. {s['id']} · {s['grade_label'].lower()} {self.kind(s['id'])} · {what}"

    def _render_status(self, b):
        c = b["counters"]
        L = [f"LIFELINE · {self.region_name} · {self._clock(b)}",
             f"Teams out {c['teams_deployed']}/3 · Survivors {c['survivors_confirmed']} · Sites {c['sites_ranked']}",
             "", "WHERE TO DIG"]
        L += [self._site_line(s) for s in b["sites_ranked"][:5]]
        L += self._decisions(b)
        L += ["", "Send help for all commands.", self.footer]
        return "\n".join(L)

    def _render_people(self, b):
        if not b["people"]:
            return "No signs of life detected yet.\nNo signal does not mean nobody is there.\n\n" + self.footer
        L = ["SIGNS OF LIFE"]
        for p in b["people"]:
            mark = "🚨" if self.state["sites"][p["site_id"]]["escalated"] else \
                   "🟢" if p["status"] == "confirmed_survivor" else "🟡"
            br = f"breathing {p['breathing_bpm']}/min ({p['trend']})" if p["breathing_bpm"] is not None \
                else "breathing not measured"
            conf = "confirmed" if p["status"] == "confirmed_survivor" else "not yet confirmed"
            L.append(f"{mark} {p['site_id']}: {br}, {'moving' if p['moving'] else 'not moving'}. "
                     f"{conf.capitalize()} ({self._src(p['source'])}).")
        L += ["", self.footer]
        return "\n".join(L)

    def _render_heartbeat(self, b):
        L = [f"LIFELINE ALERT · {self.region_name} · {self._clock(b)}"]
        by = {}
        for a in b["new_alerts"]:
            by.setdefault(a["type"], []).append(a)
        for a in by.get("region", []):
            L += ["", f"🗺️ Now working on {self.region_name}. Replay restarted at 04:17."]
        for a in by.get("escalation", []):
            d = a["data"]
            L += ["", f"🚨 URGENT {a['site_id']}: breathing dropped to {d['bpm']}/min (was {d['peak']}).",
                  "Moved to priority 1."]
        for a in by.get("confirmed_survivor", []):
            d = a["data"]
            br = f"breathing {d['bpm']}/min" if d.get("bpm") is not None else "breathing not measured"
            L += ["", f"🟢 SURVIVOR CONFIRMED at {a['site_id']}",
                  f"{br.capitalize()}, {'moving' if d.get('moving') else 'not moving'}. "
                  f"2 sensors agree ({self._src(d.get('source'))})."]
        for a in by.get("possible_survivor", []):
            d = a["data"]
            br = f"breathing {d['bpm']}/min" if d.get("bpm") is not None else "breathing not measured yet"
            L += ["", f"🟡 Possible survivor at {a['site_id']}: {br}. "
                      f"1 sensor ({self._src(d.get('source'))}), checking again."]
        if by.get("dispatched"):
            L += ["", "🚑 " + ", ".join(f"Team {a['data']['team']} now at {a['site_id']}" for a in by["dispatched"])]
        if by.get("ack_survivor"):
            L += ["", "✅ Rescue confirmed at " + ", ".join(a["site_id"] for a in by["ack_survivor"])]
        if by.get("no_signal"):
            ids = ", ".join(a["site_id"] for a in by["no_signal"])
            L += ["", f"⚪ No signal yet at {ids}. People may still be inside.",
                  "Teams move to the next building."]
        L += self._decisions(b)
        L += ["", self.footer]
        return "\n".join(L)

    def _sensor_source(self):
        try:
            return json.loads(SENSORS_FILE.read_text())["source"]
        except Exception:
            return "no sensor feed"

    def alert_log(self, limit=40):
        """Everything the engine raised, newest first, with whether the agent has posted it."""
        with self.lock:
            return [{k: a.get(k) for k in ("id", "type", "site_id", "text", "delivered", "real_time")}
                    for a in reversed(self.state["alerts"][-limit:])]

    def map_view(self):
        with self.lock:
            st = self.state
            prio = {r["id"]: r["priority"] for r in self.ranked()}
            return {
                "region": self.region_name, "region_slug": self.region,
                "replay_clock": self.replay_clock(),
                "counters": self.counters(),
                "live": bool(self.meta.get("live")),
                "sites": [{"id": sid, "lat": s["lat"], "lon": s["lon"], "status": st["sites"][sid]["status"],
                           "outline": s.get("outline"),
                           "priority": prio[sid], "grade_label": s["grade_label"],
                           "escalated": st["sites"][sid]["escalated"],
                           "proposed_team": st["proposals"].get(sid)} for sid, s in self.sites.items()],
                "teams": [{"id": t, "lat": v["lat"], "lon": v["lon"], "site": v["site"]}
                          for t, v in st["teams"].items()],
                "people": [{"site_id": p["site_id"], "breathing_bpm": p["breathing_bpm"],
                            "status": p["status"]} for p in self.people()],
                "awaiting_ack": list(dict.fromkeys(list(st["proposals"]) + st["survivor_ack"])),
                "sensor_source": self._sensor_source(),
            }
