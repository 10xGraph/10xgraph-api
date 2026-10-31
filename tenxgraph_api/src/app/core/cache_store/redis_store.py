"""``BaseCacheStore`` on Redis (also Valkey, Dragonfly and KeyDB).

Slots are a sorted set per key (member = slot id, score = expiry in seconds on the Redis
clock). ``acquire`` and ``renew`` run as one Lua script each, so they are atomic and read the
clock with ``TIME`` inside the script: pods with skewed clocks agree on every expiry.
``if_greater`` is a Lua script too. Every command and script touches exactly one key.

Key layout. Callers pass ``"<feature>:<id>"``; the store maps the feature segment to a Redis
namespace and puts the deployment prefix (``REDIS_KEY_PREFIX``, via ``prefixed``) in front.
The namespaces are the ones the API used before the cache store, so old and new pods share
the same locks, records and revocations during a rolling deploy:

==============================  ==================================================
Caller key                      Redis key (``P:`` only when a prefix is set)
==============================  ==================================================
``concurrency:<lease key>``     ``P:10xgraph:concurrency:<lease key>``
``idempotency:<ident>``         ``P:10xgraph:idempotency:<ident>``
``owner:id:<thread>``           ``P:af:authz:owner:id:<thread>``
``owner:tomb:<thread>``         ``P:af:authz:owner:tomb:<thread>``
``revoked:jti:<jti>``           ``P:10xgraph:revoked:jti:<jti>``
``revoked:user:<user>``         ``P:10xgraph:revoked:user:<user>``
``ratelimit:<client key>``      ``P:10xgraph:ratelimit2:<client key>``
anything else                   ``P:10xgraph:cache:<key>``
==============================  ==================================================

Lease keys keep their old shape too (``runs:thread:<id>``, ``runs:user:<id>``,
``runs:idem:<ident>``, ``ws:all``, ``ws:user:<id>``), with scores that are expiry seconds as
the API's Redis leases wrote them before the cache store. The rate limiter gets a new
namespace because its scores used to be request times and are now expiries; its counters
reset once at deploy.

Redis must not evict keys that have a TTL under memory pressure (``maxmemory-policy`` must be
``noeviction`` or ``volatile-*`` with room to spare): an evicted lease lets two workers run the
same thread.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from tenxgraph_api.src.app.core.redis_client import create_redis, prefixed

from .base import BaseCacheStore, SetMode, SlotResult


# Feature segment of a caller key -> the Redis namespace it has always used.
FEATURE_NAMESPACES: dict[str, str] = {
    "concurrency": "10xgraph:concurrency",
    "idempotency": "10xgraph:idempotency",
    "owner": "af:authz:owner",
    "revoked": "10xgraph:revoked",
    "ratelimit": "10xgraph:ratelimit2",
}
# Keys whose feature is not listed above.
DEFAULT_NAMESPACE = "10xgraph:cache"

# The key outlives its newest slot by this much, like ``expire(ttl + 1)`` did before.
_KEY_TTL_MARGIN_MS = 1000

_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

# Effects replication is the default since Redis 5 and the only mode in 7; asking for it keeps
# a write after TIME legal on older servers.
_PREAMBLE = """
if redis.replicate_commands then redis.replicate_commands() end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local function touch_key()
  local newest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
  if newest[2] then
    local at = math.ceil(tonumber(newest[2]) * 1000) + tonumber(ARGV[4])
    redis.call('PEXPIREAT', KEYS[1], string.format('%.0f', at))
  end
end
"""

# ARGV: limit, ttl seconds, slot id, key ttl margin ms.
# Returns {granted, live count, oldest expiry or ''}. Scores travel as strings: Lua numbers
# in a reply are truncated to integers.
_ACQUIRE_LUA = (
    _PREAMBLE
    + """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', string.format('%.6f', now))
local count = redis.call('ZCARD', KEYS[1])
local granted = 0
if count < tonumber(ARGV[1]) then
  redis.call('ZADD', KEYS[1], string.format('%.6f', now + tonumber(ARGV[2])), ARGV[3])
  count = count + 1
  granted = 1
  touch_key()
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {granted, count, oldest[2] or ''}
"""
)

# ARGV: unused, ttl seconds, slot id, key ttl margin ms. Returns 1 when renewed, 0 when the
# slot is gone. An expired slot that is still in the set counts as gone and is removed; ``XX``
# makes sure nothing is ever added.
_RENEW_LUA = (
    _PREAMBLE
    + """
local score = redis.call('ZSCORE', KEYS[1], ARGV[3])
if not score then
  return 0
end
if tonumber(score) <= now then
  redis.call('ZREM', KEYS[1], ARGV[3])
  return 0
end
redis.call('ZADD', KEYS[1], 'XX', string.format('%.6f', now + tonumber(ARGV[2])), ARGV[3])
touch_key()
return 1
"""
)

# ARGV: value, ttl ms ('' for none). Keeps the larger number; a stored value that is not a
# number counts as absent (the old token revocation script behaved the same way).
_IF_GREATER_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '')
if current ~= nil and current >= tonumber(ARGV[1]) then
  return 0
end
local ttl = tonumber(ARGV[2])
if ttl ~= nil and ttl > 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
else
  redis.call('SET', KEYS[1], ARGV[1])
end
return 1
"""


class _Script:
    __slots__ = ("sha", "source")

    def __init__(self, source: str) -> None:
        self.source = source
        self.sha = hashlib.sha1(source.encode(), usedforsecurity=False).hexdigest()


_ACQUIRE = _Script(_ACQUIRE_LUA)
_RENEW = _Script(_RENEW_LUA)
_IF_GREATER = _Script(_IF_GREATER_LUA)


