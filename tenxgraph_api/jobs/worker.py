"""The broker the developer imports, and the worker's startup and shutdown.

Built on import from the ``worker`` key of 10xgraph.json (the file the API reads: ``GRAPH_PATH``,
else ``10xgraph.json`` in the cwd). Without that key the broker is in-memory.
"""

import asyncio
import importlib
import logging
import os
import sys
from pathlib import Path

from injectq import InjectQ
from injectq.integrations.taskiq import setup_taskiq
from taskiq import InMemoryBroker, TaskiqEvents, TaskiqState
from taskiq.middlewares import SimpleRetryMiddleware

from tenxgraph_api.jobs import state as job_state
from tenxgraph_api.jobs.config import WorkerConfig, build_broker
from tenxgraph_api.jobs.middleware import TaskMiddleware, wrap_tasks
from tenxgraph_api.jobs.monitoring import MonitoringMiddleware
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, default_config_path


logger = logging.getLogger("tenxgraph_api.jobs")

# Same value as BOOT_CLEANUP_TIMEOUT_SECONDS in src/app/main.py (not imported: it builds the app).
BOOT_CLEANUP_TIMEOUT_SECONDS = 15.0


def _load_graph_config() -> GraphConfig | None:
    path = os.environ.get("GRAPH_PATH") or default_config_path()
    return GraphConfig(path) if Path(path).exists() else None


def import_tasks(cfg: WorkerConfig) -> None:
    """Import the ``worker.tasks`` modules (names or file globs) so their tasks register."""
    for entry in cfg.tasks:
        if entry.endswith(".py") or any(c in entry for c in "*?/"):
            if str(Path.cwd()) not in sys.path:
                sys.path.insert(0, str(Path.cwd()))
            for file in sorted(Path.cwd().glob(entry)):
                importlib.import_module(
                    file.relative_to(Path.cwd()).with_suffix("").as_posix().replace("/", ".")
                )
        else:
            importlib.import_module(entry)


graph_config = _load_graph_config()
worker_config = graph_config.worker if graph_config else None

if worker_config is None:
    logger.warning('No "worker" key in 10xgraph.json: using an in-memory broker (no worker).')
    broker = InMemoryBroker()
else:
    broker = build_broker(worker_config)
    job_state.RESULT_TTL = worker_config.result_ttl

broker.add_middlewares(
    SimpleRetryMiddleware(default_retry_count=worker_config.retries if worker_config else 3),
    MonitoringMiddleware(),
    TaskMiddleware(),
)
if worker_config is not None and worker_config.prometheus is not None:
    from taskiq.middlewares import PrometheusMiddleware

    broker.add_middlewares(
        PrometheusMiddleware(
            server_addr=worker_config.prometheus.host, server_port=worker_config.prometheus.port
        )
    )


async def _prepare_checkpointer(container: InjectQ) -> None:
    """Create or upgrade the checkpointer schema like the API does at boot. Best effort."""
    checkpointer = container.try_get("checkpointer") or container.try_get("BaseCheckpointer")
    asetup = getattr(checkpointer, "asetup", None)
    if not callable(asetup):
        return
    try:
        await asyncio.wait_for(asetup(), BOOT_CLEANUP_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.error(
            "Checkpointer setup failed at startup (%s: %s); it will be retried on first use",
            type(exc).__name__,
            exc,
        )


async def worker_startup(state: TaskiqState) -> None:
    """Build the container and graph like the API does, then find and wrap the tasks."""
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.loader import attach_all_modules, load_container
    from tenxgraph_api.src.app.routers.ping.startup_checks import check_backends_at_startup

    if graph_config is None or worker_config is None:
        raise RuntimeError('The worker needs a "worker" key in 10xgraph.json')
    container = load_container(graph_config.injectq_path) or InjectQ.get_instance()
    container.bind_instance(GraphConfig, graph_config)
    state.container = container
    state.graph = await attach_all_modules(graph_config, container)
    await _prepare_checkpointer(container)
    settings = get_settings()
    if settings.STARTUP_CHECKS:
        await check_backends_at_startup(container, timeout=settings.STARTUP_CHECK_TIMEOUT_SECONDS)
    setup_taskiq(container, broker)
    import_tasks(worker_config)
    wrap_tasks(broker)


async def worker_shutdown(state: TaskiqState) -> None:
    """Close the graph (and with it the checkpointer and store), then the cache store."""
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore
    from tenxgraph_api.src.app.core.config.settings import get_settings

    container = getattr(state, "container", None) or InjectQ.get_instance()
    graph = getattr(state, "graph", None)
    if graph is not None:
        settings = get_settings()
        try:
            await graph.aclose(
                grace=settings.SHUTDOWN_GRACE_SECONDS,
                stop_timeout=settings.SHUTDOWN_STOP_SECONDS,
                timeout=settings.SHUTDOWN_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Graph close did not finish cleanly: %s", exc)
    try:
        cache_store = container.try_get(BaseCacheStore)
        if cache_store is not None:
            await cache_store.aclose()
    except Exception as exc:
        logger.warning("Cache store close failed: %s", exc)


if worker_config is not None:
    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, worker_startup)
    broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, worker_shutdown)
