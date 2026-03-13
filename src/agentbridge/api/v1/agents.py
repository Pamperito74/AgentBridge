"""Agent CRUD, heartbeat, requests, and memory endpoints."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...deps import get_store, get_uvicorn_loop, get_ws_manager
from ...sse_helpers import broadcast_sse
from ...ws_helpers import try_ws_deliver

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RegisterAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="", max_length=512)
    capabilities: list[str] = Field(default_factory=list)
    agent_type: Literal["bot", "human"] = "bot"


class HeartbeatRequest(BaseModel):
    status: Literal["online", "busy", "idle", "needs_input"] = "online"
    working_on: str = Field(default="", max_length=512)


class MemorySetRequest(BaseModel):
    value: str = Field(min_length=0, max_length=100000)


class AgentRespondRequest(BaseModel):
    from_agent: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=10000)
    status: Literal["success", "error"] = "success"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_active_window_sec() -> int:
    try:
        return int(os.environ.get("AGENTBRIDGE_ACTIVE_WINDOW_SEC", "300"))
    except ValueError:
        return 300


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/agents")
async def http_register_agent(body: RegisterAgentRequest):
    store = get_store()
    existing = await store.get_agent_async(body.name)
    agent = await store.register_agent_async(
        body.name, body.role, body.capabilities, agent_type=body.agent_type
    )
    result = agent.model_dump(mode="json")
    if existing:
        result["warning"] = (
            f"Agent '{body.name}' was already registered — previous instance evicted"
        )
    broadcast_sse("agent_joined", result)
    await store.log_activity_async(
        action="agent.registered", actor_type="agent", actor_id=body.name,
        entity_type="agent", entity_id=body.name,
        details={"role": body.role, "agent_type": body.agent_type},
    )
    return result


@router.get("/agents")
async def http_list_agents(active: bool = Query(False)):
    agents = await get_store().list_agents_async()
    if active:
        now = datetime.now(timezone.utc)
        window = _agent_active_window_sec()
        manager = get_ws_manager()
        agents = [
            a
            for a in agents
            if manager.is_connected(a.name)
            or (now - a.last_seen).total_seconds() <= window
        ]
    return [a.model_dump(mode="json") for a in agents]


@router.get("/actors")
async def http_list_actors():
    actors = []
    agent_rows = await get_store().list_agents_async()
    for a in agent_rows:
        data = a.model_dump(mode="json")
        actors.append(
            {
                "id": data["name"],
                "type": "agent",
                "status": data.get("status", "online"),
                "role": data.get("role", ""),
                "capabilities": data.get("capabilities", []),
                "working_on": data.get("working_on", ""),
                "last_seen": data.get("last_seen"),
            }
        )
    return actors


@router.delete("/agents/{name}")
async def http_kick_agent(name: str):
    store = get_store()
    manager = get_ws_manager()
    loop = get_uvicorn_loop()
    if loop and loop.is_running() and manager.is_connected(name):
        try:
            asyncio.run_coroutine_threadsafe(
                manager.unregister_connection(name), loop
            ).result(timeout=2)
        except Exception:
            pass
    await store.remove_agent_async(name)
    broadcast_sse("agent_kicked", {"name": name})
    await store.log_activity_async(
        action="agent.kicked", entity_type="agent", entity_id=name
    )
    return {"kicked": name}


@router.post("/agents/{name}/heartbeat")
async def http_heartbeat(name: str, body: HeartbeatRequest):
    agent = await get_store().heartbeat_async(
        name, status=body.status, working_on=body.working_on
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    result = agent.model_dump(mode="json")
    broadcast_sse("agent_status", result)
    return result


# --- Agent Request Queue ---


@router.get("/agents/{name}/requests")
async def http_get_agent_requests(name: str):
    """Poll pending requests for this agent."""
    requests = await get_store().get_pending_requests_async(name)
    return requests


@router.post("/agents/{name}/requests/{request_id}/respond")
async def http_respond_to_agent_request(
    name: str, request_id: str, body: AgentRespondRequest
):
    """Respond to a pending request via HTTP."""
    req = await get_store().get_agent_request_async(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["to_agent"] != name:
        raise HTTPException(
            status_code=403, detail="This request is not addressed to you"
        )
    if req["status"] != "pending":
        raise HTTPException(
            status_code=409, detail=f"Request already {req['status']}"
        )

    ok = await get_store().respond_to_agent_request_async(request_id, body.content)
    if not ok:
        raise HTTPException(status_code=409, detail="Request already answered")

    correlation_id = req["correlation_id"]
    msg = await get_store().add_message_async(
        sender=name,
        recipient=req["from_agent"],
        content=body.content,
        thread=req.get("thread", "general"),
        msg_type="response",
        correlation_id=correlation_id,
    )
    broadcast_sse("message", msg.model_dump(mode="json"))

    loop = get_uvicorn_loop()
    if loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                get_ws_manager().handle_response(
                    correlation_id,
                    {"content": body.content, "status": body.status},
                ),
                loop,
            )
        except Exception:
            pass

    try_ws_deliver(
        req["from_agent"],
        {
            "type": "response",
            "correlation_id": correlation_id,
            "content": body.content,
            "status": body.status,
        },
    )

    broadcast_sse(
        "agent_request_answered",
        {
            "request_id": request_id,
            "from_agent": req["from_agent"],
            "to_agent": name,
            "correlation_id": correlation_id,
        },
    )

    return {"ok": True, "request_id": request_id, "correlation_id": correlation_id}


# --- Agent Memory ---


@router.put("/agents/{name}/memory/{key}", status_code=200)
async def http_memory_set(name: str, key: str, body: MemorySetRequest):
    result = await get_store().memory_set_async(name, key, body.value)
    return result


@router.get("/agents/{name}/memory/{key}")
async def http_memory_get(name: str, key: str):
    entry = await get_store().memory_get_async(name, key)
    if not entry:
        raise HTTPException(status_code=404, detail="Memory key not found")
    return entry


@router.get("/agents/{name}/memory")
async def http_memory_list(name: str, q: str | None = Query(None)):
    if q:
        return await get_store().memory_search_async(name, q)
    return await get_store().memory_list_async(name)


@router.delete("/agents/{name}/memory/{key}", status_code=204)
async def http_memory_delete(name: str, key: str):
    deleted = await get_store().memory_delete_async(name, key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory key not found")
