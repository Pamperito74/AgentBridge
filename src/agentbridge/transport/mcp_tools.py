"""All MCP tool definitions extracted from server.py."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from ..deps import get_store, get_uvicorn_loop, get_ws_manager
from ..models import MESSAGE_TYPES
from ..sse_helpers import broadcast_sse
from ..ws_helpers import notify_incoming_waiter, try_ws_deliver

mcp = FastMCP(
    "AgentBridge",
    instructions=(
        "Inter-agent communication server. Use these tools to send and receive messages "
        "with other AI agents. Use request_agent() for synchronous calls where you need "
        "an answer before continuing."
    ),
)


# ---------------------------------------------------------------------------
# Remote proxy helpers
# ---------------------------------------------------------------------------


def _remote_url() -> str | None:
    url = os.environ.get("AGENTBRIDGE_REMOTE_URL", "").strip().rstrip("/")
    return url if url else None


def _rhttp(
    method: str,
    path: str,
    body: dict | None = None,
    params: dict | None = None,
    timeout: float = 10.0,
) -> dict | list:
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode

    base = _remote_url()
    if not base:
        raise RuntimeError("AGENTBRIDGE_REMOTE_URL is not set")

    url = base + path
    if params:
        qs = urlencode({k: v for k, v in params.items() if v is not None})
        if qs:
            url += "?" + qs

    token = os.environ.get("AGENTBRIDGE_TOKEN", "")
    headers: dict[str, str] = {}
    if token:
        headers["X-AgentBridge-Token"] = token

    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    req = urllib.request.Request(
        url, data=data, headers=headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        raise RuntimeError(
            f"Remote {method.upper()} {path} → HTTP {e.code}: {body_text}"
        )
    except Exception as e:
        raise RuntimeError(f"Remote {method.upper()} {path} failed: {e}")


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def register(name: str, role: str = "", capabilities: list[str] | None = None) -> str:
    """Register this agent. Call first with a unique name, optional role, and optional
    capabilities list (e.g. ['code-review', 'testing', 'deployment'])."""
    if _remote_url():
        caps = capabilities or []
        _rhttp(
            "POST",
            "/agents",
            {"name": name, "role": role, "capabilities": caps, "agent_type": "bot"},
        )
        cap_str = f" capabilities=[{', '.join(caps)}]" if caps else ""
        return (
            f"Registered as '{name}'"
            + (f" ({role})" if role else "")
            + cap_str
        )
    existing = get_store().get_agent(name)
    agent = get_store().register_agent(name, role, capabilities)
    broadcast_sse("agent_joined", agent.model_dump(mode="json"))
    cap_str = (
        f" capabilities=[{', '.join(agent.capabilities)}]"
        if agent.capabilities
        else ""
    )
    result = (
        f"Registered as '{agent.name}'"
        + (f" ({agent.role})" if agent.role else "")
        + cap_str
    )
    if existing:
        result += (
            f"\nWARNING: Agent '{name}' was already registered — previous instance evicted. "
            "Use a unique name if running multiple instances."
        )
    return result


@mcp.tool()
def send(
    sender: str,
    content: str,
    recipient: str | None = None,
    thread: str = "general",
    msg_type: str = "chat",
    artifacts: list[dict] | None = None,
) -> str:
    """Send a fire-and-forget message. For synchronous calls where you need a reply,
    use request_agent() instead. msg_type: chat, request, status, alert."""
    if msg_type not in MESSAGE_TYPES:
        raise ValueError(
            f"Invalid msg_type '{msg_type}'. Valid: {', '.join(MESSAGE_TYPES)}"
        )
    if _remote_url():
        body: dict = {
            "sender": sender,
            "content": content,
            "thread": thread,
            "msg_type": msg_type,
        }
        if recipient:
            body["recipient"] = recipient
        if artifacts:
            body["artifacts"] = artifacts
        msg_d = _rhttp("POST", "/messages", body)
        target = (
            f" → {msg_d.get('recipient')}" if msg_d.get("recipient") else " (broadcast)"
        )
        return f"[{msg_type}] Sent to [{thread}]{target}: {content}"
    msg = get_store().add_message(
        sender=sender,
        content=content,
        recipient=recipient,
        thread=thread,
        msg_type=msg_type,
        artifacts=artifacts,
    )
    broadcast_sse("message", msg.model_dump(mode="json"))
    if msg_type == "request" and recipient:
        notify_incoming_waiter(recipient, msg.model_dump(mode="json"))
    if recipient:
        try_ws_deliver(recipient, {"type": "message", **msg.model_dump(mode="json")})
    target = f" → {msg.recipient}" if msg.recipient else " (broadcast)"
    mentions = (
        f" [mentions: {', '.join(msg.mentions)}]" if msg.mentions else ""
    )
    return f"[{msg.msg_type}] Sent to [{msg.thread}]{target}: {msg.content}{mentions}"


@mcp.tool()
def request_agent(
    sender: str,
    recipient: str,
    content: str,
    timeout_sec: float = 180.0,
    thread: str = "general",
) -> str:
    """Send a request to another agent and WAIT synchronously for their response.

    Blocks until the recipient calls respond() with the matching correlation_id,
    or until timeout_sec elapses. Use this when you need an answer before continuing.

    The request is persisted in the agent_requests queue — recipients can poll
    GET /agents/{name}/requests and respond via POST /agents/{name}/requests/{id}/respond
    regardless of transport (MCP, HTTP, WebSocket, curl, etc.).

    Returns the recipient's response content, or raises TimeoutError.
    """
    import uuid as _uuid

    if _remote_url():
        correlation_id = str(_uuid.uuid4())
        msg_d = _rhttp(
            "POST",
            "/messages",
            {
                "sender": sender,
                "recipient": recipient,
                "content": content,
                "thread": thread,
                "msg_type": "request",
                "correlation_id": correlation_id,
            },
        )
        since_id = msg_d.get("id")
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            params: dict = {"as_agent": sender, "limit": 50}
            if since_id:
                params["since_id"] = since_id
            msgs = _rhttp("GET", "/messages", params=params)
            for m in msgs if isinstance(msgs, list) else []:
                if (
                    m.get("msg_type") == "response"
                    and m.get("correlation_id") == correlation_id
                ):
                    return m["content"]
            time.sleep(0.5)
        raise TimeoutError(
            f"No response from '{recipient}' within {timeout_sec}s "
            f"(correlation_id={correlation_id})"
        )
    correlation_id = str(_uuid.uuid4())

    req_entry = get_store().create_agent_request(
        from_agent=sender,
        to_agent=recipient,
        content=content,
        timeout_sec=timeout_sec,
        thread=thread,
        correlation_id=correlation_id,
    )

    msg = get_store().add_message(
        sender=sender,
        recipient=recipient,
        content=content,
        thread=thread,
        msg_type="request",
        correlation_id=correlation_id,
    )
    broadcast_sse("message", msg.model_dump(mode="json"))
    broadcast_sse(
        "agent_request",
        {
            "request_id": req_entry["id"],
            "from_agent": sender,
            "to_agent": recipient,
            "correlation_id": correlation_id,
            "content": content,
            "expires_at": req_entry["expires_at"],
        },
    )

    notify_incoming_waiter(recipient, msg.model_dump(mode="json"))

    loop = get_uvicorn_loop()
    manager = get_ws_manager()
    if loop and loop.is_running() and manager.is_connected(recipient):
        try:
            result = asyncio.run_coroutine_threadsafe(
                manager.deliver_and_wait(
                    recipient,
                    {
                        "type": "request",
                        "correlation_id": correlation_id,
                        "request_id": req_entry["id"],
                        "sender": sender,
                        "content": content,
                        "thread": thread,
                    },
                    correlation_id,
                    timeout_sec,
                ),
                loop,
            ).result(timeout=timeout_sec + 2)
            return result["content"]
        except TimeoutError:
            raise TimeoutError(
                f"No response from '{recipient}' within {timeout_sec}s "
                f"(correlation_id={correlation_id}). "
                f"Recipient can still respond via GET /agents/{recipient}/requests"
            )
    else:
        try_ws_deliver(
            recipient,
            {
                "type": "request",
                "correlation_id": correlation_id,
                "request_id": req_entry["id"],
                "sender": sender,
                "content": content,
                "thread": thread,
            },
        )
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            row = get_store().get_agent_request_by_correlation(correlation_id)
            if row and row["status"] == "answered":
                return row["response"]
            if row and row["status"] == "timeout":
                break
            time.sleep(0.4)
        raise TimeoutError(
            f"No response from '{recipient}' within {timeout_sec}s "
            f"(correlation_id={correlation_id}). "
            f"Recipient can still respond via GET /agents/{recipient}/requests"
        )


@mcp.tool()
def wait_for_request(agent_name: str, timeout_sec: float = 30.0) -> str:
    """Block until an incoming request arrives for this agent.

    Returns a JSON object with: correlation_id, sender, content, thread, timestamp.
    Use the correlation_id with respond() to reply.
    Returns a timeout status dict if nothing arrives within timeout_sec.
    """
    if _remote_url():
        deadline = time.monotonic() + timeout_sec
        seen: set[str] = set()
        while time.monotonic() < deadline:
            msgs = _rhttp(
                "GET", "/messages", params={"as_agent": agent_name, "limit": 50}
            )
            for m in msgs if isinstance(msgs, list) else []:
                if (
                    m.get("msg_type") == "request"
                    and m.get("correlation_id")
                    and m["id"] not in seen
                ):
                    seen.add(m["id"])
                    return json.dumps(
                        {
                            "correlation_id": m["correlation_id"],
                            "sender": m["sender"],
                            "content": m["content"],
                            "thread": m.get("thread", "general"),
                            "timestamp": m.get("timestamp"),
                        }
                    )
            time.sleep(0.5)
        return json.dumps(
            {"status": "timeout", "message": f"No requests within {timeout_sec}s"}
        )

    start_ts = datetime.now(timezone.utc).isoformat()

    def _make_result_from_queue(r: dict) -> str:
        return json.dumps(
            {
                "correlation_id": r["correlation_id"],
                "request_id": r["id"],
                "sender": r["from_agent"],
                "content": r["content"],
                "thread": r["thread"],
                "timestamp": r["created_at"],
            }
        )

    queued = get_store().get_pending_requests(agent_name)
    if queued:
        return _make_result_from_queue(queued[0])

    def _make_result(m) -> str:
        return json.dumps(
            {
                "correlation_id": m.correlation_id,
                "sender": m.sender,
                "content": m.content,
                "thread": m.thread,
                "timestamp": m.timestamp.isoformat(),
            }
        )

    def _make_result_from_dict(d: dict) -> str:
        return json.dumps(
            {
                "correlation_id": d.get("correlation_id"),
                "sender": d.get("sender"),
                "content": d.get("content"),
                "thread": d.get("thread"),
                "timestamp": d.get("timestamp"),
            }
        )

    msgs = get_store().read_messages(as_agent=agent_name, since=start_ts, limit=20)
    for m in msgs:
        if m.msg_type == "request" and m.correlation_id:
            return _make_result(m)

    loop = get_uvicorn_loop()
    if loop and loop.is_running():
        manager = get_ws_manager()
        try:
            pending = asyncio.run_coroutine_threadsafe(
                manager.register_incoming_waiter(agent_name, timeout_sec),
                loop,
            ).result(timeout=2)
        except Exception:
            pending = None

        if pending is not None:
            msgs = get_store().read_messages(
                as_agent=agent_name, since=start_ts, limit=20
            )
            for m in msgs:
                if m.msg_type == "request" and m.correlation_id:
                    asyncio.run_coroutine_threadsafe(
                        manager.clear_incoming_waiter(agent_name), loop
                    )
                    return _make_result(m)

            try:
                result = asyncio.run_coroutine_threadsafe(
                    pending.wait(), loop
                ).result(timeout=timeout_sec + 2)
                return _make_result_from_dict(result)
            except (TimeoutError, Exception):
                pass
            finally:
                asyncio.run_coroutine_threadsafe(
                    manager.clear_incoming_waiter(agent_name), loop
                )

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        msgs = get_store().read_messages(as_agent=agent_name, since=start_ts, limit=20)
        for m in msgs:
            if m.msg_type == "request" and m.correlation_id:
                return _make_result(m)
        time.sleep(0.4)

    return json.dumps(
        {"status": "timeout", "message": f"No requests within {timeout_sec}s"}
    )


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
    if _remote_url():
        msgs = _rhttp("GET", "/messages", params={"limit": 50})
        original = next(
            (
                m
                for m in (msgs if isinstance(msgs, list) else [])
                if m.get("correlation_id") == correlation_id
                and m.get("msg_type") == "request"
            ),
            None,
        )
        requester = original["sender"] if original else None
        thread = original.get("thread", "general") if original else "general"
        _rhttp(
            "POST",
            "/messages",
            {
                "sender": agent_name,
                "recipient": requester,
                "content": content,
                "thread": thread,
                "msg_type": "response",
                "correlation_id": correlation_id,
            },
        )
        return f"Response sent (correlation_id={correlation_id}, to={requester or 'unknown'})"

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

    loop = get_uvicorn_loop()
    if loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                get_ws_manager().handle_response(
                    correlation_id, {"content": content, "status": status}
                ),
                loop,
            ).result(timeout=1)
        except Exception:
            pass

    if requester:
        try_ws_deliver(
            requester,
            {
                "type": "response",
                "correlation_id": correlation_id,
                "content": content,
                "status": status,
            },
        )

    return f"Response sent (correlation_id={correlation_id}, to={requester or 'unknown'})"


@mcp.tool()
def kick_agent(name: str) -> str:
    """Remove an agent from the registry and terminate their connection.

    Use when an agent is stale, misbehaving, or needs to be restarted.
    """
    if _remote_url():
        try:
            _rhttp("DELETE", f"/agents/{name}")
        except Exception:
            pass
        return f"Agent '{name}' kicked and removed from registry."
    loop = get_uvicorn_loop()
    manager = get_ws_manager()
    if loop and loop.is_running() and manager.is_connected(name):
        try:
            asyncio.run_coroutine_threadsafe(
                manager.unregister_connection(name), loop
            ).result(timeout=2)
        except Exception:
            pass
    get_store().remove_agent(name)
    broadcast_sse("agent_kicked", {"name": name})
    return f"Agent '{name}' kicked and removed from registry."


@mcp.tool()
def find_agent(capability: str) -> str:
    """Find agents that advertise a given capability.

    Returns a list of agent names and roles that can handle the capability.
    Example: find_agent('code-review') → agents that registered with that capability.
    """
    if _remote_url():
        all_agents = _rhttp("GET", "/agents")
        cap_lower = capability.lower()
        matches_d = [
            a
            for a in (all_agents if isinstance(all_agents, list) else [])
            if cap_lower in a.get("role", "").lower()
            or any(cap_lower in c.lower() for c in a.get("capabilities", []))
        ]
        if not matches_d:
            return f"No agents found with capability '{capability}'."
        return "\n".join(
            f"  {a['name']}"
            + (f" ({a['role']})" if a.get("role") else "")
            + f" [{a.get('status', '?')}]"
            for a in matches_d
        )
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
    if _remote_url():
        params: dict = {"limit": limit}
        if thread:
            params["thread"] = thread
        if sender:
            params["sender"] = sender
        if as_agent:
            params["as_agent"] = as_agent
        if since_id:
            params["since_id"] = since_id
        msgs = _rhttp("GET", "/messages", params=params)
        if not msgs:
            return "No messages."
        lines = []
        for m in msgs if isinstance(msgs, list) else []:
            prefix = (
                f"[{m['thread']}] "
                if m.get("thread") and m["thread"] != "general"
                else ""
            )
            to = f" → {m['recipient']}" if m.get("recipient") else ""
            type_tag = (
                f"[{m['msg_type']}] "
                if m.get("msg_type") and m["msg_type"] != "chat"
                else ""
            )
            artifacts_val = m.get("artifacts") or []
            artifact_tag = f" [{len(artifacts_val)} artifacts]" if artifacts_val else ""
            corr_tag = (
                f" (cid={m['correlation_id'][:8]})"
                if m.get("correlation_id")
                else ""
            )
            lines.append(
                f"{type_tag}{prefix}{m['sender']}{to}: {m['content']}{artifact_tag}{corr_tag}"
            )
        if save_cursor and cursor_agent and lines:
            last = (msgs if isinstance(msgs, list) else [])[-1]
            _rhttp(
                "POST",
                "/cursors",
                {
                    "agent": cursor_agent,
                    "thread": cursor_thread or thread or "general",
                    "last_message_id": last["id"],
                    "timestamp": last.get("timestamp"),
                },
            )
        return "\n".join(lines)
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
        lines.append(
            f"{type_tag}{prefix}{m.sender}{to}: {m.content}{artifact_tag}{corr_tag}"
        )
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
    if _remote_url():
        params = {"agent": agent} if agent else {}
        cursors = _rhttp("GET", "/cursors", params=params)
        if not cursors:
            return "No cursors recorded."
        return "\n".join(
            f"{c['agent_name']}#{c['thread']}: last={c.get('last_message_id') or '<none>'} @ {c.get('last_timestamp') or 'unknown'}"
            for c in (cursors if isinstance(cursors, list) else [])
        )
    cursors = get_store().list_delivery_cursors(agent)
    if not cursors:
        return "No cursors recorded."
    lines = []
    for c in cursors:
        lines.append(
            f"{c['agent_name']}#{c['thread']}: last={c['last_message_id'] or '<none>'} @ {c['last_timestamp'] or 'unknown'}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_cursor(agent: str, thread: str = "general") -> str:
    """Get a single delivery cursor for an agent/thread."""
    if _remote_url():
        cursors = _rhttp("GET", "/cursors", params={"agent": agent})
        cursor = next(
            (
                c
                for c in (cursors if isinstance(cursors, list) else [])
                if c.get("thread") == thread
            ),
            None,
        )
        if not cursor:
            return f"No cursor for {agent}#{thread}"
        return f"{agent}#{thread}: {cursor.get('last_message_id')} @ {cursor.get('last_timestamp')} (updated {cursor.get('updated_at')})"
    cursor = get_store().get_delivery_cursor(agent, thread)
    if not cursor:
        return f"No cursor for {agent}#{thread}"
    return f"{agent}#{thread}: {cursor['last_message_id']} @ {cursor['last_timestamp']} (updated {cursor['updated_at']})"


@mcp.tool()
def set_cursor(
    agent: str,
    thread: str,
    last_message_id: str,
    timestamp: str | None = None,
) -> str:
    """Persist a delivery cursor."""
    if _remote_url():
        _rhttp(
            "POST",
            "/cursors",
            {
                "agent": agent,
                "thread": thread,
                "last_message_id": last_message_id,
                "timestamp": timestamp,
            },
        )
        return f"Cursor set for {agent}#{thread} -> {last_message_id}"
    ts = timestamp or get_store().message_timestamp(last_message_id)
    if not ts:
        raise ValueError("message_id not found")
    get_store().set_delivery_cursor(agent, thread, last_message_id, ts)
    return f"Cursor set for {agent}#{thread} -> {last_message_id} @ {ts}"


@mcp.tool()
def agents() -> str:
    """List all connected agents with their capabilities."""
    if _remote_url():
        agent_list = _rhttp("GET", "/agents")
        if not agent_list:
            return "No agents connected."
        lines = []
        for a in agent_list if isinstance(agent_list, list) else []:
            parts = [f"  {a['name']}"]
            if a.get("role"):
                parts.append(f"({a['role']})")
            parts.append(f"[{a.get('status', '?')}]")
            if a.get("working_on"):
                parts.append(f"— {a['working_on']}")
            if a.get("capabilities"):
                parts.append(f"caps=[{', '.join(a['capabilities'])}]")
            lines.append(" ".join(parts))
        return "\n".join(lines)
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
    """Update agent status. status: online|busy|idle|needs_input.

    Use needs_input when blocked waiting for human direction or approval.
    This surfaces as a ⚠ indicator in the UI so humans know to intervene.
    """
    if status not in {"online", "busy", "idle", "needs_input"}:
        raise ValueError("status must be one of: online, busy, idle, needs_input")
    if _remote_url():
        a = _rhttp(
            "POST",
            f"/agents/{name}/heartbeat",
            {"status": status, "working_on": working_on},
        )
        return f"{a.get('name', name)} [{a.get('status', status)}]" + (
            f" — {a['working_on']}" if a.get("working_on") else ""
        )
    agent = get_store().heartbeat(name, status=status, working_on=working_on)
    if not agent:
        return f"Agent '{name}' not found. Register first."
    broadcast_sse("agent_status", agent.model_dump(mode="json"))
    return f"{agent.name} [{agent.status}]" + (
        f" — {agent.working_on}" if agent.working_on else ""
    )


@mcp.tool()
def threads() -> str:
    """List active threads."""
    if _remote_url():
        thread_list = _rhttp("GET", "/threads")
        if not thread_list:
            return "No threads."
        return "\n".join(
            f"  #{t['name']} (by {t.get('created_by', '?')})"
            for t in (thread_list if isinstance(thread_list, list) else [])
        )
    thread_list = get_store().list_threads()
    if not thread_list:
        return "No threads."
    return "\n".join(f"  #{t.name} (by {t.created_by})" for t in thread_list)


@mcp.tool()
def create_thread(name: str, created_by: str) -> str:
    """Create a named discussion thread."""
    if _remote_url():
        t = _rhttp("POST", "/threads", {"name": name, "created_by": created_by})
        return f"Created thread: #{t.get('name', name)}"
    t = get_store().create_thread(name, created_by)
    broadcast_sse("thread_created", t.model_dump(mode="json"))
    return f"Created thread: #{t.name}"


@mcp.tool()
def thread_summary(name: str) -> str:
    """Get a summary of a thread: message count, participants, last activity."""
    if _remote_url():
        s = _rhttp("GET", f"/threads/{name}/summary")
        if s.get("message_count", 0) == 0:
            return f"Thread '{name}': no messages."
        participants = s.get("participants", [])
        last = (
            s.get("last_message_at", "?")[:16] if s.get("last_message_at") else "?"
        )
        return (
            f"#{s.get('name', name)}: {s['message_count']} messages, "
            f"{len(participants)} participants ({', '.join(participants)}), "
            f"last activity {last}\n"
            f"Last: {s.get('last_message_preview', '')}"
        )
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


# ── MCP Memory Tools ────────────────────────────────────────────────


@mcp.tool()
def memory_set(agent_name: str, key: str, value: str) -> str:
    """Persist a key-value memory entry for this agent.

    Use to store facts, preferences, or context that should survive across
    conversation turns. Values are plain text (stringify JSON if needed).
    Example: memory_set('claude-ct1', 'preferred_language', 'TypeScript')
    """
    if _remote_url():
        _rhttp("PUT", f"/agents/{agent_name}/memory/{key}", {"value": value})
        return f"Memory set: {agent_name}[{key}]"
    get_store().memory_set(agent_name, key, value)
    return f"Memory set: {agent_name}[{key}]"


@mcp.tool()
def memory_get(agent_name: str, key: str) -> str:
    """Retrieve a stored memory entry for this agent.

    Returns the value string, or 'Key not found' if it doesn't exist.
    """
    if _remote_url():
        try:
            entry = _rhttp("GET", f"/agents/{agent_name}/memory/{key}")
            return (
                str(entry.get("value", ""))
                if isinstance(entry, dict)
                else "Key not found"
            )
        except Exception:
            return "Key not found"
    entry = get_store().memory_get(agent_name, key)
    if not entry:
        return "Key not found"
    return entry["value"]


@mcp.tool()
def memory_list(agent_name: str, search: str | None = None) -> str:
    """List all memory entries for this agent, optionally filtered by search query.

    Returns key=value pairs, one per line.
    """
    if _remote_url():
        params: dict = {}
        if search:
            params["q"] = search
        entries = _rhttp(
            "GET", f"/agents/{agent_name}/memory", params=params or None
        )
        if not entries:
            return "No memory entries."
        return "\n".join(
            f"  {e['key']} = {e['value'][:80]}{'…' if len(e.get('value', '')) > 80 else ''}"
            for e in (entries if isinstance(entries, list) else [])
        )
    if search:
        entries = get_store().memory_search(agent_name, search)
    else:
        entries = get_store().memory_list(agent_name)
    if not entries:
        return "No memory entries."
    return "\n".join(
        f"  {e['key']} = {e['value'][:80]}{'…' if len(e['value']) > 80 else ''}"
        for e in entries
    )


@mcp.tool()
def memory_delete(agent_name: str, key: str) -> str:
    """Delete a stored memory entry for this agent."""
    if _remote_url():
        try:
            _rhttp("DELETE", f"/agents/{agent_name}/memory/{key}")
        except Exception:
            return f"Key '{key}' not found for agent '{agent_name}'"
        return f"Deleted: {agent_name}[{key}]"
    deleted = get_store().memory_delete(agent_name, key)
    if not deleted:
        return f"Key '{key}' not found for agent '{agent_name}'"
    return f"Deleted: {agent_name}[{key}]"


# ── Task Tools ──────────────────────────────────────────────────────────────


def _fmt_task(t: dict) -> str:
    """Format a single task dict as a human-readable line."""
    status = t.get("status", "?")
    priority = t.get("priority", "medium")
    assignee = t.get("assignee") or "unassigned"
    labels = t.get("labels") or []
    label_str = f" [{', '.join(labels)}]" if labels else ""
    return (
        f"[{status}] {t['title']} (id={t['id'][:8]} priority={priority} "
        f"assignee={assignee}{label_str})"
    )


@mcp.tool()
def task_list(
    status: str | None = None,
    assignee: str | None = None,
    limit: int = 20,
) -> str:
    """List tasks, optionally filtered by status or assignee.

    status: todo | in_progress | in_review | done | blocked | cancelled
    Returns a formatted list of matching tasks.
    """
    if _remote_url():
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if assignee:
            params["assignee"] = assignee
        tasks = _rhttp("GET", "/tasks", params=params)
        if not tasks:
            return "No tasks found."
        return "\n".join(_fmt_task(t) for t in (tasks if isinstance(tasks, list) else []))
    tasks = get_store().list_tasks(status=status, assignee=assignee, limit=limit)
    if not tasks:
        return "No tasks found."
    return "\n".join(_fmt_task(t) for t in tasks)


@mcp.tool()
def task_create(
    title: str,
    created_by: str,
    description: str = "",
    priority: str = "medium",
    assignee: str | None = None,
    labels: list[str] | None = None,
    thread: str = "general",
) -> str:
    """Create a new task.

    priority: low | medium | high | critical
    assignee: agent name to assign immediately (optional — leave blank to let agents claim it)
    labels: list of tag strings, e.g. ['backend', 'bug']
    Returns the created task id and title.
    """
    if _remote_url():
        body: dict = {
            "title": title,
            "created_by": created_by,
            "description": description,
            "priority": priority,
            "thread": thread,
            "labels": labels or [],
        }
        if assignee:
            body["assignee"] = assignee
        task = _rhttp("POST", "/tasks", body)
        return f"Task created: {task['id'][:8]} — {task['title']}"
    task = get_store().create_task(
        title=title,
        created_by=created_by,
        description=description,
        priority=priority,
        assignee=assignee,
        thread=thread,
        labels=labels or [],
    )
    return f"Task created: {task['id'][:8]} — {task['title']}"


@mcp.tool()
def task_claim(task_id: str, agent_name: str) -> str:
    """Claim an unassigned task and set its status to in_progress.

    Use this when you pick up work. Prevents two agents from working on
    the same task simultaneously.
    Returns the updated task or an error if already claimed by someone else.
    """
    if _remote_url():
        try:
            task = _rhttp("POST", f"/tasks/{task_id}/claim", params={"agent_name": agent_name})
            return f"Claimed: [{task['status']}] {task['title']} (assigned to {task['assignee']})"
        except RuntimeError as e:
            return f"Could not claim task: {e}"
    task = get_store().get_task(task_id)
    if not task:
        return f"Task {task_id} not found."
    if task.get("assignee") and task["assignee"] != agent_name:
        return f"Task already claimed by {task['assignee']}."
    task = get_store().update_task(task_id, assignee=agent_name, status="in_progress")
    return f"Claimed: [{task['status']}] {task['title']} (assigned to {task['assignee']})"


@mcp.tool()
def task_complete(task_id: str, note: str = "") -> str:
    """Mark a task as done.

    Optionally provide a completion note (e.g. PR number, summary of what was done).
    """
    if _remote_url():
        if note:
            _rhttp("PATCH", f"/tasks/{task_id}", {"description": note})
        task = _rhttp("POST", f"/tasks/{task_id}/complete")
        return f"Completed: {task['title']}"
    if note:
        get_store().update_task(task_id, description=note)
    task = get_store().update_task(task_id, status="done")
    if not task:
        return f"Task {task_id} not found."
    return f"Completed: {task['title']}"


@mcp.tool()
def task_update(
    task_id: str,
    status: str | None = None,
    assignee: str | None = None,
    priority: str | None = None,
    description: str | None = None,
) -> str:
    """Update a task's status, assignee, priority, or description.

    status: todo | in_progress | in_review | done | blocked | cancelled
    priority: low | medium | high | critical
    Only provided fields are updated — omit fields you don't want to change.
    """
    if _remote_url():
        updates: dict = {}
        if status is not None:
            updates["status"] = status
        if assignee is not None:
            updates["assignee"] = assignee
        if priority is not None:
            updates["priority"] = priority
        if description is not None:
            updates["description"] = description
        if not updates:
            return "Nothing to update — provide at least one field."
        task = _rhttp("PATCH", f"/tasks/{task_id}", updates)
        return _fmt_task(task)
    updates = {}
    if status is not None:
        updates["status"] = status
    if assignee is not None:
        updates["assignee"] = assignee
    if priority is not None:
        updates["priority"] = priority
    if description is not None:
        updates["description"] = description
    if not updates:
        return "Nothing to update — provide at least one field."
    task = get_store().update_task(task_id, **updates)
    if not task:
        return f"Task {task_id} not found."
    return _fmt_task(task)
