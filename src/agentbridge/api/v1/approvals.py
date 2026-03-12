"""Approval workflow endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...deps import get_store
from ...sse_helpers import broadcast_sse

router = APIRouter()


class CreateApprovalRequest(BaseModel):
    type: str = Field(min_length=1, max_length=128)
    requested_by: str = Field(min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)


class DecideApprovalRequest(BaseModel):
    decided_by: str = Field(min_length=1, max_length=128)
    decision_note: str | None = None


@router.post("/approvals", status_code=201)
async def create_approval(body: CreateApprovalRequest):
    store = get_store()
    approval = await store.create_approval_async(
        type=body.type, requested_by=body.requested_by, payload=body.payload
    )
    broadcast_sse("approval_requested", approval)
    await store.log_activity_async(
        action="approval.requested", actor_type="agent", actor_id=body.requested_by,
        entity_type="approval", entity_id=approval["id"],
        details={"type": body.type},
    )
    return approval


@router.get("/approvals")
async def list_approvals(
    status: str | None = Query(None),
    type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    return await get_store().list_approvals_async(status=status, type=type, limit=limit)


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str):
    approval = await get_store().get_approval_async(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, body: DecideApprovalRequest):
    store = get_store()
    approval = await store.decide_approval_async(
        approval_id=approval_id, decision="approved",
        decided_by=body.decided_by, decision_note=body.decision_note,
    )
    if not approval:
        raise HTTPException(status_code=409, detail="Approval not found or already decided")
    broadcast_sse("approval_decided", approval)
    await store.log_activity_async(
        action="approval.approved", actor_type="user", actor_id=body.decided_by,
        entity_type="approval", entity_id=approval_id,
    )
    return approval


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, body: DecideApprovalRequest):
    store = get_store()
    approval = await store.decide_approval_async(
        approval_id=approval_id, decision="rejected",
        decided_by=body.decided_by, decision_note=body.decision_note,
    )
    if not approval:
        raise HTTPException(status_code=409, detail="Approval not found or already decided")
    broadcast_sse("approval_decided", approval)
    await store.log_activity_async(
        action="approval.rejected", actor_type="user", actor_id=body.decided_by,
        entity_type="approval", entity_id=approval_id,
        details={"note": body.decision_note},
    )
    return approval


@router.post("/approvals/{approval_id}/cancel", status_code=200)
async def cancel_approval(approval_id: str):
    store = get_store()
    cancelled = await store.cancel_approval_async(approval_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="Approval not found or already decided")
    await store.log_activity_async(
        action="approval.cancelled", entity_type="approval", entity_id=approval_id
    )
    return {"id": approval_id, "status": "cancelled"}
