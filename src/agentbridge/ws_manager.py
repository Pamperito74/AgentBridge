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
        # Per-agent waiters for incoming requests (used by wait_for_request MCP tool)
        self.incoming_waiters: dict[str, PendingRequest] = {}
        self.incoming_lock = asyncio.Lock()

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

    async def broadcast(
        self,
        message: dict,
        exclude: str | None = None,
        thread: str | None = None,
        event_type: str | None = None,
    ):
        """Send a message to all connected agents, respecting subscription filters."""
        async with self.lock:
            targets = [(name, conn) for name, conn in self.connections.items() if name != exclude]
        for name, conn in targets:
            if not conn.wants_event(thread=thread, event_type=event_type):
                continue
            try:
                await conn.send_json(message)
            except Exception as e:
                logger.warning(f"Broadcast failed for {name}: {e}")

    async def handle_stream_event(self, sender: str, event_type: str, data: dict):
        """Broadcast a streaming event to all connections that want it."""
        thread = data.get("thread", "general")
        await self.broadcast(data, exclude=sender, thread=thread, event_type=event_type)

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

    async def deliver_and_wait(
        self,
        recipient: str,
        message: dict,
        correlation_id: str,
        timeout_sec: float,
    ) -> dict:
        """Register a pending future, deliver via WS, and await the response.

        Runs entirely in the uvicorn event loop so the MCP sync thread can block
        on run_coroutine_threadsafe(...).result() without freezing the loop.
        """
        pending = PendingRequest(correlation_id, timeout_sec)
        async with self.lock:
            self.pending_requests[correlation_id] = pending
        try:
            await self.send_to_agent(recipient, message)
            return await pending.wait()
        finally:
            async with self.lock:
                self.pending_requests.pop(correlation_id, None)

    async def register_incoming_waiter(self, agent_name: str, timeout_sec: float) -> PendingRequest:
        """Register this agent as waiting for the next incoming request.

        Returns the PendingRequest to await. The caller must clear it when done.
        """
        pending = PendingRequest(f"incoming:{agent_name}", timeout_sec)
        async with self.incoming_lock:
            self.incoming_waiters[agent_name] = pending
        return pending

    async def clear_incoming_waiter(self, agent_name: str):
        """Remove the incoming waiter for this agent (idempotent)."""
        async with self.incoming_lock:
            self.incoming_waiters.pop(agent_name, None)

    async def notify_incoming_request(self, agent_name: str, request_data: dict) -> bool:
        """If agent_name has a registered wait_for_request() waiter, resolve it immediately.

        Returns True if a waiter was found and notified.
        """
        async with self.incoming_lock:
            pending = self.incoming_waiters.pop(agent_name, None)
        if pending:
            pending.resolve(request_data)
            logger.debug(f"Notified wait_for_request waiter for '{agent_name}'")
            return True
        return False

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
        # Event subscription filters (None = receive all)
        self._subscribed_threads: set[str] | None = None    # None = all threads
        self._subscribed_event_types: set[str] | None = None  # None = all types

    async def send_json(self, data: dict):
        try:
            await self.ws.send_json(data)
        except Exception as e:
            logger.error(f"Failed to send to {self.agent_name}: {e}")
            raise

    def on_request(self, handler: Callable[[dict], Coroutine]):
        self.request_handlers["default"] = handler

    def wants_event(self, thread: str | None = None, event_type: str | None = None) -> bool:
        """Return True if this connection should receive an event with the given filters."""
        if self._subscribed_threads is not None and thread is not None:
            if thread not in self._subscribed_threads:
                return False
        if self._subscribed_event_types is not None and event_type is not None:
            if event_type not in self._subscribed_event_types:
                return False
        return True

    def subscribe(self, threads: list[str] | None = None, event_types: list[str] | None = None):
        """Narrow the event subscription. Pass None to subscribe to all."""
        self._subscribed_threads = set(threads) if threads is not None else None
        self._subscribed_event_types = set(event_types) if event_types is not None else None

    def unsubscribe_all(self):
        """Reset to receive all events (default)."""
        self._subscribed_threads = None
        self._subscribed_event_types = None

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
        elif msg_type == "subscribe":
            # Client requests filtered event delivery
            threads = data.get("threads")  # list[str] | null
            event_types = data.get("event_types")  # list[str] | null
            self.subscribe(threads=threads, event_types=event_types)
            await self.send_json({"type": "subscribed", "threads": threads, "event_types": event_types})
        elif msg_type == "unsubscribe":
            self.unsubscribe_all()
            await self.send_json({"type": "unsubscribed"})
        elif msg_type in ("stream_start", "stream_chunk", "stream_end"):
            # Forward streaming events to the manager for broadcast
            await self.manager.handle_stream_event(self.agent_name, msg_type, data)


# Global manager instance
ws_manager: WebSocketConnectionManager | None = None


def get_ws_manager() -> WebSocketConnectionManager:
    global ws_manager
    if ws_manager is None:
        ws_manager = WebSocketConnectionManager()
    return ws_manager
