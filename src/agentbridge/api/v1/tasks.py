"""Task management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...deps import get_store
from ...sse_helpers import broadcast_sse

router = APIRouter()


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=4096)
    status: str = "todo"
    priority: str = "medium"
    assignee: str | None = None
    thread: str = "general"
    parent_id: str | None = None
    labels: list[str] = Field(default_factory=list)
    created_by: str = Field(min_length=1, max_length=128)


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee: str | None = None
    thread: str | None = None
    labels: list[str] | None = None


class AssignTaskRequest(BaseModel):
    assignee: str = Field(min_length=1, max_length=128)


@router.post("/tasks", status_code=201)
async def create_task(body: CreateTaskRequest):
    store = get_store()
    task = await store.create_task_async(
        title=body.title, created_by=body.created_by, description=body.description,
        status=body.status, priority=body.priority, assignee=body.assignee,
        thread=body.thread, parent_id=body.parent_id, labels=body.labels,
    )
    broadcast_sse("task_created", task)
    await store.log_activity_async(
        action="task.created", actor_type="user", actor_id=body.created_by,
        entity_type="task", entity_id=task["id"],
        details={"title": body.title, "priority": body.priority},
    )
    return task


@router.get("/tasks")
async def list_tasks(
    status: str | None = Query(None),
    assignee: str | None = Query(None),
    thread: str | None = Query(None),
    priority: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    return await get_store().list_tasks_async(
        status=status, assignee=assignee, thread=thread, priority=priority, limit=limit
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = await get_store().get_task_async(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: UpdateTaskRequest):
    store = get_store()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    task = await store.update_task_async(task_id, **updates)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    broadcast_sse("task_updated", task)
    await store.log_activity_async(
        action="task.updated", entity_type="task", entity_id=task_id, details=updates
    )
    return task


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str):
    store = get_store()
    deleted = await store.delete_task_async(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    broadcast_sse("task_deleted", {"id": task_id})
    await store.log_activity_async(
        action="task.deleted", entity_type="task", entity_id=task_id
    )


@router.post("/tasks/{task_id}/assign")
async def assign_task(task_id: str, body: AssignTaskRequest):
    store = get_store()
    task = await store.update_task_async(task_id, assignee=body.assignee)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    broadcast_sse("task_updated", task)
    await store.log_activity_async(
        action="task.assigned", entity_type="task", entity_id=task_id,
        details={"assignee": body.assignee},
    )
    return task


@router.post("/tasks/{task_id}/claim")
async def claim_task(task_id: str, agent_name: str = Query(...)):
    store = get_store()
    task = await store.get_task_async(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["assignee"] and task["assignee"] != agent_name:
        raise HTTPException(status_code=409, detail="Task already assigned to another agent")
    task = await store.update_task_async(task_id, assignee=agent_name, status="in_progress")
    broadcast_sse("task_updated", task)
    await store.log_activity_async(
        action="task.claimed", actor_type="agent", actor_id=agent_name,
        entity_type="task", entity_id=task_id,
    )
    return task


@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str):
    store = get_store()
    task = await store.update_task_async(task_id, status="done")
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    broadcast_sse("task_completed", task)
    await store.log_activity_async(
        action="task.completed", entity_type="task", entity_id=task_id
    )
    return task
