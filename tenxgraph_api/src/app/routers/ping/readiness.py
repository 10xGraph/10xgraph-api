"""Readiness checks behind ``GET /health/ready``.

``/ping`` is a liveness probe: it answers as long as the process can serve a request. A
readiness probe must also say whether the server can do its job, so this checks the
dependencies a request needs: the checkpointer, the configured Redis, and the store.

Each check is time-boxed and reports only ``ok`` or ``fail``; components with nothing to check
are left out, and the reason for a failure is logged, never returned, because the endpoint is
public. Results are cached for ``CACHE_SECONDS`` with one check in flight at a time, so a flood
of probe requests cannot turn into a flood of database and Redis round trips.

Redis is the one dependency the server survives losing: the run lock, run cap, WebSocket caps
and (by default) the rate limiter fail open, and ownership falls back to the database. A Redis
outage therefore must not take every pod out of rotation. Its result is still reported, but only
counts against readiness when a configured feature fails closed without it (see
:func:`redis_required`). While the app is shutting down readiness answers 503 (``draining``) so
the load balancer stops sending new requests.

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


async def _check_schema_version(checkpointer: Any) -> None:
    """Fail while the database schema is older than the one this code expects.

    Reads the recorded version without creating anything (``aget_schema_version``), so a
    missing or half-upgraded schema (boot setup failed, or the database was restored from an
    older backup) keeps the pod out of rotation instead of failing every request.
    """
    read_version = getattr(checkpointer, "aget_schema_version", None)
    expected_fn = getattr(checkpointer, "_get_current_schema_version", None)
    if not callable(read_version) or not callable(expected_fn):
        return
    found, expected = await read_version(), expected_fn()
    if found < expected:
        raise RuntimeError(f"checkpointer schema version {found} is below the expected {expected}")


async def check_checkpointer(checkpointer: Any) -> None:
    if checkpointer is None:
        raise _Skip
    if await _own_check(checkpointer):
        return
    run_query = getattr(checkpointer, "_run_query", None)
    if callable(run_query):  # PgCheckpointer: one round trip on its pool
        await run_query(lambda conn: conn.fetchval("SELECT 1"))
        await _check_schema_version(checkpointer)
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
    from tenxgraph_api.src.app.core.redis_client import create_redis

    # A fresh connection per probe: probes are seconds apart, and a cached client would be
    # tied to the event loop that created it.
    client = create_redis(url)
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


def redis_required(graph_config: Any) -> bool:
    """True when a configured feature fails closed without Redis.

    Token revocation on Redis answers 503 when Redis is down, and a Redis rate limiter with
    ``fail_open`` off denies requests. Every other Redis use fails open.
    """
    try:
        if getattr(graph_config, "token_revocation", None) == "redis":
            return True
        limit = getattr(graph_config, "rate_limit", None)
        return bool(
            limit is not None
            and getattr(limit, "backend", None) == "redis"
            and getattr(limit, "fail_open", True) is False
        )
    except Exception:
        return False


def is_ready(checks: dict[str, str], *, redis_is_required: bool) -> bool:
    """Whether ``checks`` allow traffic; a failed optional Redis does not count."""
    return all(
        result != FAIL or (name == "redis" and not redis_is_required)
        for name, result in checks.items()
    )


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
