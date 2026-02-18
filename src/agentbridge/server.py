"""AgentBridge server — MCP tools (stdio) + HTTP API (FastAPI)."""

import asyncio
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Query
from mcp.server.fastmcp import FastMCP

from .models import Agent, Message, Thread
from .store import MessageStore

# Shared store
store = MessageStore()

# ── HTTP API (FastAPI) ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    store.close()

http_app = FastAPI(title="AgentBridge", version="0.1.0", lifespan=lifespan)


@http_app.post("/agents")
def http_register_agent(body: dict):
    agent = store.register_agent(body["name"], body.get("role", ""))
    return agent.model_dump(mode="json")


@http_app.get("/agents")
def http_list_agents():
    return [a.model_dump(mode="json") for a in store.list_agents()]


@http_app.post("/messages")
def http_send_message(body: dict):
    msg = store.add_message(
        sender=body["sender"],
        content=body["content"],
        recipient=body.get("recipient"),
        thread=body.get("thread", "general"),
    )
    return msg.model_dump(mode="json")


@http_app.get("/messages")
def http_read_messages(
    thread: str | None = Query(None),
    sender: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(50),
):
    return [m.model_dump(mode="json") for m in store.read_messages(thread=thread, since=since, limit=limit, sender=sender)]


@http_app.post("/threads")
def http_create_thread(body: dict):
    t = store.create_thread(body["name"], body["created_by"])
    return t.model_dump(mode="json")


@http_app.get("/threads")
def http_list_threads():
    return [t.model_dump(mode="json") for t in store.list_threads()]


@http_app.get("/health")
def health():
    return {"status": "ok"}


# ── MCP Server (FastMCP) ───────────────────────────────────────────

mcp = FastMCP("AgentBridge", instructions="Inter-agent communication server. Use these tools to send and receive messages with other AI agents working on the same codebase.")


@mcp.tool()
def register(name: str, role: str = "") -> str:
    """Register this agent with the bridge. Call this first with a unique name and optional role description."""
    agent = store.register_agent(name, role)
    return f"Registered as '{agent.name}'" + (f" ({agent.role})" if agent.role else "")


@mcp.tool()
def send(sender: str, content: str, recipient: str | None = None, thread: str = "general") -> str:
    """Send a message. Set recipient to target a specific agent, or leave empty to broadcast."""
    msg = store.add_message(sender=sender, content=content, recipient=recipient, thread=thread)
    target = f" → {msg.recipient}" if msg.recipient else " (broadcast)"
    return f"Sent to [{msg.thread}]{target}: {msg.content}"


@mcp.tool()
def read(thread: str | None = None, sender: str | None = None, limit: int = 20) -> str:
    """Read recent messages. Optionally filter by thread or sender."""
    messages = store.read_messages(thread=thread, sender=sender, limit=limit)
    if not messages:
        return "No messages."
    lines = []
    for m in messages:
        prefix = f"[{m.thread}] " if m.thread != "general" else ""
        to = f" → {m.recipient}" if m.recipient else ""
        lines.append(f"{prefix}{m.sender}{to}: {m.content}")
    return "\n".join(lines)


@mcp.tool()
def agents() -> str:
    """List all connected agents."""
    agent_list = store.list_agents()
    if not agent_list:
        return "No agents connected."
    return "\n".join(f"  {a.name}" + (f" ({a.role})" if a.role else "") for a in agent_list)


@mcp.tool()
def threads() -> str:
    """List active threads."""
    thread_list = store.list_threads()
    if not thread_list:
        return "No threads."
    return "\n".join(f"  #{t.name} (by {t.created_by})" for t in thread_list)


@mcp.tool()
def create_thread(name: str, created_by: str) -> str:
    """Create a named discussion thread."""
    t = store.create_thread(name, created_by)
    return f"Created thread: #{t.name}"


# ── Entry point ─────────────────────────────────────────────────────

def _run_http_server():
    """Run HTTP server in a background thread."""
    uvicorn.run(http_app, host="0.0.0.0", port=7890, log_level="warning")


def main():
    """Start both MCP (stdio) and HTTP servers."""
    # Start HTTP in background thread
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()

    # Run MCP on stdio (blocking)
    mcp.run()


if __name__ == "__main__":
    main()
