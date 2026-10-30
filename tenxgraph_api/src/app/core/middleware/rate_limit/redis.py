import logging
import time
import uuid
from typing import Any

from .base import BaseRateLimitBackend, RateLimitDecision


try:
    from redis.asyncio import Redis as AsyncRedis  # type: ignore[import]

    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
    AsyncRedis = None  # type: ignore[assignment,misc]


logger = logging.getLogger("tenxgraph_api.rate_limit")
_REDIS_EXTRA_INSTALL = 'pip install "10xgraph-api[redis]"'

# Atomic sliding-window check using a Redis sorted set.
#
# The script:
# 1. Removes timestamps older than the current window.
# 2. Counts the remaining requests for the key.
# 3. If under the limit, adds the current request as a unique sorted-set member.
# 4. Sets an expiry so idle keys clean themselves up.
# 5. Returns whether the request is allowed, remaining quota, and reset time.
_SLIDING_WINDOW_LUA = """
local key        = KEYS[1]
local now_ms     = tonumber(ARGV[1])
local window_ms  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local member     = ARGV[4]

local window_start = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', key, '-inf', tostring(window_start))

local count = tonumber(redis.call('ZCARD', key))

if count < limit then
    redis.call('ZADD', key, tostring(now_ms), member)
    redis.call('EXPIRE', key, math.ceil(window_ms / 1000) + 1)
    return {1, limit - count - 1, math.ceil(window_ms / 1000)}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_after
    if #oldest >= 2 then
        local oldest_ms = tonumber(oldest[2])
        reset_after = math.ceil((oldest_ms + window_ms - now_ms) / 1000) + 1
    else
        reset_after = math.ceil(window_ms / 1000)
    end
    return {0, 0, math.max(reset_after, 1)}
end
"""


class RedisRateLimitBackend(BaseRateLimitBackend):
    """Distributed sliding-window rate limiter backed by Redis.

    On a Redis error it allows the request (``fail_open``) or denies it as unavailable, which
    the middleware answers with 503, never with the 429 of a real limit hit.
    """

    # The URL the client was built from (``from_url``); None for a client passed in. The
    # startup check pings it.
    redis_url: str | None = None
    # True while Redis keeps failing: only the first error of an outage logs a traceback.
    _failing = False

    def __init__(
        self,
        redis: Any,
        prefix: str,
        fail_open: bool = True,
        close_redis: bool = False,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._fail_open = fail_open
        self._close_redis = close_redis
        self._script = self._redis.register_script(_SLIDING_WINDOW_LUA)

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        prefix: str,
        fail_open: bool = True,
    ) -> "RedisRateLimitBackend":
        if not _REDIS_AVAILABLE or AsyncRedis is None:
            raise ImportError(
                "Redis backend requires the 'redis' package. "
                f"Install the optional Redis extra with: {_REDIS_EXTRA_INSTALL}"
            )
        from tenxgraph_api.src.app.core.redis_client import create_redis

        redis = create_redis(redis_url)
        backend = cls(redis=redis, prefix=prefix, fail_open=fail_open, close_redis=True)
        backend.redis_url = redis_url
        return backend

    async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
        redis_key = f"{self._prefix}:{key}"
        now_ms = int(time.time() * 1000)
        window_ms = window * 1000
        member = f"{now_ms}:{uuid.uuid4().hex}"

        try:
            result = await self._script(
                keys=[redis_key],
                args=[now_ms, window_ms, limit, member],
            )
            decision = RateLimitDecision(
                allowed=bool(result[0]),
                remaining=int(result[1]),
                reset_after=int(result[2]),
            )
        except Exception as exc:
            self._log_error(exc)
            if self._fail_open:
                return RateLimitDecision(allowed=True, remaining=0, reset_after=window)
            return RateLimitDecision.backend_unavailable()
        if self._failing:
            self._failing = False
            logger.info("Redis rate-limit backend recovered")
        return decision

    def _log_error(self, exc: Exception) -> None:
        """Traceback for the first error of an outage; one line for each error after it."""
        action = "allowing" if self._fail_open else "denying (503)"
        if not self._failing:
            self._failing = True
            logger.exception(
                "Redis rate-limit backend error; %s requests until it recovers", action
            )
        else:
            logger.warning(
                "Redis rate-limit backend error (%s): %s: %s", action, type(exc).__name__, exc
            )

    async def close(self) -> None:
        if self._close_redis:
            await self._redis.aclose()
