"""AgentBridge server — MCP tools (stdio) + HTTP API (FastAPI) + SSE + Web Dashboard."""

import asyncio
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .models import MESSAGE_TYPES, Message
from .schema_registry import SchemaRegistry, SchemaValidationError
from .store import MessageStore
from .ws_manager import get_ws_manager

# Shared store (lazy initialized to avoid import-time DB side effects)
store: MessageStore | None = None
_store_lock = threading.Lock()

# Reference to the uvicorn event loop — captured at startup so MCP tools
# (which run synchronously) can submit coroutines to the async WS manager.
_uvicorn_loop: asyncio.AbstractEventLoop | None = None


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


def _try_ws_deliver(recipient: str, message: dict):
    """Best-effort real-time delivery to a WS-connected agent (non-blocking).

    Called from sync HTTP handlers. Uses the captured uvicorn loop.
    Silently skips if the recipient isn't WS-connected.
    """
    if _uvicorn_loop is None or not _uvicorn_loop.is_running():
        return
    manager = get_ws_manager()
    if not manager.is_connected(recipient):
        return
    try:
        asyncio.run_coroutine_threadsafe(
            manager.send_to_agent(recipient, message),
            _uvicorn_loop,
        ).result(timeout=2)
    except Exception:
        pass  # WS delivery is best-effort; store already persisted


def _try_ws_broadcast(message: dict, exclude: str | None = None):
    """Best-effort broadcast to all WS-connected agents (non-blocking)."""
    if _uvicorn_loop is None or not _uvicorn_loop.is_running():
        return
    manager = get_ws_manager()
    try:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(message, exclude=exclude),
            _uvicorn_loop,
        ).result(timeout=2)
    except Exception:
        pass


# ── HTTP API (FastAPI) ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _uvicorn_loop
    _uvicorn_loop = asyncio.get_running_loop()
    get_store()
    yield
    global store
    with _store_lock:
        if store is not None:
            store.close()
            store = None


http_app = FastAPI(title="AgentBridge", version="0.3.0", lifespan=lifespan)


