"""Shared singletons accessible from all modules.

Import `get_store`, `get_ws_manager`, etc. from here rather than from server.py
to avoid circular imports.  The actual singleton factories live in their own
modules (store.py, ws_manager.py); this module re-exports them for convenience
and also hosts the shared SSE/uvicorn-loop state that is genuinely cross-cutting.
"""
from __future__ import annotations

import asyncio
import threading

from .store import MessageStore
from .ws_manager import get_ws_manager  # noqa: F401 — re-export

# ---------------------------------------------------------------------------
# Store singleton
# ---------------------------------------------------------------------------

_store: MessageStore | None = None
_store_lock = threading.Lock()


def get_store() -> MessageStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = MessageStore()
        return _store


def set_store(s: MessageStore | None) -> None:
    """Allow test monkeypatching / teardown."""
    global _store
    with _store_lock:
        _store = s


# ---------------------------------------------------------------------------
# Uvicorn event loop reference — captured at startup so MCP tools (sync) can
# submit coroutines to the async WS manager via run_coroutine_threadsafe.
# ---------------------------------------------------------------------------

_uvicorn_loop: asyncio.AbstractEventLoop | None = None


def get_uvicorn_loop() -> asyncio.AbstractEventLoop | None:
    return _uvicorn_loop


def set_uvicorn_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _uvicorn_loop
    _uvicorn_loop = loop


# ---------------------------------------------------------------------------
# SSE subscribers
# ---------------------------------------------------------------------------

_sse_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []
_sse_lock = threading.Lock()
