"""Token-bucket rate limiter for AgentBridge endpoints.

Usage:
    limiter = RateLimiter()
    if not limiter.acquire("agent-name", rate=10, burst=20):
        raise HTTPException(429, "Rate limit exceeded")
"""

import threading
import time


class TokenBucket:
    """Thread-safe token bucket for a single key."""

    def __init__(self, rate: float, burst: float):
        self.rate = rate      # tokens refilled per second
        self.burst = burst    # maximum token capacity
        self._tokens = burst
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def acquire(self, tokens: float = 1.0) -> bool:
        """Consume tokens. Returns True if allowed, False if rate limited."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class RateLimiter:
    """Per-key rate limiter using token buckets.

    Default limits (configurable per call-site):
      - message send: 30 msg/s burst 60
      - event broadcast: 100 ev/s burst 200
    """

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, key: str, rate: float, burst: float) -> TokenBucket:
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(rate, burst)
            return self._buckets[key]

    def acquire(self, key: str, rate: float = 30.0, burst: float = 60.0, tokens: float = 1.0) -> bool:
        """Return True if request is allowed for this key."""
        bucket = self._get_bucket(key, rate, burst)
        return bucket.acquire(tokens)

    def reset(self, key: str):
        """Remove bucket for a key (useful in tests)."""
        with self._lock:
            self._buckets.pop(key, None)


# Global singleton
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter
