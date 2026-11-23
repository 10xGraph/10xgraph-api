"""The broker the developer imports, and the worker's startup and shutdown.

Built on import from the ``worker`` key of 10xgraph.json (the file the API reads: ``GRAPH_PATH``,
else ``10xgraph.json`` in the cwd). Without that key the broker is in-memory.
"""

import logging
import os
from pathlib import Path

from injectq import InjectQ
from injectq.integrations.taskiq import setup_taskiq
from taskiq import InMemoryBroker, TaskiqEvents, TaskiqState
from taskiq.middlewares import SimpleRetryMiddleware

from tenxgraph_api.jobs import state as job_state
from tenxgraph_api.jobs.config import build_broker
from tenxgraph_api.jobs.middleware import TaskMiddleware, wrap_tasks
from tenxgraph_api.jobs.monitoring import MonitoringMiddleware
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, default_config_path


logger = logging.getLogger("tenxgraph_api.jobs")


def _load_graph_config() -> GraphConfig | None:
    path = os.environ.get("GRAPH_PATH") or default_config_path()
    return GraphConfig(path) if Path(path).exists() else None


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


async def worker_startup(state: TaskiqState) -> None:
    """Build the container and graph like the API does, then wrap the tasks.

    The ``taskiq worker`` CLI has imported the task modules before this event.
    """
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config.metrics import setup_metrics
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.config.setup_middleware import (
        _attach_otel_publisher,
        _setup_observability,
        setup_otel_provider,
    )
    from tenxgraph_api.src.app.core.lifecycle import build_container, check_backends

    if graph_config is None or worker_config is None:
        raise RuntimeError('The worker needs a "worker" key in 10xgraph.json')
    container = build_container(graph_config)
    settings = get_settings()
    metrics_cfg = graph_config.metrics
    state.meter_provider = (
        setup_metrics(metrics_cfg, role="worker") if metrics_cfg.enabled else None
    )
    # Same traces as the API; before the graph freezes BasePublisher.
    if settings.OTEL_ENABLED:
        setup_otel_provider(settings, metrics_owned=state.meter_provider is not None)
        _attach_otel_publisher(container, settings, graph_config)
    _setup_observability(container, graph_config)
    state.container = container
    state.graph = await loader.attach_all_modules(graph_config, container)
    await check_backends(container, settings)
    setup_taskiq(container, broker)
    wrap_tasks(broker)


async def worker_shutdown(state: TaskiqState) -> None:
    """Close what startup opened, in the API's order (``lifecycle.close_resources``): the
    graph (and with it the checkpointer and store), the leases, the cache store, the auth
    class, then the last metrics and AI spans. Taskiq bounds this hook
    (``--shutdown-timeout``); the telemetry flush is last so a slow collector cuts only it."""
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.lifecycle import close_resources

    await close_resources(
        getattr(state, "container", None) or InjectQ.get_instance(),
        getattr(state, "graph", None),
        get_settings(),
        meter_provider=getattr(state, "meter_provider", None),
    )


if worker_config is not None:
    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, worker_startup)
    broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, worker_shutdown)
