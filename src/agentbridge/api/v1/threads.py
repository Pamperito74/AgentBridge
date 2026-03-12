"""Thread management and cursor endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...deps import get_store
from ...sse_helpers import broadcast_sse

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateThreadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    created_by: str = Field(min_length=1, max_length=128)


class CursorRequest(BaseModel):
    agent_name: str = Field(min_length=1, max_length=128)
    thread: str = Field(default="general", min_length=1, max_length=128)
    last_message_id: str = Field(min_length=1)
    last_timestamp: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/threads")
async def http_create_thread(body: CreateThreadRequest):
    t = await get_store().create_thread_async(body.name, body.created_by)
    result = t.model_dump(mode="json")
    broadcast_sse("thread_created", result)
    return result


@router.get("/threads")
async def http_list_threads():
    threads = await get_store().list_threads_async()
    return [t.model_dump(mode="json") for t in threads]


@router.get("/threads/{name}/summary")
async def http_thread_summary(name: str):
    summary = await get_store().thread_summary_async(name)
    return summary.model_dump(mode="json")


@router.get("/cursors")
async def http_list_cursors(agent: str | None = Query(None)):
    cursors = await get_store().list_delivery_cursors_async(agent)
    return cursors


@router.post("/cursors")
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
