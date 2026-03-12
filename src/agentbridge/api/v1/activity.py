"""Activity / audit log read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ...deps import get_store

router = APIRouter()


@router.get("/activity")
async def list_activity(
    actor: str | None = Query(None),
    action: str | None = Query(None),
    entity_type: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    return await get_store().list_activity_async(
        actor_id=actor, action=action, entity_type=entity_type, since=since, limit=limit
    )