def _ttl_ms(ttl: float) -> int:
    if not ttl > 0 or math.isinf(ttl):
        raise ValueError(f"ttl must be a positive number of seconds, got {ttl!r}")
    return max(1, math.ceil(ttl * 1000))


def decimal_text(value: bytes) -> str:
    """``value`` as decimal text for ``if_greater``; ``ValueError`` when it is not one."""
    try:
        text = value.decode("ascii").strip()
    except (UnicodeDecodeError, AttributeError):
        raise ValueError("if_greater needs a decimal number as bytes")
    if not _DECIMAL.fullmatch(text) or not math.isfinite(float(text)):
        raise ValueError(f"if_greater needs a decimal number, got {text!r}")
    return text


def _endpoint(url: str | None) -> str | None:
    """``host:port`` of a URL, never user or password."""
    if not isinstance(url, str) or "://" not in url:
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port else host


def _is_noscript(exc: Exception) -> bool:
    return type(exc).__name__ == "NoScriptError" or str(exc).startswith("NOSCRIPT")


class RedisCacheStore(BaseCacheStore):
    """Shared cache store on Redis; see the module docstring for the key layout.

    Pass a ``url`` (the store builds and later closes its client with ``create_redis``, so
    the timeouts and pool bound in the settings apply) or an existing async ``client`` (left
    open on ``aclose``; its owner closes it). ``prefix`` overrides ``REDIS_KEY_PREFIX``; ``""``
    means no prefix.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any = None,
        prefix: str | None = None,
    ) -> None:
        if (url is None) == (client is None):
            raise ValueError("RedisCacheStore needs exactly one of url or client")
        self._url = url
        self._owns_client = client is None
        if client is None:
            try:
                client = create_redis(url)  # type: ignore[arg-type]
            except ImportError:
                raise ImportError(
                    'RedisCacheStore needs the redis package: pip install "10xgraph-api[redis]"'
                )
        self._client = client
        if prefix is None:
            self._namespaces = {f: prefixed(ns) for f, ns in FEATURE_NAMESPACES.items()}
            self._default = prefixed(DEFAULT_NAMESPACE)
        else:
            lead = f"{prefix}:" if prefix else ""
            self._namespaces = {f: lead + ns for f, ns in FEATURE_NAMESPACES.items()}
            self._default = lead + DEFAULT_NAMESPACE

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisCacheStore:
        return cls(url, **kwargs)

    @property
    def client(self) -> Any:
        """The Redis client (for diagnostics; features go through the store's methods)."""
        return self._client

    def redis_key(self, key: str) -> str:
        """The Redis key a caller key is stored under."""
        feature, sep, rest = key.partition(":")
        namespace = self._namespaces.get(feature) if sep else None
        if namespace is None:
            return f"{self._default}:{key}"
        return f"{namespace}:{rest}"

    async def _script(self, script: _Script, key: str, *args: Any) -> Any:
        try:
            return await self._client.evalsha(script.sha, 1, key, *args)
        except Exception as exc:
            if not _is_noscript(exc):
                raise
        return await self._client.eval(script.source, 1, key, *args)

    async def aping(self) -> None:
        await self._client.ping()

    async def now(self) -> float:
        seconds, microseconds = await self._client.time()
        return float(seconds) + float(microseconds) / 1_000_000

    async def acquire(self, key: str, limit: int, ttl: float) -> SlotResult:
        _ttl_ms(ttl)
        slot_id = uuid.uuid4().hex
        reply = await self._script(
            _ACQUIRE, self.redis_key(key), int(limit), repr(float(ttl)), slot_id, _KEY_TTL_MARGIN_MS
        )
        granted, count, oldest = int(reply[0]), int(reply[1]), reply[2]
        if isinstance(oldest, bytes | bytearray):
            oldest = oldest.decode()
        return SlotResult(
            granted=bool(granted),
            count=count,
            oldest_expiry=float(oldest) if oldest not in ("", None) else None,
            slot_id=slot_id if granted else None,
        )

    async def renew(self, key: str, slot_id: str, ttl: float) -> bool:
        _ttl_ms(ttl)
        reply = await self._script(
            _RENEW, self.redis_key(key), "", repr(float(ttl)), slot_id, _KEY_TTL_MARGIN_MS
        )
        return bool(int(reply))

    async def release(self, key: str, slot_id: str) -> None:
        await self._client.zrem(self.redis_key(key), slot_id)

    async def get(self, key: str) -> bytes | None:
        value = await self._client.get(self.redis_key(key))
        if value is None or isinstance(value, bytes):
            return value
        return value.encode() if isinstance(value, str) else bytes(value)

    async def set(
        self,
        key: str,
        value: bytes,
        ttl: float | None,
        mode: SetMode = "always",
    ) -> bool:
        px = None if ttl is None else _ttl_ms(ttl)
        redis_key = self.redis_key(key)
        if mode == "if_greater":
            reply = await self._script(
                _IF_GREATER, redis_key, decimal_text(value), "" if px is None else str(px)
            )
            return bool(int(reply))
        if mode == "if_absent":
            return bool(await self._client.set(redis_key, value, px=px, nx=True))
        if mode == "always":
            await self._client.set(redis_key, value, px=px)
            return True
        raise ValueError(f"Unknown set mode {mode!r}")

    async def delete(self, key: str) -> None:
        await self._client.delete(self.redis_key(key))

    def describe(self) -> str | None:
        if self._url is not None:
            return _endpoint(self._url)
        pool = getattr(self._client, "connection_pool", None)
        kwargs = getattr(pool, "connection_kwargs", None)
        if not isinstance(kwargs, dict) or not kwargs.get("host"):
            return None
        return f"{kwargs['host']}:{kwargs.get('port', 6379)}"

    async def aclose(self) -> None:
        if not self._owns_client:
            return
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()
