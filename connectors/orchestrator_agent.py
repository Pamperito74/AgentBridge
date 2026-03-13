#!/usr/bin/env python3
"""
Orchestrator Agent for AgentBridge
====================================
Registers as an orchestrator, watches for system notifications about unassigned
tasks, then uses Claude (or a custom dispatch function) to match each task to the
best-fit agent based on capabilities and role.

Usage
-----
  python connectors/orchestrator_agent.py

Required
  ANTHROPIC_API_KEY=sk-ant-...

Optional
  AGENTBRIDGE_URL=http://localhost:7890
  AGENTBRIDGE_TOKEN=...
  ORCHESTRATOR_NAME=orchestrator          (default)
  ORCHESTRATOR_MODEL=claude-opus-4-6      (default; falls back to ANTHROPIC_API_KEY)
  POLL_INTERVAL=3                         (seconds)
  DEBUG=0
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import requests
import anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BRIDGE_URL = os.environ.get("AGENTBRIDGE_URL", "http://localhost:7890")
AGENTBRIDGE_TOKEN = os.environ.get("AGENTBRIDGE_TOKEN", "")
AGENT_NAME = os.environ.get("ORCHESTRATOR_NAME", "orchestrator")
MODEL = os.environ.get("ORCHESTRATOR_MODEL", os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
HEARTBEAT_INTERVAL = 30
DEBUG = os.environ.get("DEBUG", "0") == "1"

ORCHESTRATOR_SYSTEM = """\
You are an orchestrator agent on AgentBridge — a multi-agent platform.

Your job: when a task arrives, analyse it and decide which registered agent is
best suited to handle it based on their capabilities and role description.

Respond with a JSON object (no markdown, no commentary) in this exact format:
{
  "assignee": "<agent_name>",
  "reason": "<one sentence>",
  "message_to_agent": "<brief instruction or context for the assignee>"
}

If no suitable agent exists, respond with {"assignee": null, "reason": "...", "message_to_agent": ""}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_client = anthropic.Anthropic()


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if AGENTBRIDGE_TOKEN:
        h["X-AgentBridge-Token"] = AGENTBRIDGE_TOKEN
    return h


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [orchestrator] {msg}", flush=True)


def _debug(msg: str) -> None:
    if DEBUG:
        _log(f"[DEBUG] {msg}")


# ---------------------------------------------------------------------------
# AgentBridge API
# ---------------------------------------------------------------------------


