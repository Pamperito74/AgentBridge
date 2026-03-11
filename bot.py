#!/usr/bin/env python3
"""
claude-ct1 persistent bot — listens on AgentBridge and responds using the Claude API.

Handles:
  - Incoming WS requests  (request_agent() calls from other agents)
  - Direct messages       (recipient == BOT_NAME)
  - @mentions             (content contains @claude-ct1)

Environment variables:
  ANTHROPIC_API_KEY       Required. Claude API key.
  AGENTBRIDGE_URL         Bridge WebSocket base URL (default: ws://172.31.141.155:7890)
  AGENTBRIDGE_HTTP_URL    Bridge HTTP base URL      (default: http://172.31.141.155:7890)
  AGENTBRIDGE_TOKEN       Auth token (same one used by the dashboard)
  BOT_NAME                Agent name (default: claude-ct1)
  BOT_MODEL               Claude model  (default: claude-sonnet-4-6)
  BOT_CONTEXT_MESSAGES    How many recent messages to include as context (default: 10)

Run:
  python bot.py

Deploy (systemd):
  sudo cp claude-ct1-bot.service /etc/systemd/system/
  sudo systemctl enable --now claude-ct1-bot
"""

import asyncio
import json
import logging
import os
import re
import sys
import urllib.request
from typing import Any

import anthropic

# Allow running from repo root without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from agentbridge.client import AgentBridgeClient  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("claude-ct1-bot")


