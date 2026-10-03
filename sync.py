"""One loop cycle: host toolbox <-> OpenClaw sandbox. Run by run_loop.sh every 30 s.

1. Read the commander's commands (ack / scan / go) from the Telegram chat transcript and apply them.
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
SANDBOX = os.environ.get("LIFELINE_SANDBOX", "my-assistant")
SKILL_DIR = "/sandbox/.openclaw/workspace/skills/lifeline/"
# The 9B model sometimes prefixes relative paths with "sandbox/.openclaw/workspace/". We keep real
# copies there too (a symlink would block NemoClaw snapshots).
ALT_DIR = "/sandbox/.openclaw/workspace/sandbox/.openclaw/workspace/skills/lifeline/"
TOOLBOX = "http://127.0.0.1:8090"
CHAT_ID = os.environ.get("LIFELINE_CHAT_ID")      # Telegram group id; unset = print only


def nemoclaw(*args, timeout=60):
    r = subprocess.run(["nemoclaw", SANDBOX, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout


SESSIONS = "/sandbox/.openclaw/agents/main/sessions"
SEEN = ROOT / "data" / "commands_seen.json"
CMD = re.compile(r"(?i)^\s*(?:@\w+\s+)?(ack|scan|go)\s+([A-Za-zçğıöşüÇĞİÖŞÜ]+|B-\d{2})\s*$")


def chat_commands():
    """Commander messages from the Telegram chat transcripts in the sandbox (not our own test or
    heartbeat sessions). Reading the chat directly makes every ack take effect even when the
    9B model fumbles a tool call."""
    code, out = nemoclaw("exec", "--", "sh", "-c",
                         f"cd {SESSIONS} && ls *.jsonl 2>/dev/null | grep -v -e '^lifeline-' -e trajectory "
                         f"| xargs -r grep -h '\"role\":\"user\"' || true", timeout=60)
    msgs = []
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = m["message"]["content"]
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        msgs.append((m["id"], content.strip().splitlines()[-1] if content.strip() else ""))
    return msgs


def apply_commands(c):
    msgs = chat_commands()
    if not SEEN.exists():          # first run after a reset: never replay old chat history
        SEEN.write_text(json.dumps([i for i, _ in msgs]))
        return
    seen = set(json.loads(SEEN.read_text()))
    for mid, text in msgs:
        if mid in seen:
            continue
        seen.add(mid)
        m = CMD.match(text)
        if not m:
            continue
        verb, arg = m.group(1).lower(), m.group(2)
        if verb == "go":
            r = c.post(f"{TOOLBOX}/region", params={"name": arg})
        elif verb == "scan" and arg.lower() != "all":
            r = c.post(f"{TOOLBOX}/scan", params={"site": arg})
        elif verb == "ack":
            r = c.post(f"{TOOLBOX}/ack", params={"site": arg})
        else:
            continue
        print(f"chat command '{text}': {r.json()['result']}")
    SEEN.write_text(json.dumps(sorted(seen)))


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
