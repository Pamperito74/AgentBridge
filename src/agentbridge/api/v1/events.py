"""Event bus and schema registry endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...deps import get_store
from ...rate_limit import get_rate_limiter
from ...schema_registry import SchemaRegistry, SchemaValidationError
from ...sse_helpers import broadcast_sse
from ...ws_helpers import try_ws_deliver

router = APIRouter()

# Module-level schema registry singleton — shared with mcp_tools.py via import
schema_registry = SchemaRegistry()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/bus/events")
async def http_send_event(body: EventWriteRequest):
    if not get_rate_limiter().acquire(
        f"event:{body.actor_id}", rate=100.0, burst=200.0
    ):
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
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
    broadcast_sse("event", result)
    broadcast_sse("message", result)
    if body.target_id:
        try_ws_deliver(body.target_id, {"type": "event", **result})
    return result


@router.get("/bus/events")
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
        thread=thread,
        actor_id=actor_id,
        target_id=target_id,
        event_type=event_type,
        since=since,
        before=before,
        limit=limit,
        since_id=since_id,
    )
    return [m.model_dump(mode="json") for m in messages]


@router.get("/bus/schemas")
def http_list_event_schemas():
    return schema_registry.list()


@router.post("/bus/schemas")
def http_register_event_schema(body: EventSchemaWriteRequest):
    try:
        schema_registry.upsert(body.event_type, body.definition)
    except SchemaValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "event_type": body.event_type}
