"""WebSocket connection manager for synchronous request-response communication."""

import asyncio
import json
import logging
import uuid
from typing import Callable, Coroutine

logger = logging.getLogger("agentbridge.ws")


class PendingRequest:
    """Track a request waiting for a response."""

    def __init__(self, request_id: str, timeout_sec: float = 300):
        self.request_id = request_id
        self.timeout_sec = timeout_sec
        self.response_future: asyncio.Future = asyncio.Future()
        self.created_at = asyncio.get_event_loop().time()

    async def wait(self) -> dict:
        """Wait for response with timeout."""
        try:
            response = await asyncio.wait_for(
                self.response_future,
                timeout=self.timeout_sec
            )
            return response
        except asyncio.TimeoutError:
            raise TimeoutError(f"Request {self.request_id} timed out after {self.timeout_sec}s")

    def resolve(self, response: dict):
        """Resolve with response."""
        if not self.response_future.done():
            self.response_future.set_result(response)

    def reject(self, error: Exception):
        """Reject with error."""
        if not self.response_future.done():
            self.response_future.set_exception(error)


class WebSocketConnectionManager:
    """Manage WebSocket connections and request-response correlation."""

    def __init__(self):
        # Agent name -> WebSocket connection
        self.connections: dict[str, "WebSocketConnection"] = {}
        # Request ID -> PendingRequest
        self.pending_requests: dict[str, PendingRequest] = {}
        self.lock = asyncio.Lock()

    async def register_connection(self, agent_name: str, ws) -> "WebSocketConnection":
        """Register a new WebSocket connection."""
        async with self.lock:
            conn = WebSocketConnection(agent_name, ws, self)
            self.connections[agent_name] = conn
            logger.info(f"WebSocket connected: {agent_name}")
            return conn

    async def unregister_connection(self, agent_name: str):
        """Unregister a WebSocket connection."""
        async with self.lock:
            if agent_name in self.connections:
                del self.connections[agent_name]
                logger.info(f"WebSocket disconnected: {agent_name}")

    async def send_to_agent(self, agent_name: str, message: dict):
        """Send a message to an agent via WebSocket."""
        async with self.lock:
            conn = self.connections.get(agent_name)

        if not conn:
            raise ValueError(f"Agent '{agent_name}' not connected")

        await conn.send_json(message)

    async def request(
        self,
        recipient: str,
        content: str,
        timeout_sec: float = 300,
        sender: str | None = None,
        thread: str = "general",
    ) -> dict:
        """Send a request and wait for response."""
        request_id = str(uuid.uuid4())

        # Create pending request
        pending = PendingRequest(request_id, timeout_sec)
        async with self.lock:
            self.pending_requests[request_id] = pending

        try:
            # Send request message
            message = {
                "type": "request",
                "correlation_id": request_id,
                "sender": sender,
                "content": content,
                "thread": thread,
            }
            await self.send_to_agent(recipient, message)

            # Wait for response
            response = await pending.wait()
            return response
        finally:
            # Clean up pending request
            async with self.lock:
                self.pending_requests.pop(request_id, None)

    async def handle_response(self, correlation_id: str, response: dict):
        """Handle incoming response message."""
        async with self.lock:
            pending = self.pending_requests.get(correlation_id)

        if pending:
            pending.resolve(response)
        else:
            logger.warning(f"Received response for unknown request: {correlation_id}")

    async def get_connection(self, agent_name: str) -> "WebSocketConnection | None":
        """Get a connection by agent name."""
        async with self.lock:
            return self.connections.get(agent_name)

    async def get_all_agents(self) -> list[str]:
        """Get list of connected agents."""
        async with self.lock:
            return list(self.connections.keys())


class WebSocketConnection:
    """Individual WebSocket connection."""

    def __init__(self, agent_name: str, ws, manager: WebSocketConnectionManager):
        self.agent_name = agent_name
        self.ws = ws
        self.manager = manager
        self.request_handlers: dict[str, Callable] = {}

    async def send_json(self, data: dict):
        """Send JSON message to agent."""
        try:
            await self.ws.send_json(data)
        except Exception as e:
            logger.error(f"Failed to send to {self.agent_name}: {e}")
            raise

    def on_request(self, handler: Callable[[dict], Coroutine]):
        """Register a request handler."""
        self.request_handlers["default"] = handler

    async def handle_message(self, data: dict):
        """Handle incoming message."""
        msg_type = data.get("type", "message")

        if msg_type == "response":
            # Handle response to our request
            correlation_id = data.get("correlation_id")
            if correlation_id:
                await self.manager.handle_response(correlation_id, data)

        elif msg_type == "request":
            # Handle incoming request
            handler = self.request_handlers.get("default")
            if handler:
                try:
                    response_content = await handler(data)
                    response = {
                        "type": "response",
                        "correlation_id": data.get("correlation_id"),
                        "content": response_content,
                        "status": "success",
                    }
                    await self.send_json(response)
                except Exception as e:
                    error_response = {
                        "type": "response",
                        "correlation_id": data.get("correlation_id"),
                        "content": str(e),
                        "status": "error",
                    }
                    await self.send_json(error_response)


# Global manager instance
ws_manager: WebSocketConnectionManager | None = None


def get_ws_manager() -> WebSocketConnectionManager:
    """Get or create the global WebSocket manager."""
    global ws_manager
    if ws_manager is None:
        ws_manager = WebSocketConnectionManager()
    return ws_manager