@http_app.middleware("http")
async def auth_and_logging_middleware(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    # /ui and static assets are always public — the dashboard JS handles auth client-side
    _public = {"/health", "/ui", "/favicon.ico"}
    if _auth_token and path not in _public:
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
    capabilities: list[str] = Field(default_factory=list)


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
    msg_type: Literal["chat", "request", "response", "status", "alert"] = "chat"
    artifacts: list[ArtifactRequest] | None = None
    correlation_id: str | None = None


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


class ClearBoardRequest(BaseModel):
    include_threads: bool = True


class CursorRequest(BaseModel):
    agent_name: str = Field(min_length=1, max_length=128)
    thread: str = Field(default="general", min_length=1, max_length=128)
    last_message_id: str = Field(min_length=1)
    last_timestamp: str | None = None


# --- Agents ---

@http_app.post("/agents")
async def http_register_agent(body: RegisterAgentRequest):
    agent = await get_store().register_agent_async(body.name, body.role, body.capabilities)
    result = agent.model_dump(mode="json")
    _broadcast_sse("agent_joined", result)
    return result


@http_app.get("/agents")
async def http_list_agents():
    agents = await get_store().list_agents_async()
    return [a.model_dump(mode="json") for a in agents]


@http_app.get("/actors")
async def http_list_actors():
    actors = []
    agent_rows = await get_store().list_agents_async()
    for a in agent_rows:
        data = a.model_dump(mode="json")
        actors.append({
            "id": data["name"],
            "type": "agent",
            "status": data.get("status", "online"),
            "role": data.get("role", ""),
            "capabilities": data.get("capabilities", []),
            "working_on": data.get("working_on", ""),
            "last_seen": data.get("last_seen"),
        })
    return actors


@http_app.delete("/agents/{name}")
async def http_kick_agent(name: str):
    """Remove an agent from the registry and drop their WS connection if active."""
    manager = get_ws_manager()
    # Drop WS connection if live
    if _uvicorn_loop and _uvicorn_loop.is_running() and manager.is_connected(name):
        try:
            asyncio.run_coroutine_threadsafe(
                manager.unregister_connection(name),
                _uvicorn_loop,
            ).result(timeout=2)
        except Exception:
            pass
    # Remove from DB
    await get_store().remove_agent_async(name)
    result = {"kicked": name}
    _broadcast_sse("agent_kicked", {"name": name})
    return result


@http_app.post("/agents/{name}/heartbeat")
async def http_heartbeat(name: str, body: HeartbeatRequest):
    agent = await get_store().heartbeat_async(name, status=body.status, working_on=body.working_on)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    result = agent.model_dump(mode="json")
    _broadcast_sse("agent_status", result)
    return result


# --- Messages ---

@http_app.post("/messages")
async def http_send_message(body: SendMessageRequest):
    correlation_id = body.correlation_id
    if body.msg_type == "request" and not correlation_id:
        correlation_id = str(uuid.uuid4())
    msg = await get_store().add_message_async(
        sender=body.sender,
        content=body.content,
        recipient=body.recipient,
        thread=body.thread,
        msg_type=body.msg_type,
        artifacts=[a.model_dump() for a in body.artifacts] if body.artifacts else None,
        correlation_id=correlation_id,
    )
    result = msg.model_dump(mode="json")
    _broadcast_sse("message", result)

    # Real-time WS delivery to agents
    if body.msg_type == "request":
        request_msg = {
            "type": "request",
            "correlation_id": correlation_id,
            "sender": body.sender,
            "content": body.content,
            "thread": body.thread,
        }
        if body.recipient:
            _try_ws_deliver(body.recipient, request_msg)
        else:
            _try_ws_broadcast(request_msg, exclude=body.sender)
    else:
        message_msg = {"type": "message", **result}
        if body.recipient:
            _try_ws_deliver(body.recipient, message_msg)
        else:
            _try_ws_broadcast(message_msg, exclude=body.sender)

    # If this is a response, resolve any pending WS future
    if body.msg_type == "response" and body.correlation_id and _uvicorn_loop:
        try:
            asyncio.run_coroutine_threadsafe(
                get_ws_manager().handle_response(body.correlation_id, {
                    "content": body.content,
                    "status": "success",
                }),
                _uvicorn_loop,
            )
        except Exception:
            pass

    return result


@http_app.get("/messages")
async def http_read_messages(
    thread: str | None = Query(None),
    sender: str | None = Query(None),
    as_agent: str | None = Query(None),
    since: str | None = Query(None),
    before: str | None = Query(None),
    correlation_id: str | None = Query(None),
    since_id: str | None = Query(None, alias="since_id"),
    limit: int = Query(50, ge=1, le=500),
):
    messages = await get_store().read_messages_async(
        thread=thread, since=since, before=before, limit=limit,
        sender=sender, as_agent=as_agent, correlation_id=correlation_id,
        since_id=since_id,
    )
    return [m.model_dump(mode="json") for m in messages]


@http_app.post("/bus/events")
async def http_send_event(body: EventWriteRequest):
    try:
        schema_registry.validate(body.event_type, body.metadata)
    except SchemaValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    msg = await get_store().add_message_async(
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
    if body.target_id:
        _try_ws_deliver(body.target_id, {"type": "event", **result})
    return result


@http_app.get("/bus/events")
async def http_read_events(
    thread: str | None = Query(None),
    actor_id: str | None = Query(None),
    target_id: str | None = Query(None),
    event_type: str | None = Query(None),
    since: str | None = Query(None),
    before: str | None = Query(None),
    since_id: str | None = Query(None, alias="since_id"),
    limit: int = Query(50, ge=1, le=500),
):
    messages = await get_store().read_messages_async(
        thread=thread, actor_id=actor_id, target_id=target_id,
        event_type=event_type, since=since, before=before, limit=limit,
        since_id=since_id,
    )
    return [m.model_dump(mode="json") for m in messages]


@http_app.post("/admin/clear")
async def http_clear_board(body: ClearBoardRequest | None = None):
    payload = body or ClearBoardRequest()
    await get_store()._run_in_thread(get_store().clear_board, payload.include_threads)
    _broadcast_sse("system", {"type": "clear", "include_threads": payload.include_threads})
    return {"cleared": True, "include_threads": payload.include_threads}


@http_app.get("/cursors")
async def http_list_cursors(agent: str | None = Query(None)):
    cursors = await get_store().list_delivery_cursors_async(agent)
    return cursors


@http_app.post("/cursors")
async def http_set_cursor(body: CursorRequest):
    timestamp = body.last_timestamp
    if not timestamp:
        timestamp = await get_store().message_timestamp_async(body.last_message_id)
        if not timestamp:
            raise HTTPException(status_code=404, detail="message not found")
    await get_store().set_delivery_cursor_async(
        body.agent_name,
        body.thread,
        body.last_message_id,
        timestamp,
    )
    return {"ok": True}


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
async def http_create_thread(body: CreateThreadRequest):
    t = await get_store().create_thread_async(body.name, body.created_by)
    result = t.model_dump(mode="json")
    _broadcast_sse("thread_created", result)
    return result


@http_app.get("/threads")
async def http_list_threads():
    threads = await get_store().list_threads_async()
    return [t.model_dump(mode="json") for t in threads]


@http_app.get("/threads/{name}/summary")
async def http_thread_summary(name: str):
    summary = await get_store().thread_summary_async(name)
    return summary.model_dump(mode="json")


# --- SSE ---

@http_app.get("/events")
async def sse_events(
    request: Request,
    since_id: str | None = Query(None, alias="since_id"),
    thread: str | None = Query(None),
    cursor_agent: str | None = Query(None, alias="cursor_agent"),
    cursor_thread: str | None = Query(None, alias="cursor_thread"),
    backlog_limit: int = Query(200, ge=10, le=1000),
):
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
    loop = asyncio.get_running_loop()
    with _sse_lock:
        _sse_subscribers.append((loop, queue))

    thread_filter = thread
    cursor_thread_value = cursor_thread or thread_filter or "general"
    since_timestamp: str | None = None
    if since_id:
        since_timestamp = await get_store().message_timestamp_async(since_id)
    elif cursor_agent:
        cursor = await get_store().get_delivery_cursor_async(cursor_agent, cursor_thread_value)
        if cursor and cursor.get("last_timestamp"):
            since_timestamp = cursor["last_timestamp"]

    backlog: list[Message] = []
    if since_timestamp:
        backlog = await get_store().read_messages_async(
            thread=thread_filter, since=since_timestamp, limit=backlog_limit,
        )

    def _build_sse(event_name: str, payload: dict) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"

    async def _update_cursor(payload: dict, payload_thread: str | None = None):
        if not cursor_agent:
            return
        message_id = payload.get("id")
        timestamp = payload.get("timestamp")
        if not message_id or not timestamp:
            return
        thread_name = cursor_thread_value or payload_thread or "general"
        await get_store().set_delivery_cursor_async(cursor_agent, thread_name, message_id, timestamp)

    def _parse_event(data: str) -> tuple[str | None, dict | None]:
        event_name: str | None = None
        payload: dict | None = None
        for line in data.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                raw = line[len("data: "):]
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = None
        return event_name, payload

    async def event_generator():
        try:
            yield "event: connected\ndata: {}\n\n"
            for msg in backlog:
                payload = msg.model_dump(mode="json")
                if thread_filter and payload.get("thread") != thread_filter:
                    continue
                await _update_cursor(payload, msg.thread)
                yield _build_sse("message", payload)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    event_name, payload = _parse_event(data)
                    if thread_filter and payload and payload.get("thread") != thread_filter:
                        continue
                    if payload:
                        await _update_cursor(payload, payload.get("thread"))
                    yield data
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                _sse_subscribers[:] = [s for s in _sse_subscribers if s[1] is not queue]

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
    return {"status": "ok", "version": "0.3.0"}


# --- WebSocket ---

@http_app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for synchronous request-response communication."""
    await websocket.accept()
    manager = get_ws_manager()
    agent_name: str | None = None

    try:
        data = await websocket.receive_text()
        msg = json.loads(data)

        if msg.get("type") != "register":
            await websocket.send_json({"error": "Must register first"})
            await websocket.close()
            return

        agent_name = msg.get("name")
        role = msg.get("role", "")
        capabilities = msg.get("capabilities", [])

        if not agent_name:
            await websocket.send_json({"error": "Missing agent name"})
            await websocket.close()
            return

        get_store().register_agent(agent_name, role, capabilities)
        conn = await manager.register_connection(agent_name, websocket)

        await websocket.send_json({
            "type": "registered",
            "agent_id": agent_name,
            "timestamp": str(time.time()),
        })

        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")

            if msg_type == "request":
                recipient = message.get("recipient")
                content = message.get("content", "")
                correlation_id = message.get("correlation_id")

                stored = get_store().add_message(
                    sender=agent_name,
                    content=content,
                    recipient=recipient,
                    thread=message.get("thread", "general"),
                    msg_type="request",
                    correlation_id=correlation_id,
                )
                _broadcast_sse("message", stored.model_dump(mode="json"))

                try:
                    await manager.send_to_agent(recipient, {
                        "type": "request",
                        "correlation_id": correlation_id,
                        "sender": agent_name,
                        "content": content,
                        "thread": message.get("thread", "general"),
                    })
                except ValueError as e:
                    await websocket.send_json({
                        "type": "response",
                        "correlation_id": correlation_id,
                        "content": str(e),
                        "status": "error",
                    })

            elif msg_type == "response":
                correlation_id = message.get("correlation_id")
                content = message.get("content", "")

                get_store().add_message(
                    sender=agent_name,
                    content=content,
                    thread=message.get("thread", "general"),
                    msg_type="response",
                    correlation_id=correlation_id,
                )

                await manager.handle_response(correlation_id, {
                    "content": content,
                    "status": message.get("status", "success"),
                })

            elif msg_type in ("message", "broadcast"):
                stored = get_store().add_message(
                    sender=agent_name,
                    content=message.get("content", ""),
                    recipient=message.get("recipient"),
                    thread=message.get("thread", "general"),
                    msg_type=message.get("msg_type", "chat"),
                )
                _broadcast_sse("message", stored.model_dump(mode="json"))
                # Deliver to recipient's WS connection if present
                if message.get("recipient"):
                    try:
                        await manager.send_to_agent(message["recipient"], {
                            "type": "message",
                            **stored.model_dump(mode="json"),
                        })
                    except ValueError:
                        pass

            elif msg_type == "heartbeat":
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

mcp = FastMCP(
    "AgentBridge",
    instructions=(
        "Inter-agent communication server. Use these tools to send and receive messages "
        "with other AI agents. Use request_agent() for synchronous calls where you need "
        "an answer before continuing."
    ),
)


@mcp.tool()
def register(name: str, role: str = "", capabilities: list[str] | None = None) -> str:
    """Register this agent. Call first with a unique name, optional role, and optional
    capabilities list (e.g. ['code-review', 'testing', 'deployment'])."""
    agent = get_store().register_agent(name, role, capabilities)
    _broadcast_sse("agent_joined", agent.model_dump(mode="json"))
    cap_str = f" capabilities=[{', '.join(agent.capabilities)}]" if agent.capabilities else ""
    return f"Registered as '{agent.name}'" + (f" ({agent.role})" if agent.role else "") + cap_str


@mcp.tool()
def send(
    sender: str, content: str, recipient: str | None = None,
    thread: str = "general", msg_type: str = "chat",
    artifacts: list[dict] | None = None,
) -> str:
    """Send a fire-and-forget message. For synchronous calls where you need a reply,
    use request_agent() instead. msg_type: chat, request, status, alert."""
    if msg_type not in MESSAGE_TYPES:
        raise ValueError(f"Invalid msg_type '{msg_type}'. Valid: {', '.join(MESSAGE_TYPES)}")
    msg = get_store().add_message(
        sender=sender, content=content, recipient=recipient,
        thread=thread, msg_type=msg_type, artifacts=artifacts,
    )
    _broadcast_sse("message", msg.model_dump(mode="json"))
    # Real-time WS delivery
    if recipient:
        _try_ws_deliver(recipient, {"type": "message", **msg.model_dump(mode="json")})
    target = f" → {msg.recipient}" if msg.recipient else " (broadcast)"
    mentions = f" [mentions: {', '.join(msg.mentions)}]" if msg.mentions else ""
    return f"[{msg.msg_type}] Sent to [{msg.thread}]{target}: {msg.content}{mentions}"


@mcp.tool()
def request_agent(
    sender: str,
    recipient: str,
    content: str,
    timeout_sec: float = 60.0,
    thread: str = "general",
) -> str:
    """Send a request to another agent and WAIT synchronously for their response.

    Blocks until the recipient calls respond() with the matching correlation_id,
    or until timeout_sec elapses. Use this when you need an answer before continuing.

    Returns the recipient's response content, or raises TimeoutError.
    """
    correlation_id = str(uuid.uuid4())

    # Persist request so recipient can see it even if not WS-connected
    get_store().add_message(
        sender=sender,
        recipient=recipient,
        content=content,
        thread=thread,
        msg_type="request",
        correlation_id=correlation_id,
    )
    _broadcast_sse("message", {"sender": sender, "recipient": recipient, "content": content,
                                "msg_type": "request", "correlation_id": correlation_id})

    # Best-effort real-time delivery via WS
    _try_ws_deliver(recipient, {
        "type": "request",
        "correlation_id": correlation_id,
        "sender": sender,
        "content": content,
        "thread": thread,
    })

    # Poll DB for response with this correlation_id
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        responses = get_store().read_messages(correlation_id=correlation_id, limit=10)
        response = next((m for m in responses if m.msg_type == "response"), None)
        if response:
            return response.content
        time.sleep(0.4)

    raise TimeoutError(
        f"No response from '{recipient}' within {timeout_sec}s "
        f"(correlation_id={correlation_id})"
    )


@mcp.tool()
def wait_for_request(agent_name: str, timeout_sec: float = 30.0) -> str:
    """Block until an incoming request arrives for this agent.

    Returns a JSON object with: correlation_id, sender, content, thread, timestamp.
    Use the correlation_id with respond() to reply.
    Returns a timeout status dict if nothing arrives within timeout_sec.
    """
    start_ts = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        msgs = get_store().read_messages(as_agent=agent_name, since=start_ts, limit=20)
        for m in msgs:
            if m.msg_type == "request" and m.correlation_id:
                return json.dumps({
                    "correlation_id": m.correlation_id,
                    "sender": m.sender,
                    "content": m.content,
                    "thread": m.thread,
                    "timestamp": m.timestamp.isoformat(),
                })
        time.sleep(0.4)

    return json.dumps({"status": "timeout", "message": f"No requests within {timeout_sec}s"})


@mcp.tool()
def respond(
    agent_name: str,
    correlation_id: str,
    content: str,
    status: str = "success",
) -> str:
    """Send a response to a specific request identified by correlation_id.

    The agent that called request_agent() will unblock and receive this content.
    status: 'success' or 'error'.
    """
    # Look up the original request to route the response back
    originals = get_store().read_messages(correlation_id=correlation_id, limit=10)
    original = next((m for m in originals if m.msg_type == "request"), None)
    requester = original.sender if original else None
    thread = original.thread if original else "general"

    get_store().add_message(
        sender=agent_name,
        recipient=requester,
        content=content,
        thread=thread,
        msg_type="response",
        correlation_id=correlation_id,
    )

    # Notify WS manager so any awaiting future resolves immediately
    if _uvicorn_loop and _uvicorn_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                get_ws_manager().handle_response(correlation_id, {
                    "content": content,
                    "status": status,
                }),
                _uvicorn_loop,
            ).result(timeout=1)
        except Exception:
            pass

    # Best-effort real-time delivery to the requester
    if requester:
        _try_ws_deliver(requester, {
            "type": "response",
            "correlation_id": correlation_id,
            "content": content,
            "status": status,
        })

    return f"Response sent (correlation_id={correlation_id}, to={requester or 'unknown'})"


@mcp.tool()
def kick_agent(name: str) -> str:
    """Remove an agent from the registry and terminate their connection.

    Use when an agent is stale, misbehaving, or needs to be restarted.
    """
    manager = get_ws_manager()
    if _uvicorn_loop and _uvicorn_loop.is_running() and manager.is_connected(name):
        try:
            asyncio.run_coroutine_threadsafe(
                manager.unregister_connection(name),
                _uvicorn_loop,
            ).result(timeout=2)
        except Exception:
            pass
    get_store().remove_agent(name)
    _broadcast_sse("agent_kicked", {"name": name})
    return f"Agent '{name}' kicked and removed from registry."


@mcp.tool()
def find_agent(capability: str) -> str:
    """Find agents that advertise a given capability.

    Returns a list of agent names and roles that can handle the capability.
    Example: find_agent('code-review') → agents that registered with that capability.
    """
    matches = get_store().find_agents_by_capability(capability)
    if not matches:
        return f"No agents found with capability '{capability}'."
    lines = []
    for a in matches:
        parts = [f"  {a.name}"]
        if a.role:
            parts.append(f"({a.role})")
        parts.append(f"[{a.status}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


@mcp.tool()
def read(
    thread: str | None = None,
    sender: str | None = None,
    as_agent: str | None = None,
    limit: int = 20,
    since_id: str | None = None,
    cursor_agent: str | None = None,
    cursor_thread: str = "general",
    save_cursor: bool = False,
) -> str:
    """Read recent messages. Filter by thread or sender. Use as_agent to see your inbox.

    Optional parameters:
    - since_id: only return messages after this ID.
    - cursor_agent/cursor_thread + save_cursor: persist the last message ID for the given agent/thread so you can resume later.
    """
    messages = get_store().read_messages(
        thread=thread,
        sender=sender,
        as_agent=as_agent,
        limit=limit,
        since_id=since_id,
    )
    if not messages:
        return "No messages."
    lines = []
    for m in messages:
        prefix = f"[{m.thread}] " if m.thread != "general" else ""
        to = f" → {m.recipient}" if m.recipient else ""
        type_tag = f"[{m.msg_type}] " if m.msg_type != "chat" else ""
        artifact_tag = f" [{len(m.artifacts)} artifacts]" if m.artifacts else ""
        corr_tag = f" (cid={m.correlation_id[:8]})" if m.correlation_id else ""
        lines.append(f"{type_tag}{prefix}{m.sender}{to}: {m.content}{artifact_tag}{corr_tag}")
    if save_cursor:
        if not cursor_agent:
            raise ValueError("save_cursor requires cursor_agent")
        last = messages[-1]
        thread_name = cursor_thread or thread or last.thread
        timestamp = last.timestamp.isoformat()
        get_store().set_delivery_cursor(cursor_agent, thread_name, last.id, timestamp)
    return "\n".join(lines)


@mcp.tool()
def list_cursors(agent: str | None = None) -> str:
    """List persisted delivery cursors."""
    cursors = get_store().list_delivery_cursors(agent)
    if not cursors:
        return "No cursors recorded."
    lines = []
    for c in cursors:
        lines.append(f"{c['agent_name']}#{c['thread']}: last={c['last_message_id'] or '<none>'} @ {c['last_timestamp'] or 'unknown'}")
    return "\n".join(lines)


@mcp.tool()
def get_cursor(agent: str, thread: str = "general") -> str:
    """Get a single delivery cursor for an agent/thread."""
    cursor = get_store().get_delivery_cursor(agent, thread)
    if not cursor:
        return f"No cursor for {agent}#{thread}"
    return f"{agent}#{thread}: {cursor['last_message_id']} @ {cursor['last_timestamp']} (updated {cursor['updated_at']})"


@mcp.tool()
def set_cursor(agent: str, thread: str, last_message_id: str, timestamp: str | None = None) -> str:
    """Persist a delivery cursor."""
    ts = timestamp or get_store().message_timestamp(last_message_id)
    if not ts:
        raise ValueError("message_id not found")
    get_store().set_delivery_cursor(agent, thread, last_message_id, ts)
    return f"Cursor set for {agent}#{thread} -> {last_message_id} @ {ts}"


@mcp.tool()
def agents() -> str:
    """List all connected agents with their capabilities."""
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
        if a.capabilities:
            parts.append(f"caps=[{', '.join(a.capabilities)}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


@mcp.tool()
def heartbeat(name: str, status: str = "online", working_on: str = "") -> str:
    """Update agent status. status: online|busy|idle."""
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
    uvicorn.run(http_app, host="0.0.0.0", port=7890, log_level="warning")


def main():
    """Start both MCP (stdio) and HTTP servers."""
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()
    mcp.run()


if __name__ == "__main__":
    main()
