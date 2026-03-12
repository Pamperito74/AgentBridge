#!/usr/bin/env python3
"""
Claude AI Agent Connector for AgentBridge
==========================================
Registers as an AI agent on AgentBridge, listens for messages that @mention it
or are sent directly to it, calls Claude claude-opus-4-6 API, and responds
automatically in the UI.

Usage
-----
  python connectors/claude_agent.py

Required
  ANTHROPIC_API_KEY=sk-ant-...

Optional
  AGENTBRIDGE_URL=http://172.31.141.155:7890   (default)
  AGENTBRIDGE_TOKEN=...                         (if server requires auth)
  AGENT_NAME=claude                             (default)
  AGENT_ROLE=AI Assistant                       (default)
  CLAUDE_MODEL=claude-opus-4-6                 (default)
  POLL_INTERVAL=2                               (seconds, default)
  CONTEXT_MESSAGES=20                           (messages to include as context)
  DEBUG=0                                       (set to 1 for verbose output)
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque

import requests
import anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BRIDGE_URL = os.environ.get("AGENTBRIDGE_URL", "http://172.31.141.155:7890")
AGENTBRIDGE_TOKEN = os.environ.get("AGENTBRIDGE_TOKEN", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "claude")
AGENT_ROLE = os.environ.get("AGENT_ROLE", "AI Assistant — powered by Claude Opus 4.6")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2"))
CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "20"))
HEARTBEAT_INTERVAL = 30  # seconds — well within the 4h TTL
DEBUG = os.environ.get("DEBUG", "0") == "1"

SYSTEM_PROMPT = f"""\
You are {AGENT_NAME}, an AI assistant connected to AgentBridge — a multi-agent \
communication platform where AI agents and humans collaborate in real time.

You are a helpful, thoughtful, and concise collaborator. You can assist with:
- Answering questions and explaining concepts
- Writing, reviewing, and debugging code
- Analyzing problems and suggesting solutions
- Coordinating with other agents in the platform

Keep responses focused and appropriately brief for a chat interface. Use markdown \
for code blocks and formatting when helpful. Do NOT prefix your response with your \
own name — the UI already shows the sender.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

anthropic_client = anthropic.Anthropic()

# Per-thread conversation context: deque of message dicts
context_by_thread: dict[str, deque] = defaultdict(lambda: deque(maxlen=CONTEXT_MESSAGES))


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if AGENTBRIDGE_TOKEN:
        h["X-AgentBridge-Token"] = AGENTBRIDGE_TOKEN
    return h


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _debug(msg: str) -> None:
    if DEBUG:
        _log(f"[DEBUG] {msg}")


# ---------------------------------------------------------------------------
# AgentBridge API calls
# ---------------------------------------------------------------------------


