"""Readiness checks behind ``GET /health/ready``.

``/ping`` is a liveness probe: it answers as long as the process can serve a request. A
readiness probe must also say whether the server can do its job, so this checks the
dependencies a request needs: the checkpointer, the store and the API's cache store. Each is
asked with its own ``aping()`` (read-only; it never creates a schema or a key).

Each check is time-boxed and reports only ``ok`` or ``fail``; components with nothing to check
are left out, and the reason for a failure is logged, never returned, because the endpoint is
public. Results are cached for ``CACHE_SECONDS`` with one check in flight at a time, so a flood
of probe requests cannot turn into a flood of database and cache round trips.

The cache store is the one dependency the server survives losing: the run lock, run cap,
WebSocket caps and idempotency fall back to this process, the ownership cache falls back to
the database, and the rate limiter allows by default. A cache store outage therefore must not
take every pod out of rotation. Its result is still reported, but only counts against
readiness when a configured feature fails closed without it (see :func:`cache_required`).
While the app is shutting down readiness answers 503 (``draining``) so the load balancer stops
sending new requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger("tenxgraph_api.readiness")

CHECK_TIMEOUT_SECONDS = 2.0
OK, FAIL, SKIPPED = "ok", "fail", "skipped"


class _Skip(Exception):
    """The component is not configured, or has no ``aping`` to call."""


async def ping_component(component: Any) -> None:
    """Call ``component.aping()``; skipped when there is no component or no ``aping``."""
    aping = getattr(component, "aping", None) if component is not None else None
    if not callable(aping):
        raise _Skip
    await aping()


async def check_outcome(
    check: Callable[[], Awaitable[None]], timeout: float = CHECK_TIMEOUT_SECONDS
) -> tuple[str, BaseException | None]:
    """Run one time-boxed check: ``(ok | fail | skipped, the error when it failed)``.

    A timeout is reported as a ``TimeoutError``. Shared by the readiness probe and the startup
    check (``startup_checks``), so both judge a component the same way.
    """
    try:
        await asyncio.wait_for(check(), timeout)
    except _Skip:
        return SKIPPED, None
    except Exception as exc:  # a timeout too: TimeoutError
        return FAIL, exc
    return OK, None


async def run_check(name: str, check: Callable[[], Awaitable[None]]) -> str:
    result, error = await check_outcome(check)
    if isinstance(error, TimeoutError):
        logger.warning("Readiness check %s timed out after %ss", name, CHECK_TIMEOUT_SECONDS)
    elif error is not None:
        logger.warning("Readiness check %s failed: %s", name, error)
    return result


def readiness_targets(container: Any) -> tuple[Any, Any, Any]:
    """``(checkpointer, store, cache store)`` bound in the app's container; None when unbound."""
    from tenxgraph.storage.checkpointer import BaseCheckpointer
    from tenxgraph.storage.store import BaseStore

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore

    def bound(kind: Any) -> Any:
        return container.try_get(kind) if kind in container else None

    return bound(BaseCheckpointer), bound(BaseStore), bound(BaseCacheStore)


def cache_required_by(graph_config: Any) -> list[str]:
    """The configured features that fail closed without the cache store, by config key.

    Token revocation answers 503 while it cannot read revocations, and a rate limiter with
    ``fail_open`` off denies requests. Every other use of the cache store fails open.
    """
    if graph_config is None:
        return []
    found = []
    # A bad value is reported by the config check at boot, not here.
    with contextlib.suppress(Exception):
        if getattr(graph_config, "token_revocation", None) is not None:
            found.append("token_revocation")
    with contextlib.suppress(Exception):
        limit = getattr(graph_config, "rate_limit", None)  # None when absent or disabled
        if limit is not None and getattr(limit, "fail_open", True) is False:
            found.append("rate_limit.fail_open=false")
    return found


def cache_required(graph_config: Any) -> bool:
    """True when a configured feature fails closed without the cache store."""
    return bool(cache_required_by(graph_config))


def is_ready(checks: dict[str, str], *, cache_is_required: bool) -> bool:
    """Whether ``checks`` allow traffic; a failed cache store counts only when required."""
    return all(
        result != FAIL or (name == "cache" and not cache_is_required)
        for name, result in checks.items()
    )


async def readiness(checkpointer: Any, store: Any, cache: Any) -> dict[str, str]:
    """Run every check concurrently; ``{component: ok | fail | skipped}``."""
    names = ("checkpointer", "store", "cache")
    results = await asyncio.gather(
        run_check("checkpointer", lambda: ping_component(checkpointer)),
        run_check("store", lambda: ping_component(store)),
        run_check("cache", lambda: ping_component(cache)),
    )
    return dict(zip(names, results, strict=True))


CACHE_SECONDS = 3.0
_cache: dict[str, Any] = {"at": float("-inf"), "result": None}
_cache_lock = asyncio.Lock()


async def cached_readiness(checkpointer: Any, store: Any, cache: Any) -> dict[str, str]:
    """:func:`readiness`, reused for ``CACHE_SECONDS``; concurrent callers share one run.

    Skipped components are dropped from the result.
    """
    loop = asyncio.get_running_loop()
    async with _cache_lock:
        if _cache["result"] is None or loop.time() - _cache["at"] >= CACHE_SECONDS:
            checks = await readiness(checkpointer, store, cache)
            _cache["result"] = {k: v for k, v in checks.items() if v != SKIPPED}
            _cache["at"] = loop.time()
        return dict(_cache["result"])


def reset_cache() -> None:
    _cache.update(at=float("-inf"), result=None)
