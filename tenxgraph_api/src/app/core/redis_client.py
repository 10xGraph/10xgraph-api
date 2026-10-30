"""One place that builds the API's Redis clients, with timeouts and a bounded pool.

Every Redis client the API opens goes through :func:`create_redis`, so a Redis that stops
answering fails a call after ``REDIS_SOCKET_TIMEOUT`` seconds instead of hanging the request.
The limits come from the settings (``REDIS_SOCKET_TIMEOUT``, ``REDIS_CONNECT_TIMEOUT``,
``REDIS_MAX_CONNECTIONS``, ``REDIS_POOL_TIMEOUT``, ``REDIS_HEALTH_CHECK_INTERVAL``).
"""

from __future__ import annotations

from typing import Any


def key_part(value: Any) -> str:
    """An id made safe to sit between ``:`` separators in a Redis key.

    ``:`` and ``%`` are percent-encoded, so ``a:b`` cannot read as two parts. Ids without
    either (UUIDs, snowflake ids, most user ids) are unchanged, so existing keys stay valid.
    """
    return str(value).replace("%", "%25").replace(":", "%3A")


def prefixed(namespace: str) -> str:
    """``namespace`` with the deployment prefix (``REDIS_KEY_PREFIX``) in front, if one is set."""
    from tenxgraph_api.src.app.core.config.settings import get_settings

    prefix = get_settings().REDIS_KEY_PREFIX
    return f"{prefix}:{namespace}" if prefix else namespace


def redis_options() -> dict[str, Any]:
    """Connection keyword arguments built from the settings (0 turns a limit off)."""
    from tenxgraph_api.src.app.core.config.settings import get_settings

    settings = get_settings()
    options: dict[str, Any] = {}
    if settings.REDIS_SOCKET_TIMEOUT > 0:
        options["socket_timeout"] = settings.REDIS_SOCKET_TIMEOUT
    if settings.REDIS_CONNECT_TIMEOUT > 0:
        options["socket_connect_timeout"] = settings.REDIS_CONNECT_TIMEOUT
    if settings.REDIS_HEALTH_CHECK_INTERVAL > 0:
        options["health_check_interval"] = settings.REDIS_HEALTH_CHECK_INTERVAL
    return options


def create_redis(url: str, *, decode_responses: bool = False) -> Any:
    """An async Redis client for ``url`` with the configured timeouts and pool bound."""
    from redis.asyncio import BlockingConnectionPool, Redis  # type: ignore[import]

    from tenxgraph_api.src.app.core.config.settings import get_settings

    settings = get_settings()
    options = redis_options()
    if settings.REDIS_MAX_CONNECTIONS > 0:
        pool = BlockingConnectionPool.from_url(
            url,
            decode_responses=decode_responses,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            timeout=settings.REDIS_POOL_TIMEOUT or None,
            **options,
        )
        return Redis.from_pool(pool)
    return Redis.from_url(url, decode_responses=decode_responses, **options)
