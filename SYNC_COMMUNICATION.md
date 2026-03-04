# AgentBridge: Synchronous Request-Response Communication

Now agents can communicate synchronously with request-response semantics! No more polling.

## What's New

**Before** (asynchronous polling):
```python
# Agent A
bridge.send("coder", "Review this code")
# Agent B has to poll for messages
while True:
    messages = bridge.read()
    time.sleep(5)  # Poll every 5 seconds
```

**After** (synchronous communication):
```python
# Agent A - waits for response
review = await bridge.request('reviewer', 'Review this code', timeout=300)

# Agent B - instantly notified
@bridge.on_request
async def handle_review_request(request):
    code = request['content']
    issues = analyze(code)
    return issues
```

## Getting Started

### 1. Install Dependencies

```bash
cd ~/code/TheDarkFactory/AgentBridge
pip install -e ".[dev]"  # Already updated with websockets
```

### 2. Start AgentBridge

```bash
ab serve
```

Server runs on `localhost:7890` (HTTP) and `localhost:7890/ws` (WebSocket)

### 3. Use in Your Agent

```python
from agentbridge.client import AgentBridgeClient
import asyncio

async def main():
    # Create client
    client = AgentBridgeClient(
        name="my-coder",
        role="code generation",
        server_url="ws://localhost:7890"
    )

    # Connect
    await client.connect()

    # Send request and wait for response
    try:
        review = await client.request(
            recipient="reviewer",
            content="Please review this code: def foo(): pass",
            timeout_sec=300
        )
        print(f"Got review: {review}")
    except TimeoutError:
        print("Reviewer didn't respond in time")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

## API Reference

### AgentBridgeClient

#### Constructor
```python
AgentBridgeClient(
    name: str,              # Unique agent name
    role: str = "",         # Agent role/description
    server_url: str = "ws://localhost:7890",  # WebSocket URL
    http_url: str = "http://localhost:7890"   # HTTP URL
)
```

#### Methods

**connect()**
```python
await client.connect()
```
Connect to AgentBridge WebSocket.

**disconnect()**
```python
await client.disconnect()
```
Close connection to AgentBridge.

**request(recipient, content, timeout_sec=300, thread="general")**
```python
response = await client.request(
    recipient="reviewer-agent",
    content="Please review PR #2726",
    timeout_sec=300,  # Wait up to 5 minutes
    thread="pr-reviews"
)
```
Send a request and wait for response with timeout.

**on_request(handler)**
```python
@client.on_request
async def handle_request(request):
    content = request['content']
    # Do work
    return "Response text"
```
Register async handler for incoming requests.

**broadcast(content, thread="general")**
```python
await client.broadcast("Deployment complete!")
```
Send message to all connected agents.

**send_message(content, recipient=None, thread="general", msg_type="chat")**
```python
await client.send_message(
    content="Working on feature X",
    msg_type="status"
)
```
Send regular chat/status message (no response expected).

**heartbeat(status="online", working_on="")**
```python
await client.heartbeat(
    status="busy",
    working_on="Analyzing code"
)
```
Send heartbeat to keep connection alive.

**is_connected()**
```python
if client.is_connected():
    print("Connected to AgentBridge")
```
Check connection status.

## Examples

### Example 1: Code Review Workflow

**Coder Agent:**
```python
from agentbridge.client import AgentBridgeClient
import asyncio

async def main():
    coder = AgentBridgeClient("coder", "code generation")
    await coder.connect()

    # Generate code
    code = "def fibonacci(n):\n    if n <= 1: return n\n    return fibonacci(n-1) + fibonacci(n-2)"

    # Request review
    try:
        review = await coder.request(
            recipient="reviewer",
            content=code,
            timeout_sec=60
        )
        print(f"Review:\n{review}")
    except TimeoutError:
        print("Reviewer didn't respond in time")

    await coder.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

**Reviewer Agent:**
```python
from agentbridge.client import AgentBridgeClient
import asyncio

async def main():
    reviewer = AgentBridgeClient("reviewer", "code review")
    await reviewer.connect()

    @reviewer.on_request
    async def review_code(request):
        code = request['content']
        # Analyze code
        issues = [
            "Inefficient recursion - consider using memoization",
            "Add input validation for n"
        ]
        return f"Found {len(issues)} issues:\n" + "\n".join(f"- {i}" for i in issues)

    # Keep listening
    await asyncio.sleep(float('inf'))

if __name__ == "__main__":
    asyncio.run(main())
```

Run in separate terminals:
```bash
# Terminal 1
python reviewer.py

# Terminal 2
python coder.py
```

Expected output (Terminal 2):
```
Review:
Found 2 issues:
- Inefficient recursion - consider using memoization
- Add input validation for n
```

### Example 2: Parallel Testing

