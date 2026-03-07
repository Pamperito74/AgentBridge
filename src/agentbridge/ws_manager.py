"""WebSocket connection manager for synchronous request-response communication."""

import asyncio
import logging
import uuid
from typing import Callable, Coroutine

logger = logging.getLogger("agentbridge.ws")


class PendingRequest:
    """Track a request waiting for a response.

    The Future is created lazily inside wait() to ensure it binds to the
    correct running event loop — avoids the deprecated asyncio.get_event_loop()
    and 'Future attached to a different loop' errors.
    """

    def __init__(self, request_id: str, timeout_sec: float = 300):
        self.request_id = request_id
        self.timeout_sec = timeout_sec
        self._response: dict | None = None
        self._error: Exception | None = None
        self._resolved = False
        self._ready: asyncio.Event | None = None

    async def wait(self) -> dict:
        """Wait for response with timeout. Must be called from within the running loop."""
        self._ready = asyncio.Event()
        # Handle the race where resolve() was called before wait()
        if self._resolved:
            self._ready.set()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Request {self.request_id} timed out after {self.timeout_sec}s")
        if self._error:
            raise self._error
        return self._response  # type: ignore[return-value]

    def resolve(self, response: dict):
        """Resolve with response. Safe to call from same loop or different thread."""
        self._response = response
        self._resolved = True
        if self._ready is not None:
            self._ready.set()

    def reject(self, error: Exception):
        """Reject with error. Safe to call from same loop or different thread."""
        self._error = error
        self._resolved = True
        if self._ready is not None:
            self._ready.set()


class WebSocketConnectionManager:
    """Manage WebSocket connections and request-response correlation."""

    def __init__(self):
        # Agent name -> WebSocketConnection
        self.connections: dict[str, "WebSocketConnection"] = {}
        # Request ID -> PendingRequest
        self.pending_requests: dict[str, PendingRequest] = {}
        self.lock = asyncio.Lock()

    async def register_connection(self, agent_name: str, ws) -> "WebSocketConnection":
        async with self.lock:
            conn = WebSocketConnection(agent_name, ws, self)
            self.connections[agent_name] = conn
            logger.info(f"WebSocket connected: {agent_name}")
            return conn

    async def unregister_connection(self, agent_name: str):
        async with self.lock:
            if agent_name in self.connections:
                del self.connections[agent_name]
                logger.info(f"WebSocket disconnected: {agent_name}")

    def is_connected(self, agent_name: str) -> bool:
        """Check if an agent has an active WebSocket connection (non-blocking)."""
        return agent_name in self.connections

    async def send_to_agent(self, agent_name: str, message: dict):
        """Send a message to an agent via WebSocket."""
        async with self.lock:
            conn = self.connections.get(agent_name)
        if not conn:
            raise ValueError(f"Agent '{agent_name}' not connected via WebSocket")
        await conn.send_json(message)

    async def broadcast(self, message: dict, exclude: str | None = None):
        """Send a message to all connected agents."""
        async with self.lock:
            targets = [(name, conn) for name, conn in self.connections.items() if name != exclude]
        for name, conn in targets:
            try:
                await conn.send_json(message)
            except Exception as e:
                logger.warning(f"Broadcast failed for {name}: {e}")

    async def request(
        self,
        recipient: str,
        content: str,
        timeout_sec: float = 300,
        sender: str | None = None,
        thread: str = "general",
    ) -> dict:
        """Send a request and wait for response (server-side, async path)."""
        request_id = str(uuid.uuid4())
        pending = PendingRequest(request_id, timeout_sec)
        async with self.lock:
            self.pending_requests[request_id] = pending
        try:
            await self.send_to_agent(recipient, {
                "type": "request",
                "correlation_id": request_id,
                "sender": sender,
                "content": content,
                "thread": thread,
            })
            return await pending.wait()
        finally:
            async with self.lock:
                self.pending_requests.pop(request_id, None)

    async def handle_response(self, correlation_id: str, response: dict):
        """Resolve a pending request with its response."""
        async with self.lock:
            pending = self.pending_requests.get(correlation_id)
        if pending:
            pending.resolve(response)
        else:
            logger.warning(f"Received response for unknown/expired request: {correlation_id}")

    async def get_all_agents(self) -> list[str]:
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
        try:
            await self.ws.send_json(data)
        except Exception as e:
            logger.error(f"Failed to send to {self.agent_name}: {e}")
            raise

    def on_request(self, handler: Callable[[dict], Coroutine]):
        self.request_handlers["default"] = handler

    async def handle_message(self, data: dict):
        msg_type = data.get("type", "message")
        if msg_type == "response":
            correlation_id = data.get("correlation_id")
            if correlation_id:
                await self.manager.handle_response(correlation_id, data)
        elif msg_type == "request":
            handler = self.request_handlers.get("default")
            if handler:
                try:
                    response_content = await handler(data)
                    await self.send_json({
                        "type": "response",
                        "correlation_id": data.get("correlation_id"),
                        "content": response_content,
                        "status": "success",
                    })
                except Exception as e:
                    await self.send_json({
                        "type": "response",
                        "correlation_id": data.get("correlation_id"),
                        "content": str(e),
                        "status": "error",
                    })


# Global manager instance
ws_manager: WebSocketConnectionManager | None = None


def get_ws_manager() -> WebSocketConnectionManager:
    global ws_manager
    if ws_manager is None:
        ws_manager = WebSocketConnectionManager()
    return ws_manager
