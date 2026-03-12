"""HTTP authentication and request logging middleware."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("agentbridge")

# Module-level token variable — kept for test monkeypatching via `server._auth_token`.
# The real value lives in server.py and is imported there; this module reads it
# through the _get_auth_token / get_store accessors passed in at app-creation time
# to avoid circular imports.


def make_middleware(get_store_fn, get_auth_token_fn):
    """Return an auth+logging middleware function bound to the given accessors."""

    async def auth_and_logging_middleware(request: Request, call_next):
        started = time.perf_counter()
        path = request.url.path
        _public = {"/health", "/ui", "/favicon.ico", "/auth/login", "/auth/setup"}
        request.state.user = None
        request.state.is_admin = False

        if path not in _public:
            supplied = (
                request.headers.get("x-agentbridge-token")
                or request.query_params.get("token")
            )
            if supplied:
                session_user = await asyncio.to_thread(
                    get_store_fn().get_session_user, supplied
                )
                if session_user:
                    request.state.user = session_user
                    request.state.is_admin = session_user["role"] == "admin"
                elif get_auth_token_fn() and supplied == get_auth_token_fn():
                    request.state.is_admin = True
                elif await asyncio.to_thread(get_store_fn().verify_agent_key, supplied):
                    request.state.is_admin = False
                else:
                    return JSONResponse(
                        status_code=401, content={"detail": "Unauthorized"}
                    )
            else:
                if get_auth_token_fn() or await asyncio.to_thread(
                    get_store_fn().has_any_users
                ):
                    return JSONResponse(
                        status_code=401, content={"detail": "Unauthorized"}
                    )

        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "%s %s %s %sms", request.method, path, response.status_code, elapsed_ms
        )
        return response

    return auth_and_logging_middleware
