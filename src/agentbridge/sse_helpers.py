"""SSE broadcast helpers shared across transport and API modules.

These functions read from / write to the shared _sse_subscribers list in deps.py.
Kept in a separate file to avoid circular imports between server.py, transport,
and api sub-modules.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .deps import _sse_lock, _sse_subscribers

logger = logging.getLogger("agentbridge")


def _safe_put(queue: asyncio.Queue[str], payload: str) -> None:
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        logger.warning(
            "SSE subscriber queue full — event dropped "
            "(consider reducing broadcast rate or increasing maxsize)"
        )


def broadcast_sse(event: str, data: dict) -> None:
    """Push an SSE event to all current subscribers (thread-safe)."""
    payload = json.dumps(data, default=str)
    message = f"event: {event}\ndata: {payload}\n\n"
    with _sse_lock:
        subscribers = list(_sse_subscribers)
    for loop, queue in subscribers:
        loop.call_soon_threadsafe(_safe_put, queue, message)
