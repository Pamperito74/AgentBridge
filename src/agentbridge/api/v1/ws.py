"""WebSocket endpoint."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...deps import get_store, get_ws_manager
from ...sse_helpers import broadcast_sse

router = APIRouter()
logger = logging.getLogger("agentbridge")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for synchronous request-response communication."""
    await websocket.accept()
    manager = get_ws_manager()
    agent_name: str | None = None

    try:
        data = await websocket.receive_text()
        msg = json.loads(data)

        if msg.get("type") != "register":
            await websocket.send_json({"error": "Must register first"})
            await websocket.close()
            return

        agent_name = msg.get("name")
        role = msg.get("role", "")
        capabilities = msg.get("capabilities", [])

        if not agent_name:
            await websocket.send_json({"error": "Missing agent name"})
            await websocket.close()
            return

        get_store().register_agent(agent_name, role, capabilities)
        conn = await manager.register_connection(agent_name, websocket)

        await websocket.send_json(
            {
                "type": "registered",
                "agent_id": agent_name,
                "timestamp": str(time.time()),
            }
        )

        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")

            if msg_type == "request":
                recipient = message.get("recipient")
                content = message.get("content", "")
                correlation_id = message.get("correlation_id")

                stored = get_store().add_message(
                    sender=agent_name,
                    content=content,
                    recipient=recipient,
                    thread=message.get("thread", "general"),
                    msg_type="request",
                    correlation_id=correlation_id,
                )
                broadcast_sse("message", stored.model_dump(mode="json"))

                try:
                    await manager.send_to_agent(
                        recipient,
                        {
                            "type": "request",
                            "correlation_id": correlation_id,
                            "sender": agent_name,
                            "content": content,
                            "thread": message.get("thread", "general"),
                        },
                    )
                except ValueError as e:
                    await websocket.send_json(
                        {
                            "type": "response",
                            "correlation_id": correlation_id,
                            "content": str(e),
                            "status": "error",
                        }
                    )

            elif msg_type == "response":
                correlation_id = message.get("correlation_id")
                content = message.get("content", "")

                get_store().add_message(
                    sender=agent_name,
                    content=content,
                    thread=message.get("thread", "general"),
                    msg_type="response",
                    correlation_id=correlation_id,
                )

                await manager.handle_response(
                    correlation_id,
                    {
                        "content": content,
                        "status": message.get("status", "success"),
                    },
                )

            elif msg_type in ("message", "broadcast"):
                stored = get_store().add_message(
                    sender=agent_name,
                    content=message.get("content", ""),
                    recipient=message.get("recipient"),
                    thread=message.get("thread", "general"),
                    msg_type=message.get("msg_type", "chat"),
                )
                broadcast_sse("message", stored.model_dump(mode="json"))
                if message.get("recipient"):
                    try:
                        await manager.send_to_agent(
                            message["recipient"],
                            {"type": "message", **stored.model_dump(mode="json")},
                        )
                    except ValueError:
                        pass

            elif msg_type in ("stream_start", "stream_chunk", "stream_end"):
                stream_payload = {
                    "type": msg_type,
                    "sender": agent_name,
                    "stream_id": message.get("stream_id", ""),
                    "thread": message.get("thread", "general"),
                    "content": message.get("content", ""),
                    "recipient": message.get("recipient"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                broadcast_sse(msg_type, stream_payload)
                await manager.broadcast(
                    stream_payload,
                    exclude=agent_name,
                    thread=stream_payload["thread"],
                    event_type=msg_type,
                )
                if msg_type == "stream_end":
                    stored = get_store().add_message(
                        sender=agent_name,
                        content=message.get("content", ""),
                        recipient=message.get("recipient"),
                        thread=message.get("thread", "general"),
                        msg_type="chat",
                        metadata={
                            "stream_id": message.get("stream_id", ""),
                            "streamed": True,
                        },
                    )
                    broadcast_sse("message", stored.model_dump(mode="json"))

            elif msg_type == "heartbeat":
                get_store().heartbeat(
                    agent_name,
                    status=message.get("status", "online"),
                    working_on=message.get("working_on", ""),
                )

    except WebSocketDisconnect:
        if agent_name:
            await manager.unregister_connection(agent_name)
            await get_store().remove_agent_async(agent_name)
            broadcast_sse("agent_left", {"name": agent_name})
            logger.info(f"Agent {agent_name} disconnected")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from {agent_name}: {e}")
    except Exception as e:
        logger.error(f"WebSocket error for {agent_name}: {e}")
        if agent_name:
            await manager.unregister_connection(agent_name)
            await get_store().remove_agent_async(agent_name)
            broadcast_sse("agent_left", {"name": agent_name})
