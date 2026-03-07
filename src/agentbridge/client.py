"""Python client for AgentBridge with synchronous request-response communication."""

import asyncio
import json
import logging
import uuid
from typing import Callable, Any, Coroutine
import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger("agentbridge.client")


class AgentBridgeClient:
    """Synchronous-style client for AgentBridge.

    Supports both HTTP polling (legacy) and WebSocket (new synchronous mode).
    """

    def __init__(
        self,
        name: str,
        role: str = "",
        server_url: str = "ws://localhost:7890",
        http_url: str = "http://localhost:7890",
    ):
        self.name = name
        self.role = role
        self.server_url = server_url
        self.http_url = http_url
        self.ws: WebSocketClientProtocol | None = None
        self.pending_requests: dict[str, asyncio.Future] = {}
        self.request_handler: Callable[[dict], Coroutine] | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._read_task: asyncio.Task | None = None
        self._running = False

    async def connect(self, capabilities: list[str] | None = None) -> None:
        """Connect to AgentBridge via WebSocket."""
        try:
            ws_url = self.server_url.replace("http://", "ws://").replace("https://", "wss://")
            if not ws_url.startswith("ws"):
                ws_url = f"ws://{ws_url}"

            self.ws = await websockets.connect(f"{ws_url}/ws")
            self._running = True

            await self.ws.send(json.dumps({
                "type": "register",
                "name": self.name,
                "role": self.role,
                "capabilities": capabilities or [],
            }))

            self._read_task = asyncio.create_task(self._read_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(f"Connected to AgentBridge as {self.name}")
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from AgentBridge."""
        self._running = False
        if self._read_task:
            self._read_task.cancel()
        if hasattr(self, "_heartbeat_task") and self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.ws:
            await self.ws.close()
        logger.info(f"Disconnected from AgentBridge")

    async def _heartbeat_loop(self) -> None:
        """Send automatic heartbeat every 30s to prevent TTL expiry."""
        while self._running:
            await asyncio.sleep(30)
            if self._running:
                try:
                    await self.heartbeat()
                except Exception:
                    pass

    async def _read_loop(self) -> None:
        """Read messages from WebSocket."""
        if not self.ws:
            return

        try:
            async for message_str in self.ws:
                message = json.loads(message_str)
                msg_type = message.get("type")

                if msg_type == "response":
                    # Response to our request
                    correlation_id = message.get("correlation_id")
                    if correlation_id in self.pending_requests:
                        future = self.pending_requests.pop(correlation_id)
                        if message.get("status") == "success":
                            future.set_result(message.get("content"))
                        else:
                            future.set_exception(
                                RuntimeError(message.get("content", "Unknown error"))
                            )

                elif msg_type == "request":
                    # Incoming request
                    if self.request_handler:
                        try:
                            response_content = await self.request_handler(message)
                            response = {
                                "type": "response",
                                "correlation_id": message.get("correlation_id"),
                                "content": response_content,
                                "status": "success",
                            }
                            await self._send_json(response)
                        except Exception as e:
                            error_response = {
                                "type": "response",
                                "correlation_id": message.get("correlation_id"),
                                "content": str(e),
                                "status": "error",
                            }
                            await self._send_json(error_response)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in read loop: {e}")
            self._running = False

    async def _send_json(self, data: dict) -> None:
        """Send JSON to WebSocket."""
        if not self.ws:
            raise RuntimeError("Not connected")
        await self.ws.send(json.dumps(data))

    async def request(
        self,
        recipient: str,
        content: str,
        timeout_sec: float = 300,
        thread: str = "general",
    ) -> Any:
        """Send a request and wait for response.

        Args:
            recipient: Name of agent to send request to
            content: Request content/message
            timeout_sec: Timeout in seconds
            thread: Thread name for grouping

        Returns:
            Response content from recipient

        Raises:
            TimeoutError: If response not received within timeout
            RuntimeError: If recipient is not connected
        """
        if not self.ws:
            raise RuntimeError("Not connected")

        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.Future()
        self.pending_requests[request_id] = future

        try:
            # Send request
            message = {
                "type": "request",
                "correlation_id": request_id,
                "recipient": recipient,
                "content": content,
                "thread": thread,
            }
            await self._send_json(message)

            # Wait for response
            response = await asyncio.wait_for(future, timeout=timeout_sec)
            return response

        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            raise TimeoutError(f"Request to {recipient} timed out after {timeout_sec}s")
        except Exception as e:
            self.pending_requests.pop(request_id, None)
            raise

    async def broadcast(self, content: str, thread: str = "general") -> None:
        """Send a broadcast message (fire-and-forget)."""
        if not self.ws:
            raise RuntimeError("Not connected")

        message = {
            "type": "broadcast",
            "sender": self.name,
            "content": content,
            "thread": thread,
        }
        await self._send_json(message)

    def on_request(
        self, handler: Callable[[dict], Coroutine[Any, Any, Any]]
    ) -> None:
        """Register handler for incoming requests.

        Handler should be an async function that takes a message dict
        and returns a response (str or dict).
        """
        self.request_handler = handler

    async def send_message(
        self,
        content: str,
        recipient: str | None = None,
        thread: str = "general",
        msg_type: str = "chat",
    ) -> None:
        """Send a regular chat message (no response expected)."""
        if not self.ws:
            raise RuntimeError("Not connected")

        message = {
            "type": "message",
            "sender": self.name,
            "content": content,
            "recipient": recipient,
            "thread": thread,
            "msg_type": msg_type,
        }
        await self._send_json(message)

    async def heartbeat(
        self, status: str = "online", working_on: str = ""
    ) -> None:
        """Send heartbeat to keep connection alive."""
        if not self.ws:
            raise RuntimeError("Not connected")

        message = {
            "type": "heartbeat",
            "status": status,
            "working_on": working_on,
        }
        await self._send_json(message)

    def is_connected(self) -> bool:
        """Check if connected to AgentBridge."""
        return self.ws is not None and self._running


async def example_usage():
    """Example of how to use the client."""
    # Agent 1: Coder
    coder = AgentBridgeClient("coder", "code generation")
    await coder.connect()

    # Agent 2: Reviewer
    reviewer = AgentBridgeClient("reviewer", "code review")
    await reviewer.connect()

    # Reviewer listens for code review requests
    async def review_code(request):
        code = request.get("content", "")
        # Simulate review work
        await asyncio.sleep(1)
        return f"Review: Found 2 issues in code"

    reviewer.on_request(review_code)

    # Coder sends request and waits for response
    try:
        review = await coder.request("reviewer", "Review this code: def foo(): pass")
        print(f"Got review: {review}")
    except TimeoutError:
        print("Review took too long")

    await coder.disconnect()
    await reviewer.disconnect()


if __name__ == "__main__":
    asyncio.run(example_usage())