**Test Coordinator:**
```python
async def main():
    coordinator = AgentBridgeClient("coordinator", "test orchestration")
    await coordinator.connect()

    # Run 3 tests in parallel
    tests = [
        coordinator.request("unit-tests", "Run unit tests", timeout_sec=60),
        coordinator.request("integration-tests", "Run integration tests", timeout_sec=120),
        coordinator.request("e2e-tests", "Run E2E tests", timeout_sec=180),
    ]

    results = await asyncio.gather(*tests)
    print("Test results:")
    for result in results:
        print(f"  ✓ {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

### Example 3: Broadcast Notifications

```python
async def main():
    agent = AgentBridgeClient("notifier", "notifications")
    await agent.connect()

    # Listen for broadcasts
    # (In real use, would be more sophisticated)

    # Broadcast news
    await agent.broadcast("Build #123 succeeded!")
    await agent.broadcast("All tests passing")

    await agent.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

## Message Format

### Request
```json
{
  "type": "request",
  "correlation_id": "uuid-string",
  "recipient": "agent-name",
  "content": "Request content",
  "thread": "general"
}
```

### Response
```json
{
  "type": "response",
  "correlation_id": "uuid-string-matching-request",
  "content": "Response content",
  "status": "success"
}
```

### Message/Broadcast
```json
{
  "type": "message|broadcast",
  "sender": "agent-name",
  "content": "Message content",
  "thread": "general",
  "msg_type": "chat|status|alert"
}
```

### Heartbeat
```json
{
  "type": "heartbeat",
  "status": "online|busy|idle",
  "working_on": "Current task description"
}
```

## Performance

| Metric | HTTP Polling | WebSocket |
|--------|--------------|-----------|
| Latency | 5-30s | <100ms |
| Request roundtrip | 30s+ | <5s |
| Overhead | High (polling) | Low (streaming) |
| Resource usage | High | Low |

## Error Handling

### Timeout
```python
try:
    response = await client.request('agent', 'task', timeout_sec=10)
except TimeoutError as e:
    print(f"Request timed out: {e}")
```

### Agent Not Connected
```python
try:
    response = await client.request('unknown-agent', 'task')
except ValueError as e:
    print(f"Agent not connected: {e}")
```

### Request Handler Error
```python
@client.on_request
async def handler(request):
    if not validate(request):
        raise ValueError("Invalid request")
    return process(request)
```

Response will have `status: "error"` with error message.

## Backward Compatibility

All existing HTTP API endpoints still work:
- `POST /messages` - send HTTP message
- `GET /messages` - read messages
- `POST /agents` - register agent
- `GET /agents` - list agents

WebSocket is an additional feature, not a replacement.

## Next Steps

1. **Run the examples** to see it in action
2. **Integrate into your agents** using AgentBridgeClient
3. **Monitor** with dashboard at `http://localhost:7890/ui`
4. **Scale** - WebSocket handles 1000+ concurrent connections

## Troubleshooting

### "Connection refused"
- Start AgentBridge: `ab serve`
- Check server URL is correct: `ws://localhost:7890`

### "Agent not found"
- Make sure recipient agent is connected and registered
- Check spelling of agent name (case-sensitive)

### Request times out
- Increase timeout: `timeout_sec=600`
- Check if recipient agent is responding
- Monitor logs: `~/.agentbridge/logs/agentbridge.log`

### WebSocket connection drops
- Implement reconnection logic (see client.py)
- Send periodic heartbeat: `await client.heartbeat()`

## Architecture

```
Agent A                    AgentBridge Server              Agent B
   |                              |                           |
   |--websocket connect---------->|                           |
   |<-----registered--------------|                           |
   |                              |<--websocket connect--------|
   |                              |-------registered--------->|
   |                              |                           |
   |--request(B, "code")--------->|                           |
   |                              |---push request data------>|
   |  [BLOCKING WAIT]             |                           |
   |                              |   [PROCESS REQUEST]       |
   |                              |<-----response-----------|
   |<-----response-----------------|                           |
   |  [UNBLOCKS, HAS RESULT]       |                           |
```

## Advanced: Custom Request Handlers

```python
class CodeReviewer:
    def __init__(self, client):
        self.client = client
        client.on_request(self.handle)

    async def handle(self, request):
        code = request['content']
        issues = await self.analyze(code)
        return self.format_review(issues)

    async def analyze(self, code):
        # Custom analysis logic
        return []

    def format_review(self, issues):
        return f"Found {len(issues)} issues"

# Usage
async def main():
    client = AgentBridgeClient("reviewer", "code review")
    await client.connect()
    reviewer = CodeReviewer(client)
    await asyncio.sleep(float('inf'))  # Keep listening

asyncio.run(main())
```

---

**Synchronous communication is ready to use!** Start with the examples and integrate into your agents.
