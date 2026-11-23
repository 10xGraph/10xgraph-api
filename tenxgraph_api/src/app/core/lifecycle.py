"""Boot and shutdown steps shared by the API lifespan (``main.py``) and the jobs worker.

Both processes build the same container and graph from 10xgraph.json, so they open and close
the same resources in the same order.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from injectq import InjectQ

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


logger = logging.getLogger("tenxgraph_api")


def build_container(graph_config: GraphConfig) -> InjectQ:
    """The ``injectq`` container from 10xgraph.json (else the global one), config bound."""
    from tenxgraph_api.src.app import loader

    container = loader.load_container(graph_config.injectq_path) or InjectQ.get_instance()
    container.bind_instance(GraphConfig, graph_config)
    return container


async def check_backends(container: InjectQ, settings: Any) -> None:
    """Refuse to start while the checkpointer, the store or a required cache store is down.

    Calls ``aping()`` on each, retrying for ``STARTUP_CHECK_TIMEOUT_SECONDS``, then raises
    ``StartupCheckError`` with the failed components. The cache store is required only by
    fail-closed features (token revocation, a rate limiter with ``fail_open: false``);
    otherwise an outage is one warning. Off with ``STARTUP_CHECKS=false``.
    """
    if not settings.STARTUP_CHECKS:
        logger.warning(
            "STARTUP_CHECKS is off: starting without checking that the configured backends "
            "are reachable."
        )
        return
    from tenxgraph_api.src.app.routers.ping.startup_checks import check_backends_at_startup

    await check_backends_at_startup(container, timeout=settings.STARTUP_CHECK_TIMEOUT_SECONDS)


async def close_resources(
    container: InjectQ,
    graph: Any,
    settings: Any,
    meter_provider: Any = None,
    stop_broker: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Close what boot opened, in this order; a failing step is logged and the rest still run.

    1. The graph: core ends the active runs in phases (finish, stop, cancel; each saved
       INTERRUPTED), drains its background work, then releases the checkpointer, publisher
       and store. Everything after it may still be used by that work.
    2. ``stop_broker``: the task broker the API sends with (taskiq closes the worker's own).
    3. The leases, then the cache store they live in: a release that can no longer reach the
       store leaves its slot (a run lock, a cap) held until the lease ttl.
    4. The auth class, which may hold its own clients.
    5. Last, so a slow collector delays nothing else: the final metrics push and AI spans.
    """
    if graph is not None:
        try:
            stats = await graph.aclose(
                grace=settings.SHUTDOWN_GRACE_SECONDS,
                stop_timeout=settings.SHUTDOWN_STOP_SECONDS,
                timeout=settings.SHUTDOWN_TIMEOUT_SECONDS,
            )
            logger.info("Graph closed: %s", stats)
        except Exception as exc:
            logger.warning("Graph close did not finish cleanly: %s", exc)

    if stop_broker is not None:
        try:
            await stop_broker()
        except Exception as exc:
            logger.warning("Task broker close failed: %s", exc)

    await _close_cache_store(container)

    try:
        from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth

        auth = container.try_get(BaseAuth)
        if auth is not None and hasattr(auth, "aclose"):
            await auth.aclose()
    except Exception as exc:
        logger.warning("Auth close failed: %s", exc)

    from tenxgraph_api.src.app.core.config import metrics

    await metrics.flush_telemetry(meter_provider)


async def _close_cache_store(container: InjectQ) -> None:
    """Give back the leases whose releases are still running, then close the cache store."""
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore
    from tenxgraph_api.src.app.core.leases import LeaseManager, wait_for_releases

    try:
        # Only a bound manager: try_get on the unbound class would build a new, empty one.
        leases = container.try_get(LeaseManager) if LeaseManager in container else None
        if leases is not None:
            await leases.aclose()
        else:
            await wait_for_releases()
    except Exception as exc:
        logger.warning("Lease close failed: %s", exc)
    try:
        cache_store = container.try_get(BaseCacheStore)
        if cache_store is not None:
            await cache_store.aclose()
    except Exception as exc:
        logger.warning("Cache store close failed: %s", exc)
