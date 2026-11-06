"""Opening a jobs Redis client: own database with a prefixed fallback, command retries off."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict
from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.connection import parse_url
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ResponseError


logger = logging.getLogger("tenxgraph_api.jobs")

#: Key prefix used when jobs share database 0 with other data.
FALLBACK_PREFIX = "10xgraph:jobs:"
#: Default database for jobs data (``worker.redis_db``).
DEFAULT_JOBS_DB = 1
#: Options for short commands (store calls, XACK, XADD): every call fails after 5 s.
SHORT_CLIENT_OPTIONS: dict[str, Any] = {
    "socket_timeout": 5.0,
    "socket_connect_timeout": 5.0,
    "socket_keepalive": True,
    "health_check_interval": 30,
    "decode_responses": True,
}


class JobsRedis(BaseModel):
    """A resolved jobs connection: the client, the key prefix and who owns the client."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    client: Any
    prefix: str
    owned: bool
    db: int


def check_prefix(prefix: str) -> str:
    """Return ``prefix``; a brace in it would move the hash tag, so it is refused."""
    if "{" in prefix or "}" in prefix:
        raise ValueError(
            "a jobs key prefix must not contain '{' or '}' (it would move the hash tag)"
        )
    return prefix


def no_retry() -> Retry:
    """A policy that never retries: a script replayed after a timeout could apply twice."""
    return Retry(NoBackoff(), 0)


def client_retries(client: Any) -> int:
    """How many times ``client`` retries a failed command (0 when it cannot tell)."""
    if isinstance(client, RedisCluster):
        retry = getattr(client, "retry", None)
        return int(retry.get_retries()) if retry is not None else 0
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None) or {}
    retry = kwargs.get("retry")
    if retry is not None:
        return int(retry.get_retries())
    return 1 if kwargs.get("retry_on_error") or kwargs.get("retry_on_timeout") else 0


def jobs_client_for(url: str, db: int, options: dict[str, Any]) -> Redis:
    """A client for ``url`` on database ``db`` (``db`` wins over the URL path), never retrying."""
    kwargs: dict[str, Any] = dict(parse_url(url))
    kwargs.update(options)
    kwargs["db"] = db
    kwargs["retry"] = no_retry()
    kwargs["retry_on_error"] = []
    kwargs.pop("retry_on_timeout", None)
    return Redis.from_pool(ConnectionPool(**kwargs))


def warn_if_retrying(client: Any, what: str) -> None:
    """One WARNING when a caller's client retries commands (it may replay a jobs script)."""
    retries = client_retries(client)
    if retries > 0:
        logger.warning(
            "jobs %s: the Redis client passed in retries failed commands (%d retries); a "
            "retried Lua script can apply a claim or transition twice. Build it with "
            "retry=Retry(NoBackoff(), 0) (redis.asyncio.retry, redis.backoff).",
            what,
            retries,
        )


def _given(client: Any, key_prefix: str, what: str) -> JobsRedis:
    """Wrap a caller's client as it is; only its database decides the prefix."""
    warn_if_retrying(client, what)
    if isinstance(client, RedisCluster):
        logger.warning(
            "jobs %s: Redis Cluster has only database 0; using it with keys under %r",
            what,
            FALLBACK_PREFIX,
        )
        return JobsRedis(client=client, prefix=key_prefix + FALLBACK_PREFIX, owned=False, db=0)
    current = int(client.connection_pool.connection_kwargs.get("db") or 0)
    prefix = key_prefix + (FALLBACK_PREFIX if current == 0 else "")
    return JobsRedis(client=client, prefix=prefix, owned=False, db=current)


async def open_jobs_redis(
    *,
    url: str | None,
    client: Any,
    db: int | None,
    key_prefix: str,
    options: dict[str, Any],
    what: str,
) -> JobsRedis:
    """Connect for jobs data, falling back to database 0 plus ``FALLBACK_PREFIX`` if refused.

    A passed ``client`` is never closed here; ``db=None`` keeps the database in ``url``.
    """
    check_prefix(key_prefix)
    if client is not None:
        return _given(client, key_prefix, what)
    if not url:
        raise ValueError(f"jobs {what}: pass a Redis url or client")
    wanted = int(parse_url(url).get("db") or 0) if db is None else int(db)
    made = jobs_client_for(url, wanted, options)
    if wanted == 0:
        return JobsRedis(client=made, prefix=key_prefix + FALLBACK_PREFIX, owned=True, db=0)
    try:
        await made.ping()
    except ResponseError as exc:
        await made.aclose()
        logger.warning(
            "jobs %s: Redis refused database %d (%s); using database 0 with keys under %r. "
            "Give each app and environment its own Redis database or server.",
            what,
            wanted,
            exc,
            FALLBACK_PREFIX,
        )
        made = jobs_client_for(url, 0, options)
        return JobsRedis(client=made, prefix=key_prefix + FALLBACK_PREFIX, owned=True, db=0)
    return JobsRedis(client=made, prefix=key_prefix, owned=True, db=wanted)
