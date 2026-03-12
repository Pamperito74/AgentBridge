"""AgentBridge app factory.

This module owns the FastAPI application lifecycle. server.py re-exports
`app`, `http_app`, and `main` from here for backward compatibility.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.v1.router import router as v1_router
from .deps import get_store, set_store, set_uvicorn_loop, _store_lock
from .middleware.auth import make_middleware
from .transport.mcp_tools import mcp
from .transport.sse import sse_router

logger = logging.getLogger("agentbridge")

# Kept at module level so tests can monkeypatch `server._auth_token`.
# main.py reads it via _get_auth_token() which is bound into middleware.
_auth_token = ""


# ---------------------------------------------------------------------------
# Dotenv / logging helpers (same logic as original server.py)
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    env_path = os.environ.get("AGENTBRIDGE_DOTENV", ".env")
    path = Path(env_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(
                r'^export\s+(\w+)=["\']?([^"\']*)["\']?$', line
            ) or re.match(r'^(\w+)=["\']?([^"\']*)["\']?$', line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            if key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


def _setup_logging() -> logging.Logger:
    _logger = logging.getLogger("agentbridge")
    if _logger.handlers:
        return _logger
    _logger.setLevel(logging.INFO)
    configured = os.environ.get("AGENTBRIDGE_LOG_DIR")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path.home() / ".agentbridge" / "logs",
            Path.cwd() / ".agentbridge" / "logs",
            Path("/tmp") / "agentbridge-logs",
        ]
    )
    for log_dir in candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "agentbridge.log"
            handler = RotatingFileHandler(
                log_file, maxBytes=2_000_000, backupCount=5
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            _logger.addHandler(handler)
            break
        except OSError:
            continue
    if not _logger.handlers:
        _logger.addHandler(logging.StreamHandler())
    return _logger


def _get_auth_token() -> str:
    return _auth_token or os.environ.get("AGENTBRIDGE_TOKEN", "")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_uvicorn_loop(asyncio.get_running_loop())
    get_store()  # eagerly initialize the singleton
    yield
    # Shutdown — close the store
    import agentbridge.deps as _deps  # noqa: PLC0415

    with _deps._store_lock:
        if _deps._store is not None:
            _deps._store.close()
            set_store(None)


def create_app() -> FastAPI:
    _load_dotenv()
    _setup_logging()

    app = FastAPI(title="AgentBridge", version="0.5.0", lifespan=lifespan)

    # Auth + logging middleware (reads _auth_token from this module)
    middleware_fn = make_middleware(get_store, _get_auth_token)
    app.middleware("http")(middleware_fn)

    # Routes — include without prefix (backward compat) AND with /api/v1 prefix
    app.include_router(v1_router)
    app.include_router(v1_router, prefix="/api/v1")

    # SSE
    app.include_router(sse_router)

    return app


# Module-level app instance
http_app = create_app()
app = http_app  # alias for `uvicorn agentbridge.main:app`


# ---------------------------------------------------------------------------
# Entry point helpers
# ---------------------------------------------------------------------------


def _run_http_server() -> None:
    host = os.environ.get("AGENTBRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENTBRIDGE_PORT", "7890"))
    uvicorn.run(http_app, host=host, port=port, log_level="warning")


def main() -> None:
    """Start both MCP (stdio) and HTTP servers."""
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()
    mcp.run()
