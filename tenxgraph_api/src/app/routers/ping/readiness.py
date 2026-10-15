"""Readiness checks behind ``GET /health/ready``.

``/ping`` is a liveness probe: it answers as long as the process can serve a request. A
readiness probe must also say whether the server can do its job, so this checks the
dependencies a request needs: the checkpointer, the configured Redis, and the store.

Each check is time-boxed and reports only ``ok`` or ``fail``; components with nothing to check
are left out, and the reason for a failure is logged, never returned, because the endpoint is
public. Results are cached for ``CACHE_SECONDS`` with one check in flight at a time, so a flood
of probe requests cannot turn into a flood of database and Redis round trips.

A component that knows best how to check itself can expose
``async def readiness_check() -> None`` (raise when not ready), which is used before the
built-in probes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger("tenxgraph_api.readiness")

CHECK_TIMEOUT_SECONDS = 2.0
OK, FAIL, SKIPPED = "ok", "fail", "skipped"

# Thread id the owner-lookup probe asks about; no real thread uses it.
_PROBE_THREAD_ID = "__10xgraph_readiness_probe__"


class _Skip(Exception):
    """The component has nothing this probe can check."""


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _own_check(component: Any) -> bool:
    """Run the component's ``readiness_check`` if it has one; True when it did."""
    check = getattr(component, "readiness_check", None)
    if not callable(check):
        return False
    await _maybe_await(check())
    return True


async def check_checkpointer(checkpointer: Any) -> None:
    if checkpointer is None:
        raise _Skip
    if await _own_check(checkpointer):
        return
    run_query = getattr(checkpointer, "_run_query", None)
    if callable(run_query):  # PgCheckpointer: one round trip on its pool
        await run_query(lambda conn: conn.fetchval("SELECT 1"))
    redis = getattr(checkpointer, "redis", None)
    if redis is not None and callable(getattr(redis, "ping", None)):  # its cache tier
        await _maybe_await(redis.ping())
    if not callable(run_query):
        try:  # any other checkpointer: a cheap, read-only lookup
            await checkpointer.aget_thread_owner(_PROBE_THREAD_ID)
        except NotImplementedError:
            raise _Skip from None


async def check_store(store: Any) -> None:
    if store is None:
        raise _Skip
    if await _own_check(store):
        return
    for name in ("aping", "ping"):
        ping = getattr(store, name, None)
        if callable(ping):
            await _maybe_await(ping())
            return
    raise _Skip


async def check_redis(url: str | None) -> None:
    if not url:
        raise _Skip
    from redis.asyncio import Redis  # type: ignore[import]

    # A fresh connection per probe: probes are seconds apart, and a cached client would be
    # tied to the event loop that created it.
    client = Redis.from_url(url)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def run_check(name: str, check: Callable[[], Awaitable[None]]) -> str:
    try:
        await asyncio.wait_for(check(), CHECK_TIMEOUT_SECONDS)
    except _Skip:
        return SKIPPED
    except TimeoutError:
        logger.warning("Readiness check %s timed out after %ss", name, CHECK_TIMEOUT_SECONDS)
        return FAIL
    except Exception as exc:
        logger.warning("Readiness check %s failed: %s", name, exc)
        return FAIL
    return OK


async def readiness(checkpointer: Any, store: Any, redis_url: str | None) -> dict[str, str]:
    """Run every check concurrently; ``{component: ok | fail | skipped}``."""
    names = ("checkpointer", "redis", "store")
    results = await asyncio.gather(
        run_check("checkpointer", lambda: check_checkpointer(checkpointer)),
        run_check("redis", lambda: check_redis(redis_url)),
        run_check("store", lambda: check_store(store)),
    )
    return dict(zip(names, results, strict=True))


CACHE_SECONDS = 3.0
_cache: dict[str, Any] = {"at": float("-inf"), "result": None}
_cache_lock = asyncio.Lock()


async def cached_readiness(checkpointer: Any, store: Any, redis_url: str | None) -> dict[str, str]:
    """:func:`readiness`, reused for ``CACHE_SECONDS``; concurrent callers share one run.

    Skipped components are dropped from the result.
    """
    loop = asyncio.get_running_loop()
    async with _cache_lock:
        if _cache["result"] is None or loop.time() - _cache["at"] >= CACHE_SECONDS:
            checks = await readiness(checkpointer, store, redis_url)
            _cache["result"] = {k: v for k, v in checks.items() if v != SKIPPED}
            _cache["at"] = loop.time()
        return dict(_cache["result"])


def reset_cache() -> None:
    _cache.update(at=float("-inf"), result=None)