def register() -> None:
    resp = requests.post(
        f"{BRIDGE_URL}/agents",
        json={
            "name": AGENT_NAME,
            "role": "Orchestrator — assigns tasks to the right agents",
            "capabilities": ["orchestration", "task-routing", "delegation"],
            "agent_type": "bot",
            "is_orchestrator": True,
        },
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    _log(f"Registered as '{AGENT_NAME}' on {BRIDGE_URL}")


def heartbeat() -> None:
    try:
        requests.post(
            f"{BRIDGE_URL}/agents/{AGENT_NAME}/heartbeat",
            json={"status": "online", "working_on": ""},
            headers=_headers(), timeout=5,
        )
    except Exception as exc:
        _debug(f"Heartbeat error: {exc}")


def fetch_messages(since_id: str | None = None) -> list[dict]:
    params: dict = {"limit": 50, "recipient": AGENT_NAME}
    if since_id:
        params["since_id"] = since_id
    r = requests.get(f"{BRIDGE_URL}/messages", params=params, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_agents() -> list[dict]:
    r = requests.get(f"{BRIDGE_URL}/agents", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_task(task_id: str) -> dict | None:
    r = requests.get(f"{BRIDGE_URL}/tasks/{task_id}", headers=_headers(), timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def assign_task(task_id: str, assignee: str) -> None:
    requests.post(
        f"{BRIDGE_URL}/tasks/{task_id}/assign",
        json={"assignee": assignee},
        headers=_headers(), timeout=10,
    ).raise_for_status()


def send_message(content: str, recipient: str | None = None, thread: str = "general") -> None:
    payload: dict = {"sender": AGENT_NAME, "content": content, "thread": thread}
    if recipient:
        payload["recipient"] = recipient
    requests.post(f"{BRIDGE_URL}/messages", json=payload, headers=_headers(), timeout=10).raise_for_status()


def report_cost(input_tokens: int, output_tokens: int) -> None:
    cost_cents = round((input_tokens * 15 + output_tokens * 75) / 1_000_000 * 100)
    try:
        requests.post(
            f"{BRIDGE_URL}/cost-events",
            json={"agent_name": AGENT_NAME, "model": MODEL, "provider": "anthropic",
                  "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_cents": cost_cents},
            headers=_headers(), timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Orchestration logic
# ---------------------------------------------------------------------------


def _agents_context(agents: list[dict]) -> str:
    lines = []
    for a in agents:
        if a["name"] == AGENT_NAME:
            continue
        caps = ", ".join(a.get("capabilities") or []) or "none listed"
        lines.append(f"- {a['name']} (role: {a.get('role','?')}, capabilities: {caps}, status: {a.get('status','?')})")
    return "\n".join(lines) if lines else "(no other agents registered)"


def dispatch_task(task: dict, agents: list[dict]) -> dict:
    """Ask Claude which agent should handle this task. Returns parsed JSON response."""
    agents_str = _agents_context(agents)
    user_msg = (
        f"Task to assign:\n"
        f"  Title: {task['title']}\n"
        f"  Description: {task.get('description') or '(none)'}\n"
        f"  Priority: {task.get('priority','medium')}\n"
        f"  Labels: {', '.join(task.get('labels') or []) or 'none'}\n\n"
        f"Available agents:\n{agents_str}"
    )

    _debug(f"Dispatching task '{task['title']}' with {len(agents)} agents available")

    response = _client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=ORCHESTRATOR_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    report_cost(response.usage.input_tokens, response.usage.output_tokens)

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _log(f"Claude returned non-JSON: {raw[:200]}")
        return {"assignee": None, "reason": "parse error", "message_to_agent": ""}


def handle_orchestrator_event(event_payload: dict) -> None:
    """Process a single ORCHESTRATOR_EVENT notification."""
    event = event_payload.get("event", "")
    data = event_payload.get("data", {})

    if event == "task_created":
        task_id = data.get("id")
        if not task_id:
            return

        # Only handle unassigned tasks
        if data.get("assignee"):
            _debug(f"Task {task_id} already assigned to {data['assignee']}, skipping")
            return

        task = get_task(task_id)
        if not task:
            _log(f"Task {task_id} not found")
            return
        if task.get("assignee"):
            _debug(f"Task {task_id} already has assignee '{task['assignee']}', skipping")
            return

        _log(f"Routing task: '{task['title']}' (id={task_id[:8]})")

        try:
            agents = get_agents()
        except Exception as exc:
            _log(f"Failed to fetch agents: {exc}")
            return

        result = dispatch_task(task, agents)
        assignee = result.get("assignee")
        reason = result.get("reason", "")
        msg_to_agent = result.get("message_to_agent", "")

        if assignee:
            try:
                assign_task(task_id, assignee)
                _log(f"Assigned '{task['title']}' → {assignee} ({reason})")

                # DM the assigned agent with context
                if msg_to_agent:
                    send_message(
                        content=f"You've been assigned task **{task['title']}** (id: {task_id}).\n\n{msg_to_agent}",
                        recipient=assignee,
                        thread=task.get("thread", "general"),
                    )

                # Broadcast to the task's thread
                send_message(
                    content=f"Task **{task['title']}** assigned to @{assignee}. ({reason})",
                    thread=task.get("thread", "general"),
                )
            except Exception as exc:
                _log(f"Assignment failed: {exc}")
        else:
            _log(f"No suitable agent found for '{task['title']}': {reason}")
            send_message(
                content=f"Task **{task['title']}** has no suitable assignee: {reason}",
                thread=task.get("thread", "general"),
            )

    elif event == "agent_joined":
        agent_name = data.get("name", "?")
        _log(f"New agent joined: {agent_name} — registry updated")
        # No action needed; next dispatch call will see the new agent

    else:
        _debug(f"Unhandled orchestrator event: {event}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _heartbeat_loop() -> None:
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        heartbeat()


def main() -> None:
    _log(f"Starting orchestrator — model: {MODEL}")

    try:
        register()
    except Exception as exc:
        _log(f"Registration failed: {exc}")
        sys.exit(1)

    # Seed cursor — don't process old messages on startup
    try:
        seed = fetch_messages()
        last_id: str | None = seed[-1]["id"] if seed else None
    except Exception:
        last_id = None

    _log(f"Listening for orchestrator events (last_id={last_id})…")

    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    while True:
        try:
            msgs = fetch_messages(since_id=last_id)
            for msg in msgs:
                msg_id = msg.get("id")
                content: str = msg.get("content", "")

                if content.startswith("ORCHESTRATOR_EVENT:"):
                    try:
                        payload = json.loads(content[len("ORCHESTRATOR_EVENT:"):])
                        handle_orchestrator_event(payload)
                    except Exception as exc:
                        _log(f"Error handling orchestrator event: {exc}")

                if msg_id:
                    last_id = msg_id

        except requests.RequestException as exc:
            _log(f"Network error: {exc}")
        except Exception as exc:
            _log(f"Poll loop error: {exc}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
