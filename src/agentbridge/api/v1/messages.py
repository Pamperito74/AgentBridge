"""Message send, read, and claim endpoints."""
from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...deps import get_store, get_uvicorn_loop, get_ws_manager
from ...rate_limit import get_rate_limiter
from ...sse_helpers import broadcast_sse
from ...ws_helpers import try_ws_broadcast, try_ws_deliver

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ArtifactRequest(BaseModel):
    type: Literal["file", "code", "url"]
    content: str = Field(min_length=1, max_length=4000)


class ContentBlockRequest(BaseModel):
    type: str = Field(default="text", max_length=64)
    content: str = Field(min_length=1, max_length=40000)
    language: str | None = Field(default=None, max_length=64)
    mime_type: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=256)


class SendMessageRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=10000)
    recipient: str | None = Field(default=None, max_length=128)
    thread: str = Field(default="general", min_length=1, max_length=128)
    msg_type: Literal["chat", "request", "response", "status", "alert"] = "chat"
    artifacts: list[ArtifactRequest] | None = None
    blocks: list[ContentBlockRequest] | None = None
    correlation_id: str | None = None


class ClaimRequest(BaseModel):
    agent_name: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/messages")
async def http_send_message(request: Request, body: SendMessageRequest):
    if not get_rate_limiter().acquire(f"send:{body.sender}", rate=30.0, burst=60.0):
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
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
        blocks=[b.model_dump() for b in body.blocks] if body.blocks else None,
        correlation_id=correlation_id,
    )
    result = msg.model_dump(mode="json")
    broadcast_sse("message", result)

    if msg.msg_type == "request" and msg.recipient:
        await get_ws_manager().notify_incoming_request(msg.recipient, result)

    if body.msg_type == "request":
        request_msg = {
            "type": "request",
            "correlation_id": correlation_id,
            "sender": body.sender,
            "content": body.content,
            "thread": body.thread,
        }
        if body.recipient:
            try_ws_deliver(body.recipient, request_msg)
        else:
            try_ws_broadcast(request_msg, exclude=body.sender)
    else:
        message_msg = {"type": "message", **result}
        if body.recipient:
            try_ws_deliver(body.recipient, message_msg)
        else:
            try_ws_broadcast(message_msg, exclude=body.sender)

    loop = get_uvicorn_loop()
    if body.msg_type == "response" and body.correlation_id and loop:
        try:
            asyncio.run_coroutine_threadsafe(
                get_ws_manager().handle_response(
                    body.correlation_id,
                    {"content": body.content, "status": "success"},
                ),
                loop,
            )
        except Exception:
            pass

    return result


@router.get("/messages")
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
        thread=thread,
        since=since,
        before=before,
        limit=limit,
        sender=sender,
        as_agent=as_agent,
        correlation_id=correlation_id,
        since_id=since_id,
    )
    return [m.model_dump(mode="json") for m in messages]


@router.post("/messages/{message_id}/claim")
async def http_claim_message(message_id: str, body: ClaimRequest):
    """Atomically claim a message for processing."""
    success = await get_store().claim_message_async(message_id, body.agent_name)
    if not success:
        raise HTTPException(
            status_code=409, detail="Message already claimed or not found"
        )
    return {"ok": True, "claimed_by": body.agent_name}
