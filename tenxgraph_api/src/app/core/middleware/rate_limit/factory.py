import logging
from typing import Any

from injectq import InjectQ

from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig

from .base import BaseRateLimitBackend
from .memory import MemoryRateLimitBackend
from .redis import RedisRateLimitBackend


try:
    # Needed at runtime: _bind_redis binds the client under its class too.
    from redis.asyncio import Redis
except ImportError:  # the redis extra is not installed
    Redis = None  # type: ignore[assignment,misc]


logger = logging.getLogger("tenxgraph_api.rate_limit")


def build_backend(
    config: RateLimitConfig,
    container: InjectQ | None = None,
) -> BaseRateLimitBackend:
    """Build or resolve the configured rate-limit backend.

    Custom backends are provided by binding a ``BaseRateLimitBackend`` instance
    into InjectQ, matching the style used by auth/authorization.
    """
    container = container or InjectQ.get_instance()

    injected_backend = container.try_get(BaseRateLimitBackend)
    if config.backend == "custom":
        if not injected_backend:
            raise ValueError(
                "rate_limit.backend='custom' requires a BaseRateLimitBackend "
                "instance bound in InjectQ"
            )
        return injected_backend

    if config.backend == "redis":
        return _build_redis_backend(config, container)

    logger.info("Rate-limit backend: memory (in-process, not shared across workers)")
    return MemoryRateLimitBackend()


def _build_redis_backend(
    config: RateLimitConfig,
    container: InjectQ,
) -> RedisRateLimitBackend:
    redis = _get_redis_from_container(container)
    if redis is not None:
        logger.info(
            "Rate-limit backend: Redis from InjectQ (prefix=%s, fail_open=%s)",
            config.redis_prefix,
            config.fail_open,
        )
        return RedisRateLimitBackend(
            redis=redis,
            prefix=config.redis_prefix,
            fail_open=config.fail_open,
            close_redis=False,
        )

    if not config.redis_url:
        raise ValueError("rate_limit.redis.url is required when no Redis client is bound")

    backend = RedisRateLimitBackend.from_url(
        redis_url=config.redis_url,
        prefix=config.redis_prefix,
        fail_open=config.fail_open,
    )
    _bind_redis(container, backend._redis)
    logger.info(
        "Rate-limit backend: created Redis and bound it in InjectQ (prefix=%s, fail_open=%s)",
        config.redis_prefix,
        config.fail_open,
    )
    return backend


def _get_redis_from_container(container: InjectQ) -> Any | None:
    redis = container.try_get("redis")
    if redis is not None:
        return redis

    redis = container.try_get("redis_client")
    if redis is not None:
        return redis

    return container.try_get("Redis")


def _bind_redis(container: InjectQ, redis: "Redis") -> None:
    if Redis is not None:
        container.bind_instance(Redis, redis)
    container.bind_instance("redis", redis)
    container.bind_instance("redis_client", redis)
