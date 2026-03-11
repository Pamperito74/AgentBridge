"""Pub/Sub abstraction for AgentBridge event broadcasting.

Two implementations:
  - InProcessPubSub: asyncio queues, zero deps, default (v1-compatible)
  - RedisPubSub: opt-in via AGENTBRIDGE_REDIS_URL env var

Both expose the same interface:
    pubsub = get_pubsub()
    sub = await pubsub.subscribe("events")
    await pubsub.publish("events", {"type": "message", ...})
    data = await sub.get()
    await pubsub.unsubscribe("events", sub)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Protocol

logger = logging.getLogger("agentbridge.pubsub")


class Subscription(Protocol):
    async def get(self) -> dict: ...
    async def close(self): ...


class _AsyncQueueSubscription:
    """In-process queue-backed subscription."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self._closed = False

    async def get(self) -> dict:
        return await self._queue.get()

    async def close(self):
        self._closed = True


class InProcessPubSub:
    """Asyncio in-process pub/sub. Default when no Redis URL is configured."""

    def __init__(self):
        # channel -> list of active queues
        self._channels: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, data: dict):
        async with self._lock:
            queues = list(self._channels.get(channel, []))
        for q in queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                logger.warning(f"PubSub queue full on channel '{channel}', dropping event")

    async def subscribe(self, channel: str, maxsize: int = 256) -> _AsyncQueueSubscription:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        sub = _AsyncQueueSubscription(q)
        async with self._lock:
            self._channels.setdefault(channel, []).append(q)
        return sub

    async def unsubscribe(self, channel: str, sub: _AsyncQueueSubscription):
        await sub.close()
        async with self._lock:
            queues = self._channels.get(channel, [])
            try:
                queues.remove(sub._queue)
            except ValueError:
                pass
            if not queues:
                self._channels.pop(channel, None)

    async def close(self):
        async with self._lock:
            self._channels.clear()


class RedisPubSub:
    """Redis-backed pub/sub. Activated when AGENTBRIDGE_REDIS_URL is set.

    Requires: pip install agentbridge[redis]   (redis[hiredis]>=5.0.0)
    """

    def __init__(self, redis_url: str):
        try:
            import redis.asyncio as aioredis  # type: ignore[import]
        except ImportError:
            raise RuntimeError(
                "Redis extras not installed. Run: pip install agentbridge[redis]"
            )
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._pubsub = self._redis.pubsub()

    async def publish(self, channel: str, data: dict):
        await self._redis.publish(channel, json.dumps(data))

    async def subscribe(self, channel: str, **kwargs) -> "_RedisSub":
        sub = self._redis.pubsub()
        await sub.subscribe(channel)
        return _RedisSub(sub, channel)

    async def unsubscribe(self, channel: str, sub: "_RedisSub"):
        await sub.close()

    async def close(self):
        await self._redis.close()


class _RedisSub:
    def __init__(self, pubsub, channel: str):
        self._ps = pubsub
        self._channel = channel

    async def get(self) -> dict:
        while True:
            msg = await self._ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg["type"] == "message":
                return json.loads(msg["data"])

    async def close(self):
        await self._ps.unsubscribe(self._channel)
        await self._ps.close()


# ── Singleton factory ──────────────────────────────────────────────────────────

_pubsub_instance: InProcessPubSub | RedisPubSub | None = None


def get_pubsub() -> InProcessPubSub | RedisPubSub:
    global _pubsub_instance
    if _pubsub_instance is None:
        redis_url = os.environ.get("AGENTBRIDGE_REDIS_URL", "")
        if redis_url:
            logger.info(f"Using Redis pub/sub: {redis_url}")
            _pubsub_instance = RedisPubSub(redis_url)
        else:
            _pubsub_instance = InProcessPubSub()
    return _pubsub_instance
