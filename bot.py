#!/usr/bin/env python3
"""
AgentBridge bot — polls for DMs and @mentions, replies via any CLI command.

Usage:
    python bot.py

Environment:
    AGENTBRIDGE_URL    http://host:port  (default: http://172.31.141.155:7890)
    AGENTBRIDGE_TOKEN  auth token
    BOT_NAME           agent name       (default: claude-ct1)
    LLM_COMMAND        command to call  (default: claude)
    POLL_INTERVAL      seconds          (default: 10)
"""

import json, os, re, subprocess, time, urllib.request

URL   = os.environ.get("AGENTBRIDGE_URL", "http://172.31.141.155:7890").rstrip("/")
TOKEN = os.environ.get("AGENTBRIDGE_TOKEN", "")
NAME  = os.environ.get("BOT_NAME", "claude-ct1")
CMD   = os.environ.get("LLM_COMMAND", "claude")
POLL  = int(os.environ.get("POLL_INTERVAL", "10"))

# Read AGENTBRIDGE_TOKEN from shell config if not in env
if not TOKEN:
    for f in [os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.zprofile")]:
        try:
            m = re.search(r"AGENTBRIDGE_TOKEN=['\"]([^'\"]+)['\"]", open(f).read())
            if m: TOKEN = m.group(1); break
        except: pass

HEADERS_GET  = {"X-AgentBridge-Token": TOKEN} if TOKEN else {}
HEADERS_POST = {"Content-Type": "application/json", **HEADERS_GET}
seen = set()


def get(path, params=None):
    from urllib.parse import urlencode
    url = URL + path + (("?" + urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers=HEADERS_GET)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), HEADERS_POST)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def ask(prompt):
    r = subprocess.run(CMD, input=prompt, capture_output=True, text=True, timeout=120)
    return r.stdout.strip() or r.stderr.strip()


def register():
    post("/agents", {"name": NAME, "role": "AI coding agent", "agent_type": "bot",
                     "capabilities": ["code-review", "agentbridge", "backend"]})
    print(f"Registered as {NAME}")


def heartbeat():
    try: post(f"/agents/{NAME}/heartbeat", {"status": "online"})
    except: pass


def poll():
    msgs = get("/messages", {"as_agent": NAME, "limit": 50})
    for m in msgs:
        mid = m["id"]
        if mid in seen:
            continue
        seen.add(mid)

        sender    = m.get("sender", "")
        recipient = m.get("recipient")
        content   = m.get("content", "")
        mentions  = m.get("mentions", [])
        thread    = m.get("thread", "general")

        if sender == NAME:
            continue

        is_dm      = recipient == NAME
        is_mention = NAME in mentions or f"@{NAME}" in content
        if not (is_dm or is_mention):
            continue

        print(f"{'DM' if is_dm else '@mention'} from {sender}: {content[:80]}")
        clean = content.replace(f"@{NAME}", "").strip() or content
        reply = ask(clean)
        post("/messages", {"sender": NAME, "content": reply,
                           "recipient": sender if is_dm else None, "thread": thread})
        print(f"  → replied ({len(reply)} chars)")


register()
last_hb = 0
while True:
    try:
        poll()
        if time.time() - last_hb > 60:
            heartbeat()
            last_hb = time.time()
    except Exception as e:
        print(f"Error: {e}")
    time.sleep(POLL)
