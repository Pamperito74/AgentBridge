"""Cost / token tracking endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...deps import get_store
from ...sse_helpers import broadcast_sse

router = APIRouter()


class RecordCostRequest(BaseModel):
    agent_name: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    provider: str = "anthropic"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_cents: int = Field(default=0, ge=0)
    task_id: str | None = None
    thread: str | None = None


class SetBudgetRequest(BaseModel):
    budget_monthly_cents: int = Field(ge=0)


@router.post("/cost-events", status_code=201)
async def record_cost_event(body: RecordCostRequest):
    store = get_store()
    event = await store.record_cost_event_async(
        agent_name=body.agent_name, model=body.model, provider=body.provider,
        input_tokens=body.input_tokens, output_tokens=body.output_tokens,
        cost_cents=body.cost_cents, task_id=body.task_id, thread=body.thread,
    )
    # Check budget threshold and log if exceeded
    agent = await store.get_agent_async(body.agent_name)
    if agent and agent.budget_monthly_cents > 0 and agent.spent_monthly_cents >= agent.budget_monthly_cents:
        broadcast_sse("budget_exceeded", {
            "agent_name": body.agent_name,
            "spent_cents": agent.spent_monthly_cents,
            "budget_cents": agent.budget_monthly_cents,
        })
        await store.log_activity_async(
            action="budget.exceeded", actor_type="system", actor_id=body.agent_name,
            entity_type="agent", entity_id=body.agent_name,
            details={"spent_cents": agent.spent_monthly_cents, "budget_cents": agent.budget_monthly_cents},
        )
    return event


@router.get("/cost-events")
async def list_cost_events(
    agent: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    return await get_store().list_cost_events_async(agent_name=agent, since=since, limit=limit)


@router.get("/agents/{name}/costs/summary")
async def agent_cost_summary(name: str):
    agent = await get_store().get_agent_async(name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    summary = await get_store().cost_summary_for_agent_async(name)
    summary["budget_monthly_cents"] = agent.budget_monthly_cents
    summary["spent_monthly_cents"] = agent.spent_monthly_cents
    if agent.budget_monthly_cents > 0:
        summary["budget_pct"] = round(agent.spent_monthly_cents / agent.budget_monthly_cents * 100, 1)
    else:
        summary["budget_pct"] = None
    return summary


@router.get("/costs/summary")
async def platform_cost_summary():
    return await get_store().cost_summary_platform_async()


@router.patch("/agents/{name}/budget")
async def set_agent_budget(name: str, body: SetBudgetRequest):
    store = get_store()
    agent = await store.get_agent_async(name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    await store.set_agent_budget_async(name, body.budget_monthly_cents)
    await store.log_activity_async(
        action="budget.set", entity_type="agent", entity_id=name,
        details={"budget_monthly_cents": body.budget_monthly_cents},
    )
    return {"agent_name": name, "budget_monthly_cents": body.budget_monthly_cents}
