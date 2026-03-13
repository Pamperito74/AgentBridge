"""Automation rules CRUD + orchestrator status endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...deps import get_store
from ...rule_engine import _BUILTIN_RULES

router = APIRouter()


class CreateRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    trigger_event: str = Field(min_length=1, max_length=128)
    conditions: dict = Field(default_factory=dict)
    action_type: str = Field(min_length=1, max_length=128)
    action_params: dict = Field(default_factory=dict)


@router.get("/automation/rules")
async def list_rules():
    rules = await get_store().list_automation_rules_async()
    return {"builtin": _BUILTIN_RULES, "custom": rules}


@router.post("/automation/rules", status_code=201)
async def create_rule(body: CreateRuleRequest):
    return await get_store().create_automation_rule_async(
        name=body.name, trigger_event=body.trigger_event,
        action_type=body.action_type, conditions=body.conditions,
        action_params=body.action_params,
    )


@router.patch("/automation/rules/{rule_id}")
async def toggle_rule(rule_id: str, enabled: bool):
    ok = await get_store().set_automation_rule_enabled_async(rule_id, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"id": rule_id, "enabled": enabled}


@router.delete("/automation/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: str):
    ok = await get_store().delete_automation_rule_async(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Rule not found")


@router.get("/automation/orchestrators")
async def list_orchestrators():
    agents = await get_store().list_agents_async()
    return [a.model_dump(mode="json") for a in agents if a.is_orchestrator]


@router.get("/automation/status")
async def automation_status():
    store = get_store()
    rules = await store.list_automation_rules_async()
    orchestrators = await store.list_agents_async()
    active_orchs = [a.name for a in orchestrators if a.is_orchestrator]
    autonomous_agents = [a.name for a in orchestrators if a.autonomous]
    return {
        "builtin_rules": len(_BUILTIN_RULES),
        "custom_rules": len(rules),
        "enabled_custom_rules": sum(1 for r in rules if r["enabled"]),
        "orchestrators": active_orchs,
        "autonomous_agents": autonomous_agents,
    }
