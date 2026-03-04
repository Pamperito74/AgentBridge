"""AgentBridge server — MCP tools (stdio) + HTTP API (FastAPI) + SSE + Web Dashboard."""

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .models import MESSAGE_TYPES
from .schema_registry import SchemaRegistry, SchemaValidationError
from .store import MessageStore
from .ws_manager import get_ws_manager

# Shared store (lazy initialized to avoid import-time DB side effects)
store: MessageStore | None = None
_store_lock = threading.Lock()


def get_store() -> MessageStore:
    global store
    with _store_lock:
        if store is None:
            store = MessageStore()
        return store

# SSE subscribers: list of (event loop, asyncio.Queue)
_sse_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []
_sse_lock = threading.Lock()
_auth_token = os.environ.get("AGENTBRIDGE_TOKEN", "")


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("agentbridge")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    configured = os.environ.get("AGENTBRIDGE_LOG_DIR")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        Path.home() / ".agentbridge" / "logs",
        Path.cwd() / ".agentbridge" / "logs",
        Path("/tmp") / "agentbridge-logs",
    ])
    for log_dir in candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "agentbridge.log"
            handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            break
        except OSError:
            continue
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    return logger


logger = _setup_logging()
schema_registry = SchemaRegistry()


def _safe_put(queue: asyncio.Queue[str], payload: str):
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        return


def _broadcast_sse(event: str, data: dict):
    """Push an event to all SSE subscribers."""
    payload = json.dumps(data, default=str)
    message = f"event: {event}\ndata: {payload}\n\n"
    with _sse_lock:
        subscribers = list(_sse_subscribers)
    for loop, queue in subscribers:
        loop.call_soon_threadsafe(_safe_put, queue, message)


# ── HTTP API (FastAPI) ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    get_store()
    yield
    global store
    with _store_lock:
        if store is not None:
            store.close()
            store = None

http_app = FastAPI(title="AgentBridge", version="0.2.0", lifespan=lifespan)


@http_app.middleware("http")
async def auth_and_logging_middleware(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    if _auth_token and path != "/health":
        supplied = request.headers.get("x-agentbridge-token") or request.query_params.get("token")
        if supplied != _auth_token:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("%s %s %s %sms", request.method, path, response.status_code, elapsed_ms)
    return response

class RegisterAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="", max_length=512)


class HeartbeatRequest(BaseModel):
    status: Literal["online", "busy", "idle"] = "online"
    working_on: str = Field(default="", max_length=512)


class ArtifactRequest(BaseModel):
    type: Literal["file", "code", "url"]
    content: str = Field(min_length=1, max_length=4000)


class SendMessageRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=10000)
    recipient: str | None = Field(default=None, max_length=128)
    thread: str = Field(default="general", min_length=1, max_length=128)
    msg_type: Literal["chat", "request", "status", "alert"] = "chat"
    artifacts: list[ArtifactRequest] | None = None


class CreateThreadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    created_by: str = Field(min_length=1, max_length=128)


class EventWriteRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=128)
    actor_type: str = Field(default="agent", min_length=1, max_length=64)
    target_id: str | None = Field(default=None, max_length=128)
    target_type: str | None = Field(default=None, max_length=64)
    thread: str = Field(default="general", min_length=1, max_length=128)
    event_type: str = Field(default="note.text", min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=10000)
    metadata: dict = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)


class EventSchemaWriteRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=128)
    definition: dict = Field(alias="schema")

    model_config = {"populate_by_name": True}


# --- Agents ---

@http_app.post("/agents")
def http_register_agent(body: RegisterAgentRequest):
    agent = get_store().register_agent(body.name, body.role)
    result = agent.model_dump(mode="json")
    _broadcast_sse("agent_joined", result)
    return result


@http_app.get("/agents")
def http_list_agents():
    return [a.model_dump(mode="json") for a in get_store().list_agents()]