def register() -> None:
    payload = {
        "name": AGENT_NAME,
        "role": AGENT_ROLE,
        "capabilities": ["chat", "code", "analysis", "reasoning", "planning"],
        "agent_type": "bot",
    }
    resp = requests.post(
        f"{BRIDGE_URL}/agents",
        json=payload,
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    _log(f"Registered as '{AGENT_NAME}' on {BRIDGE_URL}")


def send_heartbeat() -> None:
    try:
        requests.post(
            f"{BRIDGE_URL}/agents/{AGENT_NAME}/heartbeat",
            json={"status": "online"},
            headers=_headers(),
            timeout=5,
        )
        _debug("Heartbeat sent")
    except Exception as exc:
        _log(f"Heartbeat error: {exc}")


def _heartbeat_loop() -> None:
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        send_heartbeat()


def fetch_messages(limit: int = 100, since_id: str | None = None) -> list[dict]:
    params: dict = {"limit": limit}
    if since_id:
        params["since_id"] = since_id
    resp = requests.get(
        f"{BRIDGE_URL}/messages",
        params=params,
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def post_message(content: str, recipient: str | None = None, thread: str = "general") -> None:
    payload: dict = {
        "sender": AGENT_NAME,
        "content": content,
        "thread": thread,
    }
    if recipient:
        payload["recipient"] = recipient
    requests.post(
        f"{BRIDGE_URL}/messages",
        json=payload,
        headers=_headers(),
        timeout=10,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Mention detection
# ---------------------------------------------------------------------------

# Pre-compile mention patterns for the configured agent name
_MENTION_PATTERN = re.compile(
    rf"@{re.escape(AGENT_NAME)}\b",
    re.IGNORECASE,
)
# Also catch bare "claude" even without @ in DM context
_BARE_PATTERN = re.compile(
    rf"\b{re.escape(AGENT_NAME)}\b",
    re.IGNORECASE,
)


def is_addressed(msg: dict) -> bool:
    """Return True if this message is directed at our agent."""
    sender = msg.get("sender", "")
    recipient = msg.get("recipient", "")
    content = msg.get("content", "")

    # Never react to our own messages
    if sender == AGENT_NAME:
        return False

    # Direct message (recipient field set to our name)
    if recipient and recipient.lower() == AGENT_NAME.lower():
        return True

    # @mention in broadcast message
    if _MENTION_PATTERN.search(content):
        return True

    return False


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------


def build_messages(thread_context: list[dict], new_msg: dict) -> list[dict]:
    """
    Build the messages list for the Claude API from thread history.
    Claude requires strictly alternating user/assistant roles.
    We label our own previous messages as assistant and everything else as user.
    Multiple consecutive same-role messages are merged.
    """
    raw: list[dict] = []

    for m in thread_context:
        role = "assistant" if m.get("sender") == AGENT_NAME else "user"
        sender_label = "" if role == "assistant" else f"{m['sender']}: "
        raw.append({"role": role, "content": f"{sender_label}{m['content']}"})

    # Add the triggering message
    raw.append({
        "role": "user",
        "content": f"{new_msg['sender']}: {new_msg['content']}",
    })

    # Merge consecutive same-role turns (required by Claude API)
    merged: list[dict] = []
    for turn in raw:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += f"\n{turn['content']}"
        else:
            merged.append(dict(turn))

    # Claude API requires starting with a user message
    if merged and merged[0]["role"] != "user":
        merged = merged[1:]

    return merged


def report_cost_event(input_tokens: int, output_tokens: int) -> None:
    """Report token usage to AgentBridge cost tracking (best-effort)."""
    # Approximate cost in cents: Opus 4.6 = $15/M input, $75/M output
    cost_cents = round((input_tokens * 15 + output_tokens * 75) / 1_000_000 * 100)
    try:
        requests.post(
            f"{BRIDGE_URL}/cost-events",
            json={
                "agent_name": AGENT_NAME,
                "model": CLAUDE_MODEL,
                "provider": "anthropic",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_cents": cost_cents,
            },
            headers=_headers(),
            timeout=5,
        )
        _debug(f"Reported cost: {input_tokens}in/{output_tokens}out = {cost_cents}¢")
    except Exception as exc:
        _debug(f"Cost report failed (non-fatal): {exc}")


def call_claude(messages: list[dict], thread: str) -> str:
    """Call Claude API and return the response text."""
    system = SYSTEM_PROMPT + f"\nCurrent channel: #{thread}"

    response = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=system,
        messages=messages,
        thinking={"type": "adaptive"},
    )

    # Report usage to AgentBridge cost tracker
    if response.usage:
        report_cost_event(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    for block in response.content:
        if block.type == "text":
            return block.text.strip()

    return "(no response)"


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def main() -> None:
    _log(f"Starting Claude agent — model: {CLAUDE_MODEL}")

    # Register
    try:
        register()
    except Exception as exc:
        _log(f"Registration failed: {exc}")
        sys.exit(1)

    # Seed context from recent messages (don't respond to old ones)
    try:
        seed = fetch_messages(limit=30)
    except Exception as exc:
        _log(f"Failed to fetch initial messages: {exc}")
        seed = []

    last_id: str | None = None
    for msg in seed:
        thread = msg.get("thread", "general")
        context_by_thread[thread].append(msg)
        last_id = msg.get("id", last_id)

    _log(f"Seeded {len(seed)} messages. last_id={last_id}. Listening for @{AGENT_NAME}...")

    # Heartbeat thread
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()

    # Poll loop
    while True:
        try:
            msgs = fetch_messages(limit=100, since_id=last_id)

            for msg in msgs:
                msg_id = msg.get("id")
                thread = msg.get("thread", "general")

                # Update context first
                context_by_thread[thread].append(msg)

                if is_addressed(msg):
                    sender = msg.get("sender", "unknown")
                    content_preview = msg.get("content", "")[:80]
                    _log(f"Mentioned by {sender} in #{thread}: {content_preview}")

                    # Build context (exclude the triggering message itself — it's appended above)
                    thread_history = list(context_by_thread[thread])[:-1]

                    try:
                        claude_msgs = build_messages(thread_history, msg)
                        _debug(f"Sending {len(claude_msgs)} messages to Claude")

                        response_text = call_claude(claude_msgs, thread)

                        # Reply as DM if the message was a DM to us, otherwise broadcast to thread
                        reply_recipient: str | None = None
                        if msg.get("recipient") == AGENT_NAME:
                            reply_recipient = sender

                        post_message(response_text, recipient=reply_recipient, thread=thread)
                        _log(f"Responded to {sender}: {response_text[:80]}{'...' if len(response_text) > 80 else ''}")

                    except anthropic.APIError as exc:
                        _log(f"Claude API error: {exc}")
                        try:
                            post_message(
                                f"Sorry, I hit an API error: {exc}",
                                recipient=sender,
                                thread=thread,
                            )
                        except Exception:
                            pass
                    except Exception as exc:
                        _log(f"Unexpected error handling message: {exc}")

                if msg_id:
                    last_id = msg_id

        except requests.RequestException as exc:
            _log(f"Network error: {exc}")

        except Exception as exc:
            _log(f"Poll loop error: {exc}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
