"""One loop cycle: host toolbox <-> OpenClaw sandbox. Run by run_loop.sh every 30 s.

1. Download the commander's acks/scans the agent recorded (commands.txt) and apply them.
2. GET /brief and write brief.json, status.txt, people.txt, alerts.txt.
3. Upload those files into the sandbox skill folder.
4. If there are new alerts, wake the agent ("LIFELINE HEARTBEAT") so it posts them to
   Telegram on its own, then mark the alerts delivered.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
OUT = ROOT / "demo" / "out"
OFFSET = ROOT / "data" / "commands_offset"
SANDBOX = os.environ.get("LIFELINE_SANDBOX", "my-assistant")
SKILL_DIR = "/sandbox/.openclaw/workspace/skills/lifeline/"
# The 9B model sometimes prefixes relative paths with "sandbox/.openclaw/workspace/". We keep real
# copies there too (a symlink would block NemoClaw snapshots), and read acks from both places.
ALT_DIR = "/sandbox/.openclaw/workspace/sandbox/.openclaw/workspace/skills/lifeline/"
TOOLBOX = "http://127.0.0.1:8090"
CHAT_ID = os.environ.get("LIFELINE_CHAT_ID")      # Telegram group id; unset = print only


def nemoclaw(*args, timeout=60):
    r = subprocess.run(["nemoclaw", SANDBOX, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout


def apply_commands(c):
    lines = []
    for d in (SKILL_DIR, ALT_DIR):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = nemoclaw("download", d + "commands.txt", tmp)
            f = Path(tmp) / "commands.txt"
            if code == 0 and f.exists():
                lines += [l.strip() for l in f.read_text().splitlines() if l.strip()]
    done = int(OFFSET.read_text()) if OFFSET.exists() else 0
    for line in lines[done:]:
        # Accept "ack B-07", "scan B-07", "ack all"; a bare "B-07"/"all" from the agent means ack.
        m = re.fullmatch(r"(?i)(ack|scan)?\s*(all|B-\d{2})", line.strip(" '\""))
        if not m or (m.group(1) or "ack").lower() == "scan" and m.group(2).lower() == "all":
            print(f"ignored command line: {line!r}")
            continue
        verb = (m.group(1) or "ack").lower()
        r = c.post(f"{TOOLBOX}/{verb}", params={"site": m.group(2)})
        print(f"command '{verb} {m.group(2)}': {r.json()['result']}")
    OFFSET.write_text(str(len(lines)))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=10) as c:
        apply_commands(c)
        b = c.get(f"{TOOLBOX}/brief").json()
        (OUT / "brief.json").write_text(json.dumps(b, indent=1, ensure_ascii=False))
        (OUT / "status.txt").write_text(b["status_text"] + "\n")
        (OUT / "people.txt").write_text(b["people_text"] + "\n")
        (OUT / "alerts.txt").write_text(b["heartbeat_text"] + "\n")
        for name in ("brief.json", "status.txt", "people.txt", "alerts.txt"):
            for d in (SKILL_DIR, ALT_DIR):
                nemoclaw("upload", str(OUT / name), d)
        c_ = b["counters"]
        print(f"{b['replay_clock']} | deployed {c_['teams_deployed']} | confirmed {c_['survivors_confirmed']}"
              f" | awaiting {b['awaiting_ack']} | new alerts {len(b['new_alerts'])}")

        if not b["new_alerts"]:
            return
        args = ["agent", "-m", "LIFELINE HEARTBEAT. Read skills/lifeline/alerts.txt and reply with its full text.", "--session-id", f"lifeline-hb-{int(time.time())}",
                "--thinking", "off", "--timeout", "120"]
        if CHAT_ID:
            args += ["--deliver", "--channel", "telegram", "--reply-to", CHAT_ID]
        code, out = nemoclaw(*args, timeout=180)
        reply = "\n".join(l for l in out.splitlines() if not l.startswith(("(node:", "(Use", "[gateway]", "  ")))
        print("agent:", reply.strip()[:400] or "(no agent output)")
        if code == 0 and "LIFELINE ALERT" in reply:
            c.post(f"{TOOLBOX}/delivered", params={"ids": ",".join(str(a["id"]) for a in b["new_alerts"])})
        else:
            print("agent did not post the alerts; they stay pending for the next cycle", file=sys.stderr)


if __name__ == "__main__":
    main()