def _load_token_from_shell() -> str:
    """Read AGENTBRIDGE_TOKEN from ~/.zshrc or ~/.zprofile if not in env."""
    for path in [os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.zprofile")]:
        try:
            m = re.search(r"AGENTBRIDGE_TOKEN=['\"]([^'\"]+)['\"]", open(path).read())
            if m:
                return m.group(1)
        except OSError:
            pass
    return ""


BOT_NAME = os.environ.get("BOT_NAME", "claude-ct1")
BOT_MODEL = os.environ.get("BOT_MODEL", "claude-sonnet-4-6")
WS_URL = os.environ.get("AGENTBRIDGE_URL", "ws://172.31.141.155:7890")
HTTP_URL = os.environ.get("AGENTBRIDGE_HTTP_URL", "http://172.31.141.155:7890")
TOKEN = os.environ.get("AGENTBRIDGE_TOKEN", "") or _load_token_from_shell()
CONTEXT_MESSAGES = int(os.environ.get("BOT_CONTEXT_MESSAGES", "10"))
CAPABILITIES = ["code-review", "backend", "typescript", "python", "agentbridge", "nestjs", "react"]


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are {BOT_NAME}, an AI coding agent connected to an AgentBridge message bus.

You work across two codebases:
- **AgentBridge** — Python FastAPI + SQLite + MCP + WebSocket platform for inter-agent communication (this repo)
- **CirrusTranslate** — NestJS multi-tenant SaaS for translation project management (Prisma/PostgreSQL, Next.js)

Your capabilities:
- Code review (TypeScript, Python, NestJS, React/Next.js, Prisma)
- Architecture and design decisions
- Debugging and root cause analysis
- Writing and refactoring code
- Answering technical questions about both codebases
- PR reviews for Cirrus-Inc/CirrusTranslate

Style:
- Be concise, technical, and direct
- Reference specific files/lines when discussing code
- For PR reviews: use the format "APPROVE / REQUEST CHANGES" with a findings table
- Don't pad responses with pleasantries

You are talking to other AI agents or human operators. Respond as a capable peer."""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get(path: str, params: dict | None = None) -> Any:
    from urllib.parse import urlencode
    url = HTTP_URL.rstrip("/") + path
    if params:
        qs = urlencode({k: v for k, v in params.items() if v is not None})
        if qs:
            url += "?" + qs
    headers = {"X-AgentBridge-Token": TOKEN} if TOKEN else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def _http_post(path: str, body: dict) -> Any:
    url = HTTP_URL.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["X-AgentBridge-Token"] = TOKEN
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def _fetch_context(thread: str = "general", limit: int = CONTEXT_MESSAGES) -> list[dict]:
    """Fetch recent messages from the bridge for conversation context."""
    try:
        return _http_get("/messages", {"thread": thread, "limit": limit})
    except Exception as e:
        logger.warning(f"Failed to fetch context: {e}")
        return []


def _post_message(content: str, recipient: str | None = None, thread: str = "general") -> None:
    """Post a message back to the bridge."""
    try:
        _http_post("/messages", {
            "sender": BOT_NAME,
            "content": content,
            "recipient": recipient,
            "thread": thread,
        })
    except Exception as e:
        logger.error(f"Failed to post message: {e}")


# ── Claude API ────────────────────────────────────────────────────────────────

_anthropic_client: anthropic.Anthropic | None = None


def get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def call_claude(user_message: str, context_messages: list[dict] | None = None) -> str:
    """Call Claude API and return the response text."""
    messages = []

    # Include recent bridge messages as context
    if context_messages:
        context_lines = []
        for m in context_messages[-CONTEXT_MESSAGES:]:
            sender = m.get("sender", "?")
            recipient = m.get("recipient")
            content = m.get("content", "")[:500]
            to = f" → {recipient}" if recipient else ""
            context_lines.append(f"[{sender}{to}]: {content}")
        if context_lines:
            context_block = "Recent bridge messages (for context):\n" + "\n".join(context_lines)
            messages.append({"role": "user", "content": context_block})
            messages.append({"role": "assistant", "content": "Understood, I have the context."})

    messages.append({"role": "user", "content": user_message})

    response = get_anthropic().messages.create(
        model=BOT_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text


# ── Extended client ───────────────────────────────────────────────────────────

class ClaudeBotClient(AgentBridgeClient):
    """AgentBridgeClient extended to handle chat messages (type=message) in addition to requests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_ids: set[str] = set()

    async def connect(self, capabilities: list[str] | None = None) -> None:
        """Connect with auth token header support."""
        import websockets

        ws_url = self.server_url.rstrip("/")
        if not ws_url.startswith("ws"):
            ws_url = "ws://" + ws_url.lstrip("http://").lstrip("https://")

        extra_headers = {}
        if TOKEN:
            extra_headers["X-AgentBridge-Token"] = TOKEN

        self.ws = await websockets.connect(
            f"{ws_url}/ws",
            extra_headers=extra_headers,
        )
        self._running = True

        await self.ws.send(json.dumps({
            "type": "register",
            "name": self.name,
            "role": self.role,
            "capabilities": capabilities or [],
        }))

        import asyncio
        self._read_task = asyncio.create_task(self._read_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Connected to AgentBridge as {self.name}")

    async def _read_loop(self) -> None:
        if not self.ws:
            return
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mtype = msg.get("type")

                if mtype == "response":
                    cid = msg.get("correlation_id")
                    if cid in self.pending_requests:
                        fut = self.pending_requests.pop(cid)
                        if msg.get("status") == "success":
                            fut.set_result(msg.get("content"))
                        else:
                            fut.set_exception(RuntimeError(msg.get("content", "error")))

                elif mtype == "request":
                    if self.request_handler:
                        asyncio.create_task(self._handle_request(msg))

                elif mtype in ("message", "registered"):
                    if mtype == "message":
                        asyncio.create_task(self._handle_chat(msg))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Read loop error: {e}")
            self._running = False

    async def _handle_request(self, msg: dict) -> None:
        """Handle a synchronous request — calls Claude and responds."""
        cid = msg.get("correlation_id")
        sender = msg.get("sender", "unknown")
        content = msg.get("content", "")
        thread = msg.get("thread", "general")
        logger.info(f"Request from {sender}: {content[:100]}")

        try:
            context = await asyncio.to_thread(_fetch_context, thread)
            reply = await asyncio.to_thread(call_claude, content, context)
            await self._send_json({
                "type": "response",
                "correlation_id": cid,
                "content": reply,
                "status": "success",
            })
        except Exception as e:
            logger.error(f"Request handler error: {e}")
            await self._send_json({
                "type": "response",
                "correlation_id": cid,
                "content": f"Error: {e}",
                "status": "error",
            })

    async def _handle_chat(self, msg: dict) -> None:
        """Handle a chat message — respond only to DMs and @mentions."""
        msg_id = msg.get("id", "")
        if msg_id:
            if msg_id in self._seen_ids:
                return
            self._seen_ids.add(msg_id)
            if len(self._seen_ids) > 1000:
                self._seen_ids = set(list(self._seen_ids)[-500:])

        sender = msg.get("sender", "")
        if sender == self.name:
            return  # don't respond to own messages

        recipient = msg.get("recipient")
        content = msg.get("content", "")
        mentions = msg.get("mentions", [])
        thread = msg.get("thread", "general")

        is_dm = recipient == self.name
        is_mention = self.name in mentions or f"@{self.name}" in content

        if not (is_dm or is_mention):
            return

        logger.info(f"Message from {sender} ({'DM' if is_dm else '@mention'}): {content[:100]}")

        try:
            context = await asyncio.to_thread(_fetch_context, thread)
            # Strip the @mention prefix so Claude sees a clean message
            clean = content.replace(f"@{self.name}", "").strip()
            reply = await asyncio.to_thread(call_claude, clean or content, context)
            await asyncio.to_thread(
                _post_message, reply, sender if is_dm else None, thread
            )
        except Exception as e:
            logger.error(f"Chat handler error: {e}")
            await asyncio.to_thread(
                _post_message, f"Sorry, I hit an error: {e}", sender if is_dm else None, thread
            )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set — exiting")
        sys.exit(1)

    bot = ClaudeBotClient(
        name=BOT_NAME,
        role="AI coding agent — AgentBridge + CirrusTranslate",
        server_url=WS_URL,
        http_url=HTTP_URL,
    )

    logger.info(f"Starting {BOT_NAME} bot (model={BOT_MODEL})")
    await bot.run(capabilities=CAPABILITIES)


if __name__ == "__main__":
    asyncio.run(main())
