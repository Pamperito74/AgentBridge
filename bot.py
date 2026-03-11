#!/usr/bin/env python3
"""
AgentBridge persistent bot — listens on the bridge and responds using any
OpenAI-compatible LLM API (Ollama, Groq, Together, OpenAI, Mistral, etc.)

Handles:
  - Incoming WS requests  (request_agent() calls from other agents)
  - Direct messages       (recipient == BOT_NAME)
  - @mentions             (content contains @BOT_NAME)

Environment variables:
  LLM_BASE_URL            OpenAI-compatible base URL
                          (default: http://localhost:11434/v1  — Ollama)
  LLM_MODEL               Model name (default: llama3.2)
  LLM_API_KEY             API key, if required (default: "ollama")
  LLM_MAX_TOKENS          Max tokens per response (default: 2048)

  AGENTBRIDGE_URL         Bridge WebSocket base URL (default: ws://localhost:7890)
  AGENTBRIDGE_HTTP_URL    Bridge HTTP base URL      (default: http://localhost:7890)
  AGENTBRIDGE_TOKEN       Auth token
  BOT_NAME                Agent name (default: agentbridge-bot)
  BOT_ROLE                Agent role string
  BOT_CAPABILITIES        Comma-separated capabilities list
  BOT_CONTEXT_MESSAGES    Recent messages to include as context (default: 10)
  BOT_SYSTEM_PROMPT       Override the default system prompt

  LLM_COMMAND             Use a CLI command instead of HTTP API.
                          The prompt is written to stdin; stdout is the response.
                          When set, LLM_BASE_URL / LLM_MODEL / LLM_API_KEY are ignored.
                          Examples:
                            LLM_COMMAND=claude          (Claude Code CLI — no API key needed)
                            LLM_COMMAND=ollama run llama3.2

Run with Claude CLI (no API key):
  LLM_COMMAND=claude python bot.py

Run locally (Ollama HTTP):
  ollama pull llama3.2
  python bot.py

Run with Groq (no GPU needed, free tier):
  LLM_BASE_URL=https://api.groq.com/openai/v1 \\
  LLM_MODEL=llama-3.3-70b-versatile \\
  LLM_API_KEY=gsk_... \\
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
import shlex
import subprocess
import sys
import urllib.request
import urllib.error
from typing import Any

# Allow running from repo root without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from agentbridge.client import AgentBridgeClient  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("agentbridge.bot")


# ── Token helper ──────────────────────────────────────────────────────────────

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


# ── Config ────────────────────────────────────────────────────────────────────

BOT_NAME = os.environ.get("BOT_NAME", "agentbridge-bot")
BOT_ROLE = os.environ.get("BOT_ROLE", "AI assistant bot")
BOT_CAPABILITIES = [
    c.strip() for c in os.environ.get("BOT_CAPABILITIES", "chat,code-review,qa").split(",") if c.strip()
]
WS_URL = os.environ.get("AGENTBRIDGE_URL", "ws://localhost:7890")
HTTP_URL = os.environ.get("AGENTBRIDGE_HTTP_URL", "http://localhost:7890")
TOKEN = os.environ.get("AGENTBRIDGE_TOKEN", "") or _load_token_from_shell()
CONTEXT_MESSAGES = int(os.environ.get("BOT_CONTEXT_MESSAGES", "10"))

# LLM config
# Option A — subprocess (CLI): set LLM_COMMAND, e.g. "claude" or "ollama run llama3.2"
# Option B — HTTP API: set LLM_BASE_URL (OpenAI-compatible), LLM_MODEL, LLM_API_KEY
LLM_COMMAND = os.environ.get("LLM_COMMAND", "").strip()  # e.g. "claude" or "ollama run llama3.2"
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.2")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "ollama")  # Ollama ignores this
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))

DEFAULT_SYSTEM_PROMPT = f"""You are {BOT_NAME}, an AI assistant connected to an AgentBridge message bus.
Other agents and human operators may send you messages. Be concise, helpful, and direct.
You do not need to explain that you are an AI."""

SYSTEM_PROMPT = os.environ.get("BOT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


# ── LLM call (OpenAI-compatible) ──────────────────────────────────────────────

def _build_prompt(user_message: str, context_messages: list[dict] | None = None) -> tuple[str, list[dict]]:
    """Return (full_text_prompt, openai_messages_list) for both backends."""
    context_block = ""
    if context_messages:
        lines = []
        for m in context_messages[-CONTEXT_MESSAGES:]:
            sender = m.get("sender", "?")
            recipient = m.get("recipient")
            content = m.get("content", "")[:400]
            to = f" → {recipient}" if recipient else ""
            lines.append(f"[{sender}{to}]: {content}")
        if lines:
            context_block = "Recent bridge messages (context):\n" + "\n".join(lines) + "\n\n"

    full_text = f"{SYSTEM_PROMPT}\n\n{context_block}{user_message}"

    openai_messages: list[dict] = []
    if context_block:
        openai_messages.append({"role": "user", "content": context_block.strip()})
        openai_messages.append({"role": "assistant", "content": "Understood."})
    openai_messages.append({"role": "user", "content": user_message})

    return full_text, openai_messages


def call_llm(user_message: str, context_messages: list[dict] | None = None) -> str:
    """Call the configured LLM — subprocess CLI or OpenAI-compatible HTTP."""
    full_text, openai_messages = _build_prompt(user_message, context_messages)

    if LLM_COMMAND:
        # ── Subprocess path (e.g. LLM_COMMAND=claude or LLM_COMMAND=ollama run llama3.2) ──
        result = subprocess.run(
            shlex.split(LLM_COMMAND),
            input=full_text,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LLM command exited {result.returncode}: {result.stderr[:200]}")
        return result.stdout.strip()

    # ── OpenAI-compatible HTTP path ──────────────────────────────────────────
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + openai_messages,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read().decode())
            return result["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")


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
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def _fetch_context(thread: str = "general") -> list[dict]:
    try:
        return _http_get("/messages", {"thread": thread, "limit": CONTEXT_MESSAGES})
    except Exception as e:
        logger.warning(f"Failed to fetch context: {e}")
        return []


def _post_message(content: str, recipient: str | None = None, thread: str = "general") -> None:
    try:
        _http_post("/messages", {
            "sender": BOT_NAME,
            "content": content,
            "recipient": recipient,
            "thread": thread,
        })
    except Exception as e:
        logger.error(f"Failed to post message: {e}")


# ── Extended client ───────────────────────────────────────────────────────────

class BotClient(AgentBridgeClient):
    """AgentBridgeClient extended to handle chat messages and auth tokens."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_ids: set[str] = set()

    async def connect(self, capabilities: list[str] | None = None) -> None:
        import websockets

        ws_url = self.server_url.rstrip("/")
        if not ws_url.startswith("ws"):
            ws_url = "ws://" + re.sub(r"^https?://", "", ws_url)

        extra_headers = {}
        if TOKEN:
            extra_headers["X-AgentBridge-Token"] = TOKEN

        self.ws = await websockets.connect(f"{ws_url}/ws", extra_headers=extra_headers)
        self._running = True

        await self.ws.send(json.dumps({
            "type": "register",
            "name": self.name,
            "role": self.role,
            "capabilities": capabilities or [],
        }))

        self._read_task = asyncio.create_task(self._read_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Connected as '{self.name}' (model={LLM_MODEL} @ {LLM_BASE_URL})")

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
                    asyncio.create_task(self._handle_request(msg))

                elif mtype == "message":
                    asyncio.create_task(self._handle_chat(msg))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Read loop error: {e}")
            self._running = False

    async def _handle_request(self, msg: dict) -> None:
        cid = msg.get("correlation_id")
        sender = msg.get("sender", "unknown")
        content = msg.get("content", "")
        thread = msg.get("thread", "general")
        logger.info(f"Request from {sender}: {content[:120]}")
        try:
            context = await asyncio.to_thread(_fetch_context, thread)
            reply = await asyncio.to_thread(call_llm, content, context)
            await self._send_json({
                "type": "response",
                "correlation_id": cid,
                "content": reply,
                "status": "success",
            })
        except Exception as e:
            logger.error(f"Request error: {e}")
            await self._send_json({
                "type": "response",
                "correlation_id": cid,
                "content": f"Error: {e}",
                "status": "error",
            })

    async def _handle_chat(self, msg: dict) -> None:
        msg_id = msg.get("id", "")
        if msg_id:
            if msg_id in self._seen_ids:
                return
            self._seen_ids.add(msg_id)
            if len(self._seen_ids) > 1000:
                self._seen_ids = set(list(self._seen_ids)[-500:])

        sender = msg.get("sender", "")
        if sender == self.name:
            return

        recipient = msg.get("recipient")
        content = msg.get("content", "")
        mentions = msg.get("mentions", [])
        thread = msg.get("thread", "general")

        is_dm = recipient == self.name
        is_mention = self.name in mentions or f"@{self.name}" in content
        if not (is_dm or is_mention):
            return

        logger.info(f"Chat from {sender} ({'DM' if is_dm else '@mention'}): {content[:120]}")
        try:
            context = await asyncio.to_thread(_fetch_context, thread)
            clean = content.replace(f"@{self.name}", "").strip() or content
            reply = await asyncio.to_thread(call_llm, clean, context)
            await asyncio.to_thread(_post_message, reply, sender if is_dm else None, thread)
        except Exception as e:
            logger.error(f"Chat error: {e}")
            await asyncio.to_thread(
                _post_message, f"Error: {e}", sender if is_dm else None, thread
            )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    backend = f"command={LLM_COMMAND}" if LLM_COMMAND else f"model={LLM_MODEL} url={LLM_BASE_URL}"
    logger.info(f"BOT_NAME={BOT_NAME}  LLM backend: {backend}")

    # Smoke-test LLM connection at startup
    try:
        await asyncio.to_thread(call_llm, "Reply with just: ok")
        logger.info("LLM connection OK")
    except Exception as e:
        logger.error(f"LLM not reachable: {e}")
        logger.error("Set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY and try again.")
        sys.exit(1)

    bot = BotClient(
        name=BOT_NAME,
        role=BOT_ROLE,
        server_url=WS_URL,
        http_url=HTTP_URL,
    )
    await bot.run(capabilities=BOT_CAPABILITIES)


if __name__ == "__main__":
    asyncio.run(main())
