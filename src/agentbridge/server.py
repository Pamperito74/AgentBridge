"""AgentBridge server — backward-compatibility shim.

All logic has been extracted into domain modules. This file re-exports
everything that tests and external code may import directly from
`agentbridge.server`.
"""

# ---------------------------------------------------------------------------
# Core app + entry point
# ---------------------------------------------------------------------------
from .main import app, http_app, main, _get_auth_token, _run_http_server  # noqa: F401

# ---------------------------------------------------------------------------
# Shared singletons — keep module-level references so test monkeypatching
# (`monkeypatch.setattr(server, "store", ...)`) continues to work.
# ---------------------------------------------------------------------------
import agentbridge.deps as _deps  # noqa: E402

# `store` attribute: tests do `monkeypatch.setattr(server, "store", store_fixture)`
# We expose a property-like getter but also allow direct attribute assignment by
# keeping a module-level variable that shadows _deps._store.
from .deps import get_store as _get_store_dep  # noqa: E402

# Tests monkeypatch `server.store` directly, so we expose it as a plain attribute.
# The property trick won't work with monkeypatch, so we use __getattr__ / import trick.
# Simplest approach: just re-point to deps._store via a module __getattr__.

def __getattr__(name: str):  # noqa: N807 — module-level __getattr__
    if name == "store":
        return _deps._store
    raise AttributeError(f"module 'agentbridge.server' has no attribute {name!r}")


def __setattr__(name: str, value):  # noqa: N807
    if name == "store":
        _deps._store = value
        return
    raise AttributeError(f"Cannot set module attribute {name!r}")


# ---------------------------------------------------------------------------
# Auth token — tests monkeypatch `server._auth_token` directly
# ---------------------------------------------------------------------------
import agentbridge.main as _main  # noqa: E402

# Expose _auth_token as a module attribute.  monkeypatch.setattr rewrites the
# attribute on the module object, so we keep main._auth_token in sync via the
# same __getattr__ / __setattr__ approach.
# Overwrite the module-level getattr/setattr to also handle _auth_token.

import sys as _sys  # noqa: E402


class _ServerModule(_sys.modules[__name__].__class__):
    """Module subclass that forwards store/_auth_token access to deps/main."""

    def __getattr__(self, name: str):
        if name == "store":
            return _deps._store
        if name == "_auth_token":
            return _main._auth_token
        raise AttributeError(
            f"module 'agentbridge.server' has no attribute {name!r}"
        )

    def __setattr__(self, name: str, value):
        # Allow normal module attributes (those set during import)
        if name.startswith("__") or name in (
            "app", "http_app", "main", "_get_auth_token", "_run_http_server",
            "_deps", "_main", "_sys", "_ServerModule",
        ):
            super().__setattr__(name, value)
            return
        if name == "store":
            _deps._store = value
            return
        if name == "_auth_token":
            _main._auth_token = value
            return
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _ServerModule

# ---------------------------------------------------------------------------
# SSE helpers — tests access `server._sse_subscribers` and `server._broadcast_sse`
# ---------------------------------------------------------------------------
from .deps import _sse_subscribers, _sse_lock  # noqa: F401, E402
from .sse_helpers import broadcast_sse as _broadcast_sse  # noqa: F401, E402

# ---------------------------------------------------------------------------
# MCP instance — some code may `from agentbridge.server import mcp`
# ---------------------------------------------------------------------------
from .transport.mcp_tools import mcp  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Schema registry — some code may import it from server
# ---------------------------------------------------------------------------
from .api.v1.events import schema_registry  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Request/response model classes — re-export for any code that imports them
# from agentbridge.server
# ---------------------------------------------------------------------------
from .api.v1.agents import (  # noqa: F401, E402
    RegisterAgentRequest,
    HeartbeatRequest,
    MemorySetRequest,
    AgentRespondRequest,
)
from .api.v1.messages import (  # noqa: F401, E402
    SendMessageRequest,
    ArtifactRequest,
    ContentBlockRequest,
    ClaimRequest,
)
from .api.v1.threads import CreateThreadRequest, CursorRequest  # noqa: F401, E402
from .api.v1.auth import LoginRequest, CreateUserRequest, UpdateUserRequest  # noqa: F401, E402
from .api.v1.events import EventWriteRequest, EventSchemaWriteRequest  # noqa: F401, E402
from .api.v1.admin import ClearBoardRequest, PruneAgentsRequest  # noqa: F401, E402

# ---------------------------------------------------------------------------
# WS / misc helpers used internally by tests
# ---------------------------------------------------------------------------
from .ws_helpers import try_ws_deliver as _try_ws_deliver  # noqa: F401, E402
from .ws_helpers import try_ws_broadcast as _try_ws_broadcast  # noqa: F401, E402
from .ws_helpers import notify_incoming_waiter as _notify_incoming_waiter  # noqa: F401, E402
from .deps import get_store, get_uvicorn_loop, set_uvicorn_loop  # noqa: F401, E402

# keep _server_started_at for any external code that reads it
import time as _time  # noqa: E402
_server_started_at = _time.time()
