"""SSE /events endpoint."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..deps import _sse_lock, _sse_subscribers, get_store
from ..models import Message

sse_router = APIRouter()


@sse_router.get("/events")
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
        cursor = await get_store().get_delivery_cursor_async(
            cursor_agent, cursor_thread_value
        )
        if cursor and cursor.get("last_timestamp"):
            since_timestamp = cursor["last_timestamp"]

    backlog: list[Message] = []
    if since_timestamp:
        backlog = await get_store().read_messages_async(
            thread=thread_filter,
            since=since_timestamp,
            limit=backlog_limit,
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
        await get_store().set_delivery_cursor_async(
            cursor_agent, thread_name, message_id, timestamp
        )

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
                    if (
                        thread_filter
                        and payload
                        and payload.get("thread") != thread_filter
                    ):
                        continue
                    if payload:
                        await _update_cursor(payload, payload.get("thread"))
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
