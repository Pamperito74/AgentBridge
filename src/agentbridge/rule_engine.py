"""Automation rule engine.

Evaluates stored trigger rules whenever a platform event fires and executes
the configured action (notify_orchestrator, assign_task, send_message, etc.).

Called from sse_helpers.broadcast_sse() in a daemon thread so it never blocks
the HTTP response path.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("agentbridge.rules")

# ── Built-in rules always active (never stored in DB) ──────────────────────
_BUILTIN_RULES = [
    {
        "id": "builtin-unassigned-task",
        "name": "Unassigned task → orchestrator",
        "trigger_event": "task_created",
        "conditions": {},           # fires on every task_created; orchestrator checks assignee
        "action_type": "notify_orchestrator",
        "action_params": {},
        "enabled": True,
        "builtin": True,
    },
]


def _matches(data: dict, conditions: dict) -> bool:
    """Return True if all conditions match the event data."""
    for key, value in conditions.items():
        if data.get(key) != value:
            return False
    return True


def _execute(rule: dict, event: str, data: dict, store) -> None:
    action = rule["action_type"]
    params = rule.get("action_params") or {}

    if action == "notify_orchestrator":
        _notify_orchestrators(event, data, params, store)

    elif action == "assign_task":
        task_id = data.get("id")
        assignee = params.get("assignee")
        if task_id and assignee:
            try:
                store.update_task(task_id, assignee=assignee)
                logger.info("Rule '%s': assigned task %s to %s", rule["name"], task_id, assignee)
            except Exception as exc:
                logger.warning("Rule '%s': assign_task failed: %s", rule["name"], exc)

    elif action == "send_message":
        sender = params.get("sender", "system")
        recipient = params.get("recipient")
        content = params.get("content", f"Automation triggered: {event}")
        thread = params.get("thread", "general")
        try:
            store.add_message(sender=sender, content=content, recipient=recipient,
                              thread=thread, msg_type="alert")
            logger.info("Rule '%s': sent message to %s", rule["name"], recipient or "broadcast")
        except Exception as exc:
            logger.warning("Rule '%s': send_message failed: %s", rule["name"], exc)

    elif action == "forward_to_agent":
        # Re-send the original message payload to a specific agent
        target = params.get("agent")
        if target and "sender" in data:
            content = data.get("content", json.dumps(data))
            try:
                store.add_message(sender="system", content=content, recipient=target,
                                  thread=data.get("thread", "general"), msg_type="request")
            except Exception as exc:
                logger.warning("Rule '%s': forward_to_agent failed: %s", rule["name"], exc)


def _notify_orchestrators(event: str, data: dict, params: dict, store) -> None:
    """Send a structured DM to every registered orchestrator agent."""
    try:
        orchestrators = store.get_orchestrators()
    except Exception as exc:
        logger.warning("Rule engine: get_orchestrators failed: %s", exc)
        return

    if not orchestrators:
        return

    # Build a rich notification so the orchestrator has full context
    payload = {
        "event": event,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = f"ORCHESTRATOR_EVENT:{json.dumps(payload)}"

    for orch in orchestrators:
        try:
            store.add_message(
                sender="system",
                content=content,
                recipient=orch.name,
                thread="system",
                msg_type="request",
            )
            logger.info("Rule engine: notified orchestrator '%s' of %s", orch.name, event)
        except Exception as exc:
            logger.warning("Rule engine: notify %s failed: %s", orch.name, exc)


def evaluate_rules(event: str, data: dict, store) -> None:
    """Evaluate all matching rules for the given event. Called in a daemon thread."""
    try:
        # Built-in rules first
        for rule in _BUILTIN_RULES:
            if not rule["enabled"]:
                continue
            if rule["trigger_event"] != event:
                continue
            if _matches(data, rule["conditions"]):
                _execute(rule, event, data, store)

        # User-defined rules from DB
        try:
            rules = store.list_automation_rules(trigger_event=event, enabled_only=True)
        except Exception:
            rules = []

        for rule in rules:
            if _matches(data, rule["conditions"]):
                _execute(rule, event, data, store)

    except Exception as exc:
        logger.exception("Rule engine error for event '%s': %s", event, exc)


def fire_rules_async(event: str, data: dict, store) -> None:
    """Fire rule evaluation in a daemon background thread (non-blocking)."""
    t = threading.Thread(target=evaluate_rules, args=(event, data, store), daemon=True)
    t.start()