@http_app.get("/actors")
def http_list_actors():
    actors = []
    for a in get_store().list_agents():
        data = a.model_dump(mode="json")
        actors.append({
            "id": data["name"],
            "type": "agent",
            "status": data.get("status", "online"),
            "role": data.get("role", ""),
            "working_on": data.get("working_on", ""),
            "last_seen": data.get("last_seen"),
        })
    return actors


@http_app.post("/agents/{name}/heartbeat")
def http_heartbeat(name: str, body: HeartbeatRequest):
    agent = get_store().heartbeat(
        name, status=body.status, working_on=body.working_on
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    result = agent.model_dump(mode="json")
    _broadcast_sse("agent_status", result)
    return result


# --- Messages ---

@http_app.post("/messages")
def http_send_message(body: SendMessageRequest):
    msg = get_store().add_message(
        sender=body.sender,
        content=body.content,
        recipient=body.recipient,
        thread=body.thread,
        msg_type=body.msg_type,
        artifacts=[a.model_dump() for a in body.artifacts] if body.artifacts else None,
    )
    result = msg.model_dump(mode="json")
    _broadcast_sse("message", result)
    return result


@http_app.get("/messages")
def http_read_messages(
    thread: str | None = Query(None),
    sender: str | None = Query(None),
    as_agent: str | None = Query(None),
    since: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    return [
        m.model_dump(mode="json")
        for m in get_store().read_messages(
            thread=thread, since=since, before=before, limit=limit, sender=sender, as_agent=as_agent,
        )
    ]


@http_app.post("/bus/events")
def http_send_event(body: EventWriteRequest):
    try:
        schema_registry.validate(body.event_type, body.metadata)
    except SchemaValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    msg = get_store().add_message(
        sender=body.actor_id,
        recipient=body.target_id,
        thread=body.thread,
        msg_type="chat",
        content=body.content,
        actor_id=body.actor_id,
        actor_type=body.actor_type,
        target_id=body.target_id,
        target_type=body.target_type,
        event_type=body.event_type,
        metadata=body.metadata,
        labels=body.labels,
    )
    result = msg.model_dump(mode="json")
    _broadcast_sse("event", result)
    _broadcast_sse("message", result)
    return result


@http_app.get("/bus/events")
def http_read_events(
    thread: str | None = Query(None),
    actor_id: str | None = Query(None),
    target_id: str | None = Query(None),
    event_type: str | None = Query(None),
    since: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    return [
        m.model_dump(mode="json")
        for m in get_store().read_messages(
            thread=thread, actor_id=actor_id, target_id=target_id,
            event_type=event_type, since=since, before=before, limit=limit,
        )
    ]


@http_app.get("/bus/schemas")
def http_list_event_schemas():
    return schema_registry.list()


@http_app.post("/bus/schemas")
def http_register_event_schema(body: EventSchemaWriteRequest):
    try:
        schema_registry.upsert(body.event_type, body.definition)
    except SchemaValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "event_type": body.event_type}


# --- Threads ---

@http_app.post("/threads")
def http_create_thread(body: CreateThreadRequest):
    t = get_store().create_thread(body.name, body.created_by)
    result = t.model_dump(mode="json")
    _broadcast_sse("thread_created", result)
    return result


@http_app.get("/threads")
def http_list_threads():
    return [t.model_dump(mode="json") for t in get_store().list_threads()]


@http_app.get("/threads/{name}/summary")
def http_thread_summary(name: str):
    return get_store().thread_summary(name).model_dump(mode="json")


# --- SSE ---

@http_app.get("/events")
async def sse_events(request: Request):
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
    loop = asyncio.get_running_loop()
    with _sse_lock:
        _sse_subscribers.append((loop, queue))

    async def event_generator():
        try:
            # Send initial heartbeat
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    yield data
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                _sse_subscribers[:] = [
                    s for s in _sse_subscribers if s[1] is not queue
                ]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Dashboard ---

@http_app.get("/ui", response_class=HTMLResponse)
def serve_dashboard():
    dashboard_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(dashboard_path.read_text())


# --- Health ---

@http_app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


# --- WebSocket (Synchronous Request-Response) ---

@http_app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for synchronous request-response communication.

    Supports:
    - registration: {type: "register", name: str, role: str}
    - request: {type: "request", correlation_id: str, recipient: str, content: str, thread: str}
    - response: {type: "response", correlation_id: str, content: str, status: "success"|"error"}
    - message: {type: "message"|"broadcast", sender: str, content: str, thread: str}
    - heartbeat: {type: "heartbeat", status: str, working_on: str}
    """
    await websocket.accept()
    manager = get_ws_manager()
    agent_name: str | None = None

    try:
        # Wait for registration
        data = await websocket.receive_text()
        msg = json.loads(data)

        if msg.get("type") != "register":
            await websocket.send_json({"error": "Must register first"})
            await websocket.close()
            return

        agent_name = msg.get("name")
        role = msg.get("role", "")

        if not agent_name:
            await websocket.send_json({"error": "Missing agent name"})
            await websocket.close()
            return

        # Register agent
        get_store().register_agent(agent_name, role)
        conn = await manager.register_connection(agent_name, websocket)

        await websocket.send_json({
            "type": "registered",
            "agent_id": agent_name,
            "timestamp": str(time.time())
        })

        # Handle incoming messages
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")

            if msg_type == "request":
                # Request to another agent
                recipient = message.get("recipient")
                content = message.get("content", "")
                correlation_id = message.get("correlation_id")

                try:
                    # Store in message history
                    stored = get_store().add_message(
                        sender=agent_name,
                        content=content,
                        recipient=recipient,
                        thread=message.get("thread", "general"),
                        msg_type="request",
                        correlation_id=correlation_id,
                    )

                    # Send via WebSocket manager for real-time delivery
                    await manager.send_to_agent(recipient, {
                        "type": "request",
                        "correlation_id": correlation_id,
                        "sender": agent_name,
                        "content": content,
                        "thread": message.get("thread", "general"),
                    })
                except ValueError as e:
                    # Recipient not connected, send error response
                    await websocket.send_json({
                        "type": "response",
                        "correlation_id": correlation_id,
                        "content": str(e),
                        "status": "error",
                    })

            elif msg_type == "response":
                # Response to a request
                correlation_id = message.get("correlation_id")
                content = message.get("content", "")

                # Store in message history
                get_store().add_message(
                    sender=agent_name,
                    content=content,
                    thread=message.get("thread", "general"),
                    msg_type="response",
                    correlation_id=correlation_id,
                )

                # Route back to requester
                await manager.handle_response(correlation_id, {
                    "content": content,
                    "status": message.get("status", "success"),
                })

            elif msg_type in ("message", "broadcast"):
                # Regular message
                stored = get_store().add_message(
                    sender=agent_name,
                    content=message.get("content", ""),
                    recipient=message.get("recipient"),
                    thread=message.get("thread", "general"),
                    msg_type=message.get("msg_type", "chat"),
                )
                _broadcast_sse("message", stored.model_dump(mode="json"))

            elif msg_type == "heartbeat":
                # Heartbeat for keep-alive
                get_store().heartbeat(
                    agent_name,
                    status=message.get("status", "online"),
                    working_on=message.get("working_on", ""),
                )

    except WebSocketDisconnect:
        if agent_name:
            await manager.unregister_connection(agent_name)
            logger.info(f"Agent {agent_name} disconnected")

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from {agent_name}: {e}")

    except Exception as e:
        logger.error(f"WebSocket error for {agent_name}: {e}")
        if agent_name:
            await manager.unregister_connection(agent_name)


# ── MCP Server (FastMCP) ───────────────────────────────────────────

mcp = FastMCP("AgentBridge", instructions="Inter-agent communication server. Use these tools to send and receive messages with other AI agents working on the same codebase.")


@mcp.tool()
def register(name: str, role: str = "") -> str:
    """Register this agent with the bridge. Call this first with a unique name and optional role description."""
    agent = get_store().register_agent(name, role)
    _broadcast_sse("agent_joined", agent.model_dump(mode="json"))
    return f"Registered as '{agent.name}'" + (f" ({agent.role})" if agent.role else "")


@mcp.tool()
def send(sender: str, content: str, recipient: str | None = None, thread: str = "general", msg_type: str = "chat", artifacts: list[dict] | None = None) -> str:
    """Send a message. Set recipient to target a specific agent, or leave empty to broadcast. msg_type can be: chat, request, status, alert. artifacts is a list of {type, content} objects."""
    if msg_type not in MESSAGE_TYPES:
        raise ValueError(f"Invalid msg_type '{msg_type}'. Valid: {', '.join(MESSAGE_TYPES)}")
    msg = get_store().add_message(
        sender=sender, content=content, recipient=recipient,
        thread=thread, msg_type=msg_type, artifacts=artifacts,
    )
    _broadcast_sse("message", msg.model_dump(mode="json"))
    target = f" → {msg.recipient}" if msg.recipient else " (broadcast)"
    mentions = f" [mentions: {', '.join(msg.mentions)}]" if msg.mentions else ""
    return f"[{msg.msg_type}] Sent to [{msg.thread}]{target}: {msg.content}{mentions}"


@mcp.tool()
def read(thread: str | None = None, sender: str | None = None, as_agent: str | None = None, limit: int = 20) -> str:
    """Read recent messages. Optionally filter by thread or sender. Use as_agent to see only your inbox (messages to you + broadcasts)."""
    messages = get_store().read_messages(thread=thread, sender=sender, as_agent=as_agent, limit=limit)
    if not messages:
        return "No messages."
    lines = []
    for m in messages:
        prefix = f"[{m.thread}] " if m.thread != "general" else ""
        to = f" → {m.recipient}" if m.recipient else ""
        type_tag = f"[{m.msg_type}] " if m.msg_type != "chat" else ""
        artifact_tag = f" 📎{len(m.artifacts)}" if m.artifacts else ""
        lines.append(f"{type_tag}{prefix}{m.sender}{to}: {m.content}{artifact_tag}")
    return "\n".join(lines)


@mcp.tool()
def agents() -> str:
    """List all connected agents."""
    agent_list = get_store().list_agents()
    if not agent_list:
        return "No agents connected."
    lines = []
    for a in agent_list:
        parts = [f"  {a.name}"]
        if a.role:
            parts.append(f"({a.role})")
        parts.append(f"[{a.status}]")
        if a.working_on:
            parts.append(f"— {a.working_on}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


@mcp.tool()
def heartbeat(name: str, status: str = "online", working_on: str = "") -> str:
    """Update agent status. status: online|busy|idle. working_on: brief description of current task."""
    if status not in {"online", "busy", "idle"}:
        raise ValueError("status must be one of: online, busy, idle")
    agent = get_store().heartbeat(name, status=status, working_on=working_on)
    if not agent:
        return f"Agent '{name}' not found. Register first."
    _broadcast_sse("agent_status", agent.model_dump(mode="json"))
    return f"{agent.name} [{agent.status}]" + (f" — {agent.working_on}" if agent.working_on else "")


@mcp.tool()
def threads() -> str:
    """List active threads."""
    thread_list = get_store().list_threads()
    if not thread_list:
        return "No threads."
    return "\n".join(f"  #{t.name} (by {t.created_by})" for t in thread_list)


@mcp.tool()
def create_thread(name: str, created_by: str) -> str:
    """Create a named discussion thread."""
    t = get_store().create_thread(name, created_by)
    _broadcast_sse("thread_created", t.model_dump(mode="json"))
    return f"Created thread: #{t.name}"


@mcp.tool()
def thread_summary(name: str) -> str:
    """Get a summary of a thread: message count, participants, last activity."""
    s = get_store().thread_summary(name)
    if s.message_count == 0:
        return f"Thread '{name}': no messages."
    last = s.last_message_at.strftime("%H:%M") if s.last_message_at else "?"
    return (
        f"#{s.name}: {s.message_count} messages, "
        f"{len(s.participants)} participants ({', '.join(s.participants)}), "
        f"last activity {last}\n"
        f"Last: {s.last_message_preview}"
    )


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
