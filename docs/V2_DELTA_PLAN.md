# AgentBridge v2 Delta: Complete Implementation Plan

> Produced 2026-03-11. Covers 7 gaps against production readiness.
> Every change is additive. Zero v1 breaking changes.

---

## Table of Contents

1. [Rate Limiting](#1-rate-limiting)
2. [Structured Message Content](#2-structured-message-content)
3. [Agent Memory Layer](#3-agent-memory-layer)
4. [Streaming Responses](#4-streaming-responses)
5. [Event Subscriptions](#5-event-subscriptions)
6. [Docker Deployment](#6-docker-deployment)
7. [Redis Pub/Sub](#7-redis-pubsub)
8. [Implementation Order](#8-implementation-order)
9. [File Inventory](#9-file-inventory)

---

## 1. Rate Limiting

### New file: `src/agentbridge/rate_limit.py`

```python
"""In-memory token-bucket rate limiter. No external dependencies."""

import threading
import time
from collections import defaultdict


class TokenBucket:
    __slots__ = ("rate", "burst", "tokens", "last", "lock")

    def __init__(self, rate: float, burst: int):
        self.rate = rate        # tokens refilled per second
        self.burst = burst      # max tokens (also initial fill)
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, n: int = 1) -> bool:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

    @property
    def remaining(self) -> float:
        with self.lock:
            now = time.monotonic()
            return min(self.burst, self.tokens + (now - self.last) * self.rate)


class RateLimiter:
    """Per-key token bucket rate limiter.

    Environment config:
        AGENTBRIDGE_RATE_LIMIT  - tokens per second per agent (default 10, 0 = disabled)
        AGENTBRIDGE_RATE_BURST  - max burst capacity (default 30)
    """

    def __init__(self, rate: float = 10.0, burst: int = 30):
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self.enabled = rate > 0

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        if not self.enabled:
            return True
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(self.rate, self.burst)
            bucket = self._buckets[key]
        return bucket.consume()

    def remaining(self, key: str) -> float:
        """Return approximate remaining tokens for a key."""
        with self._lock:
            bucket = self._buckets.get(key)
        if not bucket:
            return float(self.burst)
        return bucket.remaining
```

### Changes to `server.py`

**Import (add near top, after existing imports on line 21):**

```python
from .rate_limit import RateLimiter
```

**Initialization (add after `schema_registry = SchemaRegistry()` on line 128):**

```python
_rate_limiter = RateLimiter(
    rate=float(os.environ.get("AGENTBRIDGE_RATE_LIMIT", "10")),
    burst=int(os.environ.get("AGENTBRIDGE_RATE_BURST", "30")),
)
```

**Guard function (add after `_notify_incoming_waiter` function, ~line 231):**

```python
def _check_rate_limit(key: str):
    """Raise 429 if rate limit exceeded for this sender/agent."""
    if not _rate_limiter.check(key):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded for '{key}'")
```

**Apply to HTTP endpoints:**

- `http_send_message` (line 534): Add `_check_rate_limit(body.sender)` as first line
- `http_send_event` (line 625): Add `_check_rate_limit(body.actor_id)` as first line
- `http_heartbeat` (line 446): Add `_check_rate_limit(name)` as first line

**Apply to WebSocket loop (inside `while True:` at line 1016):**

After `msg_type = message.get("type")` on line 1019, add:

```python
            if msg_type in ("request", "message", "broadcast", "stream_start") and not _rate_limiter.check(agent_name):
                await websocket.send_json({"type": "error", "code": "RATE_LIMITED", "detail": "Rate limit exceeded"})
                continue
```

### Changes to `cli.py`

No changes. CLI commands are low-frequency.

---

## 2. Structured Message Content

### Changes to `models.py`

**Add `ContentBlock` class after `Artifact` (after line 27):**

```python
class ContentBlock(BaseModel):
    """Typed content block within a message."""
    type: str  # "text" | "code" | "json" | "image" | "tool_result" | "error"
    content: str
    language: str | None = None   # for type="code" (e.g. "python", "sql")
    title: str | None = None      # optional label for UI rendering
    metadata: dict = Field(default_factory=dict)
```

**Add `blocks` field to `Message` class (after `artifacts` on line 46):**

```python
    blocks: list[ContentBlock] = Field(default_factory=list)
```

### Changes to `store.py`

**Schema migration in `_migrate()` (after `claimed_by` migration, line 188-191):**

Add to the `msg_cols` migration list:

```python
                ("blocks", "ALTER TABLE messages ADD COLUMN blocks TEXT DEFAULT '[]'"),
```

**`add_message()` signature change (line 529-538):**

Add `blocks` parameter:

```python
    def add_message(
        self, sender: str, content: str, recipient: str | None = None,
        thread: str = "general", msg_type: str = "chat",
        artifacts: list[dict] | None = None,
        blocks: list[dict] | None = None,       # <-- NEW
        actor_id: str | None = None, actor_type: str = "agent",
        target_id: str | None = None, target_type: str | None = None,
        event_type: str = "note.text", metadata: dict | None = None,
        labels: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> Message:
```

**Inside `add_message()`, construct block objects (after line 539):**

```python
        block_objs = [ContentBlock(**b) for b in (blocks or [])]
```

**Pass to Message constructor (line 540-548), add:**

```python
            blocks=block_objs,
```

**INSERT statement (line 550-560) -- expand to 18 columns:**

Change the INSERT to include `blocks`:

```sql
INSERT INTO messages (id, actor_id, actor_type, target_id, target_type, event_type, metadata, labels, sender, recipient, thread, content, msg_type, mentions, artifacts, blocks, correlation_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

Add to the values tuple (after artifacts serialization):

```python
                    json.dumps([b.model_dump() for b in msg.blocks]),
```

**`read_messages()` SELECT (line 581):**

Expand to include `blocks` at index 17 (after `correlation_id` at index 15, before `timestamp` at index 16):

```sql
SELECT id, actor_id, actor_type, target_id, target_type, event_type, metadata, labels, sender, recipient, thread, content, msg_type, mentions, artifacts, blocks, correlation_id, timestamp FROM messages WHERE 1=1
```

**Message reconstruction (lines 621-634):**

Update column indices. After expanding, `blocks` is at index 15, `correlation_id` at 16, `timestamp` at 17:

```python
            Message(
                id=r[0], actor_id=r[1], actor_type=r[2] or "agent",
                target_id=r[3], target_type=r[4], event_type=r[5] or "note.text",
                metadata=json.loads(r[6]) if r[6] else {},
                labels=json.loads(r[7]) if r[7] else [],
                sender=r[8], recipient=r[9], thread=r[10], content=r[11],
                msg_type=r[12] or "chat",
                mentions=json.loads(r[13]) if r[13] else [],
                artifacts=[Artifact(**a) for a in json.loads(r[14])] if r[14] else [],
                blocks=[ContentBlock(**b) for b in json.loads(r[15])] if r[15] else [],
                correlation_id=r[16],
                timestamp=datetime.fromisoformat(r[17]),
            )
```

**`add_message_async()` (line 238-253):**

Add `blocks: list[dict] | None = None` to signature and pass through.

**Index (add to `_ensure_indexes`):**

No new index needed. Blocks are not queried by; they are payload.

### Changes to `server.py`

**Add Pydantic model after `ArtifactRequest` (line 302):**

```python
class ContentBlockRequest(BaseModel):
    type: Literal["text", "code", "json", "image", "tool_result", "error"]
    content: str = Field(min_length=1, max_length=50000)
    language: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=256)
    metadata: dict = Field(default_factory=dict)
```

**Add `blocks` to `SendMessageRequest` (after `artifacts` on line 310):**

```python
    blocks: list[ContentBlockRequest] | None = None
```

**`http_send_message` (line 534-587):**

Pass `blocks` to `add_message_async`:

```python
        blocks=[b.model_dump() for b in body.blocks] if body.blocks else None,
```

**`http_send_event` (line 625-649):**

Add `blocks` param to `EventWriteRequest` (optional, defaults `None`). Pass through.

**MCP `send` tool (line 1143-1175):**

Add `blocks: list[dict] | None = None` parameter. Pass through to `add_message`.

**Import `ContentBlock` in `store.py` (line 14):**

```python
from .models import Agent, Artifact, ContentBlock, Message, Thread, ThreadSummary
```

---

## 3. Agent Memory Layer

### Changes to `store.py`

**New table in `_create_tables()` (after `settings` table, before the closing `""")`):**

```sql
                CREATE TABLE IF NOT EXISTS agent_memory (
                    agent_name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY (agent_name, key)
                );
```

**New index in `_ensure_indexes()`:**

```sql
                CREATE INDEX IF NOT EXISTS idx_agent_memory_agent
                    ON agent_memory(agent_name);
                CREATE INDEX IF NOT EXISTS idx_agent_memory_expires
                    ON agent_memory(expires_at);
```

**Add to `_prune()` (after agent_requests timeout update, line 222):**

```python
            # Expire agent memory entries with TTL
            self._conn.execute(
                "DELETE FROM agent_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,)
            )
```

**New sync methods (add after `clear_board` method, line 907):**

```python
    # --- Agent Memory (KV Store) ---

    def memory_set(self, agent_name: str, key: str, value: str, ttl_sec: int | None = None) -> None:
        """Set a key-value pair for an agent. Optional TTL in seconds."""
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl_sec)).isoformat() if ttl_sec else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO agent_memory (agent_name, key, value, updated_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(agent_name, key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at,
                       expires_at = excluded.expires_at""",
                (agent_name, key, value, now.isoformat(), expires_at),
            )
            self._conn.commit()

    def memory_get(self, agent_name: str, key: str) -> str | None:
        """Get a value by key for an agent. Returns None if not found or expired."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM agent_memory WHERE agent_name = ? AND key = ? AND (expires_at IS NULL OR expires_at > ?)",
                (agent_name, key, now_iso),
            ).fetchone()
        return row[0] if row else None

    def memory_delete(self, agent_name: str, key: str) -> bool:
        """Delete a single key. Returns True if the key existed."""
        with self._lock:
            result = self._conn.execute(
                "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?",
                (agent_name, key),
            )
            self._conn.commit()
        return result.rowcount > 0

    def memory_list(self, agent_name: str, prefix: str | None = None) -> list[dict]:
        """List all keys for an agent. Optional prefix filter."""
        now_iso = datetime.now(timezone.utc).isoformat()
        query = "SELECT key, value, updated_at, expires_at FROM agent_memory WHERE agent_name = ? AND (expires_at IS NULL OR expires_at > ?)"
        params: list = [agent_name, now_iso]
        if prefix:
            query += " AND key LIKE ?"
            params.append(prefix + "%")
        query += " ORDER BY key ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"key": r[0], "value": r[1], "updated_at": r[2], "expires_at": r[3]}
            for r in rows
        ]

    def memory_clear(self, agent_name: str) -> int:
        """Delete all memory for an agent. Returns count deleted."""
        with self._lock:
            result = self._conn.execute(
                "DELETE FROM agent_memory WHERE agent_name = ?",
                (agent_name,),
            )
            self._conn.commit()
        return result.rowcount

    # Async wrappers

    async def memory_set_async(self, agent_name: str, key: str, value: str, ttl_sec: int | None = None):
        await self._run_in_thread(self.memory_set, agent_name, key, value, ttl_sec)

    async def memory_get_async(self, agent_name: str, key: str) -> str | None:
        return await self._run_in_thread(self.memory_get, agent_name, key)

    async def memory_delete_async(self, agent_name: str, key: str) -> bool:
        return await self._run_in_thread(self.memory_delete, agent_name, key)

    async def memory_list_async(self, agent_name: str, prefix: str | None = None) -> list[dict]:
        return await self._run_in_thread(self.memory_list, agent_name, prefix)

    async def memory_clear_async(self, agent_name: str) -> int:
        return await self._run_in_thread(self.memory_clear, agent_name)
```

### Changes to `server.py`

**New Pydantic models (add after `AgentRespondRequest`, ~line 377):**

```python
class MemorySetRequest(BaseModel):
    value: str = Field(min_length=1, max_length=100000)
    ttl_sec: int | None = Field(default=None, ge=1, le=31536000)  # max 1 year
```

**New HTTP endpoints (add after `/agents/{name}/requests/{request_id}/respond`, ~line 528):**

```python
# --- Agent Memory ---

@http_app.get("/agents/{name}/memory")
async def http_memory_list(name: str, prefix: str | None = Query(None)):
    """List memory keys for an agent. Optional prefix filter."""
    entries = await get_store().memory_list_async(name, prefix)
    return entries


@http_app.get("/agents/{name}/memory/{key:path}")
async def http_memory_get(name: str, key: str):
    """Get a single memory value."""
    value = await get_store().memory_get_async(name, key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"Key '{key}' not found for agent '{name}'")
    return {"key": key, "value": value}


@http_app.put("/agents/{name}/memory/{key:path}")
async def http_memory_set(name: str, key: str, body: MemorySetRequest):
    """Set a memory key-value pair."""
    await get_store().memory_set_async(name, key, body.value, body.ttl_sec)
    return {"ok": True, "key": key, "ttl_sec": body.ttl_sec}


@http_app.delete("/agents/{name}/memory/{key:path}")
async def http_memory_delete(name: str, key: str):
    """Delete a single memory key."""
    deleted = await get_store().memory_delete_async(name, key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Key '{key}' not found for agent '{name}'")
    return {"ok": True, "key": key}


@http_app.delete("/agents/{name}/memory")
async def http_memory_clear(name: str):
    """Delete all memory for an agent."""
    count = await get_store().memory_clear_async(name)
    return {"ok": True, "deleted": count}
```

**New MCP tools (add after `thread_summary` tool, ~line 1764):**

```python
@mcp.tool()
def memory_set(agent_name: str, key: str, value: str, ttl_sec: int | None = None) -> str:
    """Store a key-value pair in agent memory. Persists across sessions.

    Use key prefixes for organization:
      - 'ctx:' for short-term context (set ttl_sec for auto-expiry)
      - 'pref:' for preferences
      - 'state:' for workflow state

    ttl_sec: auto-delete after this many seconds. None = permanent.
    """
    if _remote_url():
        body = {"value": value}
        if ttl_sec is not None:
            body["ttl_sec"] = ttl_sec
        _rhttp("PUT", f"/agents/{agent_name}/memory/{key}", body)
        ttl_str = f" (TTL {ttl_sec}s)" if ttl_sec else " (permanent)"
        return f"Stored: {agent_name}/{key}{ttl_str}"
    get_store().memory_set(agent_name, key, value, ttl_sec)
    ttl_str = f" (TTL {ttl_sec}s)" if ttl_sec else " (permanent)"
    return f"Stored: {agent_name}/{key}{ttl_str}"


@mcp.tool()
def memory_get(agent_name: str, key: str) -> str:
    """Retrieve a value from agent memory. Returns the value or 'Not found'."""
    if _remote_url():
        try:
            result = _rhttp("GET", f"/agents/{agent_name}/memory/{key}")
            return result.get("value", "Not found")
        except RuntimeError:
            return "Not found"
    value = get_store().memory_get(agent_name, key)
    if value is None:
        return "Not found"
    return value


@mcp.tool()
def memory_list(agent_name: str, prefix: str | None = None) -> str:
    """List all memory keys for an agent. Optional prefix filter (e.g. 'ctx:')."""
    if _remote_url():
        params = {"prefix": prefix} if prefix else {}
        entries = _rhttp("GET", f"/agents/{agent_name}/memory", params=params)
        if not entries:
            return "No memory entries."
        lines = []
        for e in (entries if isinstance(entries, list) else []):
            ttl = f" [expires {e['expires_at']}]" if e.get("expires_at") else ""
            lines.append(f"  {e['key']}: {e['value'][:80]}{ttl}")
        return "\n".join(lines)
    entries = get_store().memory_list(agent_name, prefix)
    if not entries:
        return "No memory entries."
    lines = []
    for e in entries:
        ttl = f" [expires {e['expires_at']}]" if e.get("expires_at") else ""
        lines.append(f"  {e['key']}: {e['value'][:80]}{ttl}")
    return "\n".join(lines)


@mcp.tool()
def memory_delete(agent_name: str, key: str) -> str:
    """Delete a key from agent memory."""
    if _remote_url():
        _rhttp("DELETE", f"/agents/{agent_name}/memory/{key}")
        return f"Deleted: {agent_name}/{key}"
    deleted = get_store().memory_delete(agent_name, key)
    return f"Deleted: {agent_name}/{key}" if deleted else f"Key not found: {agent_name}/{key}"
```

### Changes to `client.py`

No changes. Client uses HTTP or WS. Memory endpoints are HTTP-only -- agents call them via the existing `requests` library or `_rhttp`.

---

## 4. Streaming Responses

### WebSocket Protocol

Three new message types flow over the existing `/ws` connection:

```
CLIENT -> SERVER:
{"type": "stream_start",  "correlation_id": "uuid", "recipient": "agent-b", "thread": "general"}
{"type": "stream_chunk",  "correlation_id": "uuid", "content": "partial token"}
{"type": "stream_chunk",  "correlation_id": "uuid", "content": " more text"}
{"type": "stream_end",    "correlation_id": "uuid", "content": "full assembled text", "recipient": "agent-b", "thread": "general"}
```

Rules:
- `stream_start` and `stream_chunk` are **transient** -- no DB write, no SSE persistence
- `stream_end` is the **commit point** -- persists message to DB, resolves `PendingRequest`, broadcasts SSE
- `stream_start` through `stream_end` share the same `correlation_id`
- If an agent disconnects mid-stream, the stream is abandoned (no cleanup needed)

### Changes to `server.py`

**WebSocket loop (inside `while True:` at line 1016). Add after the `heartbeat` handler at line 1093:**

```python
            elif msg_type == "stream_start":
                # Forward to recipient + SSE. No DB write.
                correlation_id = message.get("correlation_id")
                recipient = message.get("recipient")
                thread = message.get("thread", "general")
                payload = {
                    "type": "stream_start",
                    "correlation_id": correlation_id,
                    "sender": agent_name,
                    "thread": thread,
                }
                _broadcast_sse("stream_start", payload)
                if recipient:
                    try:
                        await manager.send_to_agent(recipient, payload)
                    except ValueError:
                        pass

            elif msg_type == "stream_chunk":
                # Forward to recipient + SSE. No DB write.
                correlation_id = message.get("correlation_id")
                recipient = message.get("recipient")
                payload = {
                    "type": "stream_chunk",
                    "correlation_id": correlation_id,
                    "sender": agent_name,
                    "content": message.get("content", ""),
                }
                _broadcast_sse("stream_chunk", payload)
                if recipient:
                    try:
                        await manager.send_to_agent(recipient, payload)
                    except ValueError:
                        pass

            elif msg_type == "stream_end":
                # Commit: persist full message, resolve PendingRequest, broadcast SSE.
                correlation_id = message.get("correlation_id")
                content = message.get("content", "")
                recipient = message.get("recipient")
                thread = message.get("thread", "general")
                blocks_raw = message.get("blocks")

                stored = get_store().add_message(
                    sender=agent_name,
                    content=content,
                    recipient=recipient,
                    thread=thread,
                    msg_type="response",
                    correlation_id=correlation_id,
                    blocks=blocks_raw,
                )
                result = stored.model_dump(mode="json")
                _broadcast_sse("stream_end", {
                    "correlation_id": correlation_id,
                    "sender": agent_name,
                    "message_id": stored.id,
                })
                _broadcast_sse("message", result)

                # Resolve any PendingRequest waiting on this correlation_id
                if correlation_id:
                    await manager.handle_response(correlation_id, {
                        "content": content,
                        "status": "success",
                    })

                # Also deliver the final message to recipient via WS
                if recipient:
                    try:
                        await manager.send_to_agent(recipient, {
                            "type": "message",
                            **result,
                        })
                    except ValueError:
                        pass
```

### Changes to `client.py`

**New callback registration (add after `on_request` method, line 216):**

```python
    def on_stream(
        self, handler: Callable[[dict], Coroutine[Any, Any, None]]
    ) -> Callable:
        """Register handler for incoming stream chunks.

        Handler receives dicts with type='stream_start', 'stream_chunk', or 'stream_end'.
        """
        self.stream_handler = handler
        return handler
```

**Add `stream_handler` to `__init__` (after `self.request_handler` on line 33):**

```python
        self.stream_handler: Callable[[dict], Coroutine] | None = None
```

**Handle stream events in `_read_loop` (after the `elif msg_type == "request":` block, line 105-124):**

```python
                elif msg_type in ("stream_start", "stream_chunk", "stream_end"):
                    if self.stream_handler:
                        try:
                            await self.stream_handler(message)
                        except Exception as e:
                            logger.warning(f"Stream handler error: {e}")
                    # stream_end also resolves the pending request future
                    if msg_type == "stream_end":
                        correlation_id = message.get("correlation_id")
                        if correlation_id in self.pending_requests:
                            future = self.pending_requests.pop(correlation_id)
                            future.set_result(message.get("content"))
```

**New method `stream_response` (add after `broadcast` method, line 200):**

```python
    async def stream_response(
        self,
        correlation_id: str,
        content_iterable,
        recipient: str | None = None,
        thread: str = "general",
        blocks: list[dict] | None = None,
    ) -> str:
        """Stream a response token-by-token, then commit.

        content_iterable: async iterable yielding string chunks.
        Returns the full assembled content.
        """
        if not self.ws:
            raise RuntimeError("Not connected")

        await self._send_json({
            "type": "stream_start",
            "correlation_id": correlation_id,
            "recipient": recipient,
            "thread": thread,
        })

        full_content = ""
        async for chunk in content_iterable:
            full_content += chunk
            await self._send_json({
                "type": "stream_chunk",
                "correlation_id": correlation_id,
                "content": chunk,
                "recipient": recipient,
            })

        end_payload = {
            "type": "stream_end",
            "correlation_id": correlation_id,
            "content": full_content,
            "recipient": recipient,
            "thread": thread,
        }
        if blocks:
            end_payload["blocks"] = blocks
        await self._send_json(end_payload)

        return full_content
```

### No HTTP streaming endpoint

Streaming is WebSocket-only. HTTP agents continue to POST complete messages. This is deliberate:
- SSE is already used for dashboard consumption; adding server-to-client streaming for HTTP agents would require a separate SSE channel per correlation_id, which adds complexity for marginal value.
- The dashboard SSE feed already receives `stream_start`/`stream_chunk`/`stream_end` events for live rendering.

---

## 5. Event Subscriptions

### WebSocket Protocol Addition

```
CLIENT -> SERVER:
{"type": "subscribe", "filters": {"event_types": ["task.*", "run.result"], "threads": ["ops"], "mentions": true}}
{"type": "unsubscribe"}
```

`subscribe` replaces any previous subscription. `unsubscribe` clears all filters (back to receive-everything). An agent with no subscription receives all messages (backward compatible).

### Changes to `ws_manager.py`

**Add subscription state to `WebSocketConnection.__init__` (after line 201):**

```python
        self.subscriptions: dict = {
            "event_types": [],   # list of patterns (supports trailing * wildcard)
            "threads": [],       # list of thread names
            "mentions": False,   # if True, receive messages where agent is @mentioned
        }
```

**Add filter method to `WebSocketConnection` (after `handle_message`, ~line 236):**

```python
    def matches_filter(self, message: dict) -> bool:
        """Check if this message passes the connection's subscription filter.

        No filters set = receive everything (v1 backward compat).
        """
        subs = self.subscriptions
        has_filters = bool(subs["event_types"] or subs["threads"] or subs["mentions"])
        if not has_filters:
            return True  # no filters = receive all

        # Check event_type patterns
        event_type = message.get("event_type", "note.text")
        for pattern in subs["event_types"]:
            if pattern.endswith("*"):
                if event_type.startswith(pattern[:-1]):
                    return True
            elif event_type == pattern:
                return True

        # Check thread
        if subs["threads"] and message.get("thread") in subs["threads"]:
            return True

        # Check @mentions
        if subs["mentions"] and self.agent_name in (message.get("mentions") or []):
            return True

        return False
```

**Modify `WebSocketConnectionManager.broadcast` (line 94-102):**

Replace the method body:

```python
    async def broadcast(self, message: dict, exclude: str | None = None):
        """Send a message to all connected agents that match their subscription filters."""
        async with self.lock:
            targets = [
                (name, conn) for name, conn in self.connections.items()
                if name != exclude and conn.matches_filter(message)
            ]
        for name, conn in targets:
            try:
                await conn.send_json(message)
            except Exception as e:
                logger.warning(f"Broadcast failed for {name}: {e}")
```

### Changes to `server.py`

**WebSocket loop -- add `subscribe`/`unsubscribe` handlers (after `heartbeat` handler, line 1093):**

```python
            elif msg_type == "subscribe":
                filters = message.get("filters", {})
                conn.subscriptions = {
                    "event_types": filters.get("event_types", []),
                    "threads": filters.get("threads", []),
                    "mentions": bool(filters.get("mentions", False)),
                }
                await websocket.send_json({
                    "type": "subscribed",
                    "filters": conn.subscriptions,
                })

            elif msg_type == "unsubscribe":
                conn.subscriptions = {"event_types": [], "threads": [], "mentions": False}
                await websocket.send_json({"type": "unsubscribed"})
```

### Changes to `client.py`

**New method (add after `heartbeat`, ~line 251):**

```python
    async def subscribe(
        self,
        event_types: list[str] | None = None,
        threads: list[str] | None = None,
        mentions: bool = False,
    ) -> None:
        """Set subscription filters. Only matching messages will be delivered.

        Call with no arguments or unsubscribe() to receive everything again.
        """
        if not self.ws:
            raise RuntimeError("Not connected")
        await self._send_json({
            "type": "subscribe",
            "filters": {
                "event_types": event_types or [],
                "threads": threads or [],
                "mentions": mentions,
            },
        })

    async def unsubscribe(self) -> None:
        """Clear all subscription filters. Receive all messages."""
        if not self.ws:
            raise RuntimeError("Not connected")
        await self._send_json({"type": "unsubscribe"})
```

**Handle `subscribed`/`unsubscribed` in `_read_loop` (after stream handlers):**

```python
                elif msg_type in ("subscribed", "unsubscribed"):
                    logger.debug(f"Subscription update: {message}")
```

---

## 6. Docker Deployment

### New file: `Dockerfile`

```dockerfile
FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies first (cacheable layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source
COPY src/ src/
RUN pip install --no-cache-dir -e .

# Data volume mount point
VOLUME /data

ENV AGENTBRIDGE_HOST=0.0.0.0
ENV AGENTBRIDGE_PORT=7890
ENV AGENTBRIDGE_DB_DIR=/data

EXPOSE 7890

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7890/health')" || exit 1

CMD ["ab", "serve", "--public"]
```

### Changes to `store.py`

**`DB_DIR` should respect env var (line 16-17):**

```python
DB_DIR = Path(os.environ.get("AGENTBRIDGE_DB_DIR", str(Path.home() / ".agentbridge")))
DB_PATH = DB_DIR / "messages.db"
```

### New file: `docker-compose.yml`

```yaml
version: "3.8"

services:
  agentbridge:
    build: .
    container_name: agentbridge
    ports:
      - "${AB_PORT:-7890}:7890"
    volumes:
      - ab_data:/data
    environment:
      - AGENTBRIDGE_HOST=0.0.0.0
      - AGENTBRIDGE_PORT=7890
      - AGENTBRIDGE_DB_DIR=/data
      - AGENTBRIDGE_TOKEN=${AGENTBRIDGE_TOKEN:-}
      - AGENTBRIDGE_RATE_LIMIT=${AGENTBRIDGE_RATE_LIMIT:-10}
      - AGENTBRIDGE_RATE_BURST=${AGENTBRIDGE_RATE_BURST:-30}
      # Uncomment for multi-instance scaling:
      # - AGENTBRIDGE_REDIS_URL=redis://redis:6379
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:7890/health')"]
      interval: 30s
      timeout: 3s
      retries: 3
      start_period: 5s

  # Uncomment for multi-instance horizontal scaling:
  # redis:
  #   image: redis:7-alpine
  #   container_name: agentbridge-redis
  #   ports:
  #     - "6379:6379"
  #   volumes:
  #     - redis_data:/data
  #   command: redis-server --appendonly yes
  #   restart: unless-stopped

volumes:
  ab_data:
  # redis_data:
```

### New file: `.dockerignore`

```
.git
.github
__pycache__
*.pyc
*.pyo
.env
.agentbridge/
tests/
docs/
*.md
.pytest_cache
.mypy_cache
.ruff_cache
```

---

## 7. Redis Pub/Sub

### New file: `src/agentbridge/pubsub.py`

```python
"""Pluggable pub/sub backend for SSE broadcast.

Default: InProcessPubSub (identical to v1 behavior).
Optional: RedisPubSub when AGENTBRIDGE_REDIS_URL is set.

The PubSub layer replaces the _sse_subscribers list in server.py.
It does NOT replace WebSocket connections (those remain instance-local).
"""

import asyncio
import json
import logging
import os
from typing import Protocol, Callable, Awaitable

logger = logging.getLogger("agentbridge.pubsub")


class PubSub(Protocol):
    """Protocol for event distribution backends."""

    async def publish(self, channel: str, data: dict) -> None:
        """Publish an event to a named channel."""
        ...

    async def subscribe(self, channel: str, callback: Callable[[str, dict], Awaitable[None]]) -> None:
        """Subscribe to a channel. Callback receives (channel, data).
        Use channel='*' for all events."""
        ...

    async def unsubscribe(self, callback: Callable) -> None:
        """Remove a specific callback."""
        ...

    async def close(self) -> None:
        """Clean shutdown."""
        ...


class InProcessPubSub:
    """In-process pub/sub. Single-instance only. Identical to v1 behavior."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, data: dict) -> None:
        async with self._lock:
            # Exact channel subscribers
            callbacks = list(self._subscribers.get(channel, []))
            # Wildcard subscribers
            callbacks.extend(self._subscribers.get("*", []))

        for cb in callbacks:
            try:
                await cb(channel, data)
            except Exception as e:
                logger.warning(f"PubSub callback error on channel '{channel}': {e}")

    async def subscribe(self, channel: str, callback: Callable) -> None:
        async with self._lock:
            self._subscribers.setdefault(channel, []).append(callback)

    async def unsubscribe(self, callback: Callable) -> None:
        async with self._lock:
            for channel, cbs in self._subscribers.items():
                self._subscribers[channel] = [cb for cb in cbs if cb is not callback]

    async def close(self) -> None:
        self._subscribers.clear()


class RedisPubSub:
    """Redis-backed pub/sub for multi-instance deployments.

    Requires: pip install agentbridge[redis]
    Set: AGENTBRIDGE_REDIS_URL=redis://hostname:6379
    """

    CHANNEL_PREFIX = "ab:"

    def __init__(self, redis_url: str):
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        self._callbacks: dict[str, list[Callable]] = {}
        self._listener_task: asyncio.Task | None = None
        self._started = False

    async def publish(self, channel: str, data: dict) -> None:
        redis_channel = f"{self.CHANNEL_PREFIX}{channel}"
        payload = json.dumps(data, default=str)
        await self._redis.publish(redis_channel, payload)

    async def subscribe(self, channel: str, callback: Callable) -> None:
        self._callbacks.setdefault(channel, []).append(callback)
        redis_channel = f"{self.CHANNEL_PREFIX}{channel}"
        if channel == "*":
            await self._pubsub.psubscribe(f"{self.CHANNEL_PREFIX}*")
        else:
            await self._pubsub.subscribe(redis_channel)
        if not self._started:
            self._listener_task = asyncio.create_task(self._listen())
            self._started = True

    async def unsubscribe(self, callback: Callable) -> None:
        for channel, cbs in self._callbacks.items():
            self._callbacks[channel] = [cb for cb in cbs if cb is not callback]

    async def _listen(self):
        try:
            async for message in self._pubsub.listen():
                if message["type"] not in ("message", "pmessage"):
                    continue
                raw_channel = message.get("channel", "")
                if isinstance(raw_channel, bytes):
                    raw_channel = raw_channel.decode()
                channel = raw_channel.removeprefix(self.CHANNEL_PREFIX)
                try:
                    data = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                # Dispatch to exact channel + wildcard subscribers
                callbacks = list(self._callbacks.get(channel, []))
                callbacks.extend(self._callbacks.get("*", []))
                for cb in callbacks:
                    try:
                        await cb(channel, data)
                    except Exception as e:
                        logger.warning(f"Redis callback error on '{channel}': {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Redis listener error: {e}")

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        await self._pubsub.close()
        await self._redis.close()


def create_pubsub() -> PubSub:
    """Factory: creates Redis or InProcess pub/sub based on environment."""
    url = os.environ.get("AGENTBRIDGE_REDIS_URL", "").strip()
    if url:
        logger.info(f"Using Redis pub/sub: {url.split('@')[-1]}")  # log without credentials
        return RedisPubSub(url)
    logger.info("Using in-process pub/sub (single instance mode)")
    return InProcessPubSub()
```

### Changes to `server.py`

This is the largest refactor. The `_sse_subscribers` / `_sse_lock` / `_broadcast_sse` mechanism is replaced by the PubSub backend.

**Remove (lines 45-47):**

```python
# Delete these lines:
_sse_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []
_sse_lock = threading.Lock()
```

**Add import (line 26):**

```python
from .pubsub import create_pubsub, PubSub
```

**Add module-level pubsub holder:**

```python
_pubsub: PubSub | None = None
```

**Modify `lifespan` (lines 236-246):**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _uvicorn_loop, _pubsub
    _uvicorn_loop = asyncio.get_running_loop()
    get_store()
    _pubsub = create_pubsub()
    yield
    if _pubsub:
        await _pubsub.close()
    global store
    with _store_lock:
        if store is not None:
            store.close()
            store = None
```

**Replace `_safe_put` and `_broadcast_sse` (lines 180-194) with:**

```python
def _broadcast_sse(event: str, data: dict):
    """Push an event to all SSE subscribers via pub/sub."""
    if _pubsub is None:
        return
    if _uvicorn_loop and _uvicorn_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            _pubsub.publish(event, data),
            _uvicorn_loop,
        )
    else:
        # Fallback for tests or non-async contexts
        try:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(_pubsub.publish(event, data), loop=loop)
        except RuntimeError:
            pass
```

**Rewrite SSE endpoint (lines 758-845):**

The SSE endpoint subscribes to the pubsub instead of appending to the list:

```python
@http_app.get("/events")
async def sse_events(
    request: Request,
    since_id: str | None = Query(None, alias="since_id"),
    thread: str | None = Query(None),
    cursor_agent: str | None = Query(None, alias="cursor_agent"),
    cursor_thread: str | None = Query(None, alias="cursor_thread"),
    backlog_limit: int = Query(200, ge=10, le=1000),
):
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    thread_filter = thread
    cursor_thread_value = cursor_thread or thread_filter or "general"

    since_timestamp: str | None = None
    if since_id:
        since_timestamp = await get_store().message_timestamp_async(since_id)
    elif cursor_agent:
        cursor = await get_store().get_delivery_cursor_async(cursor_agent, cursor_thread_value)
        if cursor and cursor.get("last_timestamp"):
            since_timestamp = cursor["last_timestamp"]

    backlog: list[Message] = []
    if since_timestamp:
        backlog = await get_store().read_messages_async(
            thread=thread_filter, since=since_timestamp, limit=backlog_limit,
        )

    def _build_sse(event_name: str, payload: dict) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"

    async def _update_cursor(payload: dict, payload_thread: str | None = None):
        if not cursor_agent:
            return
        message_id = payload.get("id")
        timestamp = payload.get("timestamp")
        if not message_id or not timestamp:
            return
        thread_name = cursor_thread_value or payload_thread or "general"
        await get_store().set_delivery_cursor_async(cursor_agent, thread_name, message_id, timestamp)

    async def _on_event(channel: str, data: dict):
        """PubSub callback: serialize and push to SSE queue."""
        if thread_filter and data.get("thread") and data["thread"] != thread_filter:
            return
        payload = json.dumps(data, default=str)
        msg = f"event: {channel}\ndata: {payload}\n\n"
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("SSE subscriber queue full -- event dropped")

    # Subscribe to all events
    await _pubsub.subscribe("*", _on_event)

    async def event_generator():
        try:
            yield "event: connected\ndata: {}\n\n"
            for msg in backlog:
                payload = msg.model_dump(mode="json")
                if thread_filter and payload.get("thread") != thread_filter:
                    continue
                await _update_cursor(payload, msg.thread)
                yield _build_sse("message", payload)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    # Parse back for cursor update
                    for line in data.splitlines():
                        if line.startswith("data:"):
                            try:
                                payload = json.loads(line[len("data: "):])
                                await _update_cursor(payload, payload.get("thread"))
                            except (json.JSONDecodeError, TypeError):
                                pass
                            break
                    yield data
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await _pubsub.unsubscribe(_on_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

### Changes to `pyproject.toml`

**Add optional dependency (after `dev` group, line 26):**

```toml
redis = ["redis[hiredis]>=5.0.0"]
```

---

## 8. Implementation Order

| Phase | Feature | Effort | Files Changed | Files Created | Depends On |
|-------|---------|--------|---------------|---------------|------------|
| 1 | Rate Limiting | 2h | server.py | rate_limit.py | None |
| 2 | Structured Content | 3h | models.py, store.py, server.py | None | None |
| 3 | Agent Memory | 4h | store.py, server.py | None | None |
| 4 | Streaming | 4h | server.py, client.py | None | Phase 2 (blocks in stream_end) |
| 5 | Subscriptions | 3h | ws_manager.py, server.py, client.py | None | None |
| 6 | Docker | 2h | store.py, pyproject.toml | Dockerfile, docker-compose.yml, .dockerignore | None |
| 7 | Redis Pub/Sub | 6h | server.py, pyproject.toml | pubsub.py | Phase 6 (Docker) |

Phases 1-3 are independent and can be parallelized. Phase 4 depends on Phase 2 (blocks field). Phase 7 should be deferred until there is a concrete need for multi-instance deployment.

Total: ~24h of implementation.

---

## 9. File Inventory

### Modified Files

| File | Changes |
|------|---------|
| `src/agentbridge/models.py` | Add `ContentBlock` class, add `blocks` field to `Message` |
| `src/agentbridge/store.py` | Add `blocks` column migration, `agent_memory` table, 5 memory CRUD methods + 5 async wrappers, memory pruning in `_prune()`, `AGENTBRIDGE_DB_DIR` env var |
| `src/agentbridge/server.py` | Rate limit imports + init + guards, `ContentBlockRequest` + `MemorySetRequest` models, 5 memory HTTP endpoints, 4 memory MCP tools, 3 streaming WS handlers, 2 subscription WS handlers, pub/sub integration replacing `_sse_subscribers` |
| `src/agentbridge/ws_manager.py` | `subscriptions` dict on `WebSocketConnection`, `matches_filter()` method, subscription-aware `broadcast()` |
| `src/agentbridge/client.py` | `stream_handler` field, `on_stream()` registration, `stream_response()` method, `subscribe()`/`unsubscribe()` methods, stream event handling in `_read_loop` |
| `pyproject.toml` | Add `redis` optional dependency group |

### New Files

| File | Purpose |
|------|---------|
| `src/agentbridge/rate_limit.py` | `TokenBucket` + `RateLimiter` classes |
| `src/agentbridge/pubsub.py` | `PubSub` protocol, `InProcessPubSub`, `RedisPubSub`, `create_pubsub()` factory |
| `Dockerfile` | Container image (python:3.12-slim, healthcheck, `/data` volume) |
| `docker-compose.yml` | Single service + commented Redis service |
| `.dockerignore` | Build context exclusions |
