"""Best-effort WebSocket delivery helpers.

Called from synchronous HTTP handlers and MCP tools.
Silently skip if the uvicorn loop is not yet running.
"""
from __future__ import annotations

import asyncio

from .deps import get_uvicorn_loop, get_ws_manager


def try_ws_deliver(recipient: str, message: dict) -> None:
    """Fire-and-forget delivery to a single WS-connected agent."""
    loop = get_uvicorn_loop()
    if loop is None or not loop.is_running():
        return
    manager = get_ws_manager()
    if not manager.is_connected(recipient):
        return
    asyncio.run_coroutine_threadsafe(
        manager.send_to_agent(recipient, message),
        loop,
    )


def try_ws_broadcast(message: dict, exclude: str | None = None) -> None:
    """Fire-and-forget broadcast to all WS-connected agents."""
    loop = get_uvicorn_loop()
    if loop is None or not loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(
        get_ws_manager().broadcast(message, exclude=exclude),
        loop,
    )


def notify_incoming_waiter(recipient: str, message_data: dict) -> None:
    """Notify any wait_for_request() waiter registered for recipient."""
    loop = get_uvicorn_loop()
    if loop is None or not loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(
        get_ws_manager().notify_incoming_request(recipient, message_data),
        loop,
    )
