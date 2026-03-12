"""Admin, health, and dashboard endpoints."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ...deps import get_store, get_ws_manager
from ...sse_helpers import broadcast_sse

router = APIRouter()

_server_started_at = time.time()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ClearBoardRequest(BaseModel):
    include_threads: bool = True


class PruneAgentsRequest(BaseModel):
    older_than_sec: int = Field(default=300, ge=60, le=86400)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_admin(request: Request) -> None:
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


def _get_auth_token() -> str:
    # Imported lazily to avoid circular dependency; read from the server module
    # where tests monkeypatch it.
    from agentbridge import server  # noqa: PLC0415

    return server._auth_token or os.environ.get("AGENTBRIDGE_TOKEN", "")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/admin/clear")
async def http_clear_board(body: ClearBoardRequest | None = None):
    payload = body or ClearBoardRequest()
    await get_store()._run_in_thread(get_store().clear_board, payload.include_threads)
    broadcast_sse(
        "system",
        {"type": "clear", "include_threads": payload.include_threads},
    )
    return {"cleared": True, "include_threads": payload.include_threads}


@router.get("/admin/debug")
async def http_admin_debug(request: Request):
    _assert_admin(request)
    manager = get_ws_manager()
    connected = list(manager.connections.keys())
    pending = len(manager.pending_requests)
    incoming = len(manager.incoming_waiters)
    agent_count = len(await get_store().list_agents_async())
    uptime_sec = max(0.0, time.time() - _server_started_at)
    return {
        "ws_connected": connected,
        "ws_connected_count": len(connected),
        "pending_requests": pending,
        "incoming_waiters": incoming,
        "agent_count": agent_count,
        "uptime_sec": uptime_sec,
    }


@router.post("/admin/prune-agents")
async def http_prune_agents(request: Request, body: PruneAgentsRequest | None = None):
    _assert_admin(request)
    payload = body or PruneAgentsRequest()
    now = datetime.now(timezone.utc)
    cutoff = payload.older_than_sec
    manager = get_ws_manager()
    removed: list[str] = []
    agents = await get_store().list_agents_async()
    for a in agents:
        if manager.is_connected(a.name):
            continue
        if (now - a.last_seen).total_seconds() > cutoff:
            await get_store().remove_agent_async(a.name)
            removed.append(a.name)
            broadcast_sse("agent_left", {"name": a.name})
    return {"removed": removed, "count": len(removed), "older_than_sec": cutoff}


@router.get("/health")
async def health():
    has_users = await asyncio.to_thread(get_store().has_any_users)
    auth_token = _get_auth_token()
    if not has_users and not auth_token:
        auth_mode = "setup"
    elif has_users:
        auth_mode = "users"
    elif auth_token:
        auth_mode = "token"
    else:
        auth_mode = "open"
    return {"status": "ok", "version": "0.5.0", "auth": auth_mode}


@router.get("/ui", response_class=HTMLResponse)
def serve_dashboard():
    dashboard_path = Path(__file__).parent.parent.parent / "dashboard.html"
    return HTMLResponse(dashboard_path.read_text())
