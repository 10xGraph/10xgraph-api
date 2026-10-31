"""Sliding-window rate limiting on the API's cache store.

A request takes one slot of ``key`` that expires ``window`` seconds later and is never
released, so the live slots are exactly the requests of the last ``window`` seconds. A denied
request takes nothing (``BaseCacheStore.acquire`` adds no slot when it denies). With a shared
cache store (Redis) the window is shared by every worker; with the in-memory store each
process counts its own.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore


if TYPE_CHECKING:
    from injectq import InjectQ


logger = logging.getLogger("tenxgraph_api.rate_limit")

# Retry-After (seconds) sent with the 503 answered while the cache store cannot be reached.
UNAVAILABLE_RETRY_AFTER_SECONDS = 5


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of a single rate-limit check.

    ``unavailable`` marks a denial caused by the cache store (it could not be reached), not by
    the limit: the middleware then answers 503 ``RATE_LIMIT_UNAVAILABLE`` instead of 429, with
    ``reset_after`` as its ``Retry-After``.
    """

    allowed: bool
    remaining: int
    reset_after: int
    unavailable: bool = False

    @classmethod
    def backend_unavailable(
        cls, retry_after: int = UNAVAILABLE_RETRY_AFTER_SECONDS
    ) -> RateLimitDecision:
        """A denial because the cache store failed; answered 503 with ``Retry-After``."""
        return cls(allowed=False, remaining=0, reset_after=retry_after, unavailable=True)


# True while the cache store keeps failing: only the first error of an outage logs a traceback.
_failing = False


def store_key(key: str) -> str:
    """The cache store key of a client's rate-limit window."""
    return f"ratelimit:{key}"


async def check(
    cache: BaseCacheStore, key: str, limit: int, window: float, *, fail_open: bool = True
) -> RateLimitDecision:
    """Count one request for ``key`` against ``limit`` per ``window`` seconds.

    On a cache store error the request is allowed (``fail_open``) or denied as unavailable
    (answered 503, never the 429 of a real limit hit). Once ``acquire`` has denied, the answer
    is a 429 even when reading the clock for ``reset_after`` then fails.
    """
    global _failing  # noqa: PLW0603 - one outage state per process
    try:
        result = await cache.acquire(store_key(key), limit, ttl=window)
    except Exception as exc:
        _log_error(exc, fail_open)
        if fail_open:
            return RateLimitDecision(allowed=True, remaining=0, reset_after=math.ceil(window))
        return RateLimitDecision.backend_unavailable()
    if _failing:
        _failing = False
        logger.info("Rate-limit cache store recovered")
    if result.granted:
        return RateLimitDecision(
            allowed=True,
            remaining=max(0, limit - result.count),
            reset_after=math.ceil(window),
        )
    reset_after = math.ceil(window)
    if result.oldest_expiry is not None:
        try:
            reset_after = math.ceil(result.oldest_expiry - await cache.now())
        except Exception as exc:  # the limit was hit: keep the 429, with the full window
            logger.debug("Rate-limit cache store clock unavailable: %s", exc)
    return RateLimitDecision(allowed=False, remaining=0, reset_after=max(1, reset_after))


def _log_error(exc: Exception, fail_open: bool) -> None:
    """Traceback for the first error of an outage; one line for each error after it."""
    global _failing  # noqa: PLW0603 - one outage state per process
    action = "allowing" if fail_open else "denying (503)"
    if not _failing:
        _failing = True
        logger.exception("Rate-limit cache store error; %s requests until it recovers", action)
    else:
        logger.warning("Rate-limit cache store error (%s): %s: %s", action, type(exc).__name__, exc)


# Used when no cache store is bound and the caller has no fallback of its own: one per process.
_process_store: BaseCacheStore | None = None


def bound_cache_store(
    container: InjectQ | None = None, fallback: BaseCacheStore | None = None
) -> BaseCacheStore:
    """The cache store bound in ``container`` (default: the app's), else ``fallback``.

    Without a ``fallback`` an in-memory store of this process is used. The app binds its cache
    store at startup, so the fallback only serves an app run without its lifespan.
    """
    global _process_store  # noqa: PLW0603 - one per process, made on first use
    try:
        if container is None:
            from tenxgraph_api.src.app.core.utils.container import current_container

            container = current_container()
        bound = container.try_get(BaseCacheStore)
    except Exception:
        bound = None
    if isinstance(bound, BaseCacheStore):
        return bound
    if fallback is not None:
        return fallback
    if _process_store is None:
        _process_store = InMemoryCacheStore()
    return _process_store
