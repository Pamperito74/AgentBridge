"""Authentication and user management endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import urllib.parse
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ...deps import get_store

logger = logging.getLogger("agentbridge")

# ---------------------------------------------------------------------------
# Google OAuth2 config
# ---------------------------------------------------------------------------

_GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# In-memory CSRF state store (good enough for single-process; TTL not needed — short-lived)
_oauth_states: set[str] = set()

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    password: str = Field(min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=256)
    role: Literal["admin", "member"] = "member"


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=256)
    role: Literal["admin", "member"] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=1024)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_admin(request: Request) -> None:
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/auth/setup", status_code=201)
async def auth_setup(body: CreateUserRequest):
    """Create the first admin account. Only works when no users exist."""
    if await asyncio.to_thread(get_store().has_any_users):
        raise HTTPException(
            status_code=409,
            detail="Setup already complete — an admin account exists",
        )
    user = await asyncio.to_thread(
        get_store().create_user,
        body.username,
        body.password,
        body.display_name or body.username,
        "admin",
    )
    token = await asyncio.to_thread(get_store().create_session, user["id"])
    logger.info(
        "First-run setup complete: admin account created for '%s'", body.username
    )
    return {"token": token, "user": user}


@router.post("/auth/login")
async def auth_login(body: LoginRequest):
    user = await asyncio.to_thread(
        get_store().authenticate_user, body.username, body.password
    )
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = await asyncio.to_thread(get_store().create_session, user["id"])
    return {"token": token, "user": user}


@router.post("/auth/logout")
async def auth_logout(request: Request):
    supplied = request.headers.get("x-agentbridge-token") or request.query_params.get(
        "token"
    )
    if supplied:
        await asyncio.to_thread(get_store().delete_session, supplied)
    return {"ok": True}


@router.get("/auth/agent-key")
async def get_agent_key(request: Request):
    """Return whether an agent API key is configured (admins only)."""
    _assert_admin(request)
    key = await asyncio.to_thread(get_store().get_agent_key)
    return {"configured": key is not None}


@router.post("/auth/agent-key")
async def rotate_agent_key(request: Request):
    """Generate a new agent API key. Only admins can rotate the key."""
    _assert_admin(request)
    import secrets as _secrets

    token = _secrets.token_hex(32)
    await asyncio.to_thread(get_store().set_agent_key, token)
    logger.info(
        "Agent API key rotated by %s",
        getattr(request.state.user, "username", "admin"),
    )
    return {
        "agent_key": token,
        "note": "Store this in AGENTBRIDGE_TOKEN — it will not be shown again",
    }


@router.get("/auth/me")
async def auth_me(request: Request):
    if request.state.user:
        return request.state.user
    return {"id": None, "username": "agent", "display_name": "Agent", "role": "admin"}


# --- User management (admin only) ---


@router.get("/users")
async def list_users(request: Request):
    _assert_admin(request)
    return await asyncio.to_thread(get_store().list_users)


@router.post("/users", status_code=201)
async def create_user(body: CreateUserRequest, request: Request):
    _assert_admin(request)
    try:
        user = await asyncio.to_thread(
            get_store().create_user,
            body.username,
            body.password,
            body.display_name,
            body.role,
        )
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(status_code=409, detail="Username already exists")
        raise HTTPException(status_code=500, detail=str(e))
    return user


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUserRequest, request: Request):
    _assert_admin(request)
    user = await asyncio.to_thread(
        get_store().update_user, user_id, body.display_name, body.role, body.password
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, request: Request):
    _assert_admin(request)
    if request.state.user and request.state.user["id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    deleted = await asyncio.to_thread(get_store().delete_user, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")


# ---------------------------------------------------------------------------
# Google OAuth2 SSO
# ---------------------------------------------------------------------------


@router.get("/auth/google/config")
async def google_config():
    """Tell the dashboard whether Google SSO is configured."""
    return {"enabled": bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET)}


@router.get("/auth/google")
async def google_login(request: Request):
    """Redirect the browser to Google's OAuth2 consent screen."""
    if not _GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=501, detail="Google SSO is not configured (GOOGLE_CLIENT_ID missing)")

    state = secrets.token_urlsafe(24)
    _oauth_states.add(state)

    redirect_uri = _get_redirect_uri(request)
    params = urllib.parse.urlencode({
        "client_id": _GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return RedirectResponse(url=f"{_GOOGLE_AUTH_URL}?{params}")


@router.get("/auth/google/callback")
async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None, request: Request = None):
    """Google redirects here after user grants/denies access."""
    if error:
        return RedirectResponse(url="/ui?error=google_denied")

    if not code or not state or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    _oauth_states.discard(state)

    redirect_uri = _get_redirect_uri(request)

    # Exchange authorization code for tokens
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": _GOOGLE_CLIENT_ID,
            "client_secret": _GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            logger.error("Google token exchange failed: %s", token_resp.text)
            raise HTTPException(status_code=502, detail="Google token exchange failed")
        token_data = token_resp.json()

        # Fetch user profile
        userinfo_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch Google user info")
        profile = userinfo_resp.json()

    google_id = profile.get("sub", "")
    email = profile.get("email", "")
    name = profile.get("name", email.split("@")[0])
    avatar = profile.get("picture", "")

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Google profile missing required fields")

    # Upsert user
    user = await asyncio.to_thread(
        get_store().find_or_create_google_user, google_id, email, name, avatar
    )

    # Create session
    session_token = await asyncio.to_thread(get_store().create_session, user["id"])
    logger.info("Google SSO login: %s (%s)", user["username"], email)

    # Redirect to dashboard with token in URL hash (never logged by server)
    return RedirectResponse(url=f"/ui#token={session_token}")


def _get_redirect_uri(request: Request) -> str:
    """Build the OAuth callback URI from env or from the incoming request."""
    env_uri = os.environ.get("GOOGLE_REDIRECT_URI", "")
    if env_uri:
        return env_uri
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/google/callback"
