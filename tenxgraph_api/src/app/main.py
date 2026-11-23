import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.core.graph import CompiledGraph

# # Prometheus Instrumentator import
# from prometheus_fastapi_instrumentator import Instrumentator
# from tortoise import Tortoise
from tenxgraph_api.src.app.core import (
    get_settings,
    init_errors_handler,
    init_logger,
    setup_middleware,
)
from tenxgraph_api.src.app.core.audit import configure_audit
from tenxgraph_api.src.app.core.auth.route_guard import (
    DEFAULT_PUBLIC_PATHS,
    assert_all_routes_protected,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, default_config_path
from tenxgraph_api.src.app.core.config.metrics import check_scrape_route, native_telemetry_off
from tenxgraph_api.src.app.core.lifecycle import build_container, check_backends, close_resources
from tenxgraph_api.src.app.loader import attach_all_modules
from tenxgraph_api.src.app.routers import init_routes


logger = logging.getLogger("tenxgraph_api")

# Boot-time checkpointer setup is best effort. It must not hold the app (and so the first
# readiness probe) behind a slow or wedged checkpointer.
BOOT_CLEANUP_TIMEOUT_SECONDS = 15.0

# GraphConfig loads the ``env`` file named in 10xgraph.json, so it must run before Settings
# is first built. `10xgraph dev` loads that file itself, but plain `gunicorn ...main:app`
# (the generated Dockerfile) does not, and Settings is cached after the first read.
graph_path = os.environ.get("GRAPH_PATH") or default_config_path()
graph_config = GraphConfig(graph_path)

settings = get_settings()
# The app's container, with the config bound (the jobs worker builds it the same way).
container: InjectQ = build_container(graph_config)


def _media_store():
    """The media store the API serves files from, or None when media is not wired.

    The app binds ``MediaService`` (never a bare ``media_store``), so that is where it lives.
    """
    try:
        from tenxgraph_api.src.app.routers.media import MediaService

        service = container.try_get(MediaService)
        return service.store if service is not None else None
    except Exception as exc:
        logger.debug("No media store available: %s", exc)
        return None


# Files younger than this are never swept: an upload may still be writing them.
MEDIA_SWEEP_MIN_AGE_SECONDS = 3600.0


async def _sweep_media_loop(interval: float) -> None:
    """Delete files a crash left in the media store, every ``interval`` seconds, forever.

    Runs once straight away (the crash that left them was probably the last shutdown). A store
    without ``sweep_orphans`` (memory, cloud) makes this return at once.
    """
    while True:
        store = _media_store()
        sweep = getattr(store, "sweep_orphans", None)
        if not callable(sweep):
            return
        try:
            removed = await sweep(MEDIA_SWEEP_MIN_AGE_SECONDS)
            if removed:
                logger.info("Media sweeper removed %d orphan files", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Media sweep failed: %s", exc)
        await asyncio.sleep(interval)


async def _prepare_checkpointer() -> None:
    """Create or upgrade the checkpointer's schema at boot when it has a ``asetup`` step.

    A fresh Postgres then works without a manual setup call, and a bad connection string or
    missing permission shows in the boot log instead of on the first request. Best effort:
    the checkpointer also creates its schema on its first query, so a failure here only logs.
    """
    from tenxgraph_api.src.app.routers.ping.readiness import readiness_targets

    # The graph binds its checkpointer under the BaseCheckpointer class (never a string
    # key); the same lookup the startup check and /health/ready use.
    checkpointer = readiness_targets(container)[0]
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


async def _check_backends(app: FastAPI) -> None:
    """Refuse to start while a configured backend is down (``lifecycle.check_backends``)."""
    await check_backends(container, settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    graph: CompiledGraph | None = await attach_all_modules(
        graph_config,
        container=container,
    )

    # Audit sinks, once, now that the auth class is bound (or not).
    from tenxgraph_api import BaseAuth

    configure_audit(graph_config.audit, auth_enabled=container.try_get(BaseAuth) is not None)

    await _prepare_checkpointer()

    # Fail fast, in development and production, while a configured backend is unreachable.
    await _check_backends(app)

    # Background tasks: import the task modules and start the broker for sending.
    if graph_config.worker is not None:
        from tenxgraph_api.src.app.routers.tasks import start_broker

        await start_broker(graph_path, graph_config.worker.tasks, container)

    sweeper: asyncio.Task | None = None
    from tenxgraph_api.src.app.core.config.media_settings import get_media_settings

    sweep_interval = get_media_settings().MEDIA_SWEEP_INTERVAL_SECONDS
    if sweep_interval > 0:
        sweeper = asyncio.create_task(_sweep_media_loop(sweep_interval))

    yield

    app.state.draining = True  # readiness answers 503 from here on
    if sweeper is not None:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)

    # Shutdown. The sweeper is already stopped, so nothing touches the media store while the
    # graph closes. Then the same order as the jobs worker: graph, task broker, leases and
    # cache store, auth, telemetry (``lifecycle.close_resources``).
    stop_broker: Callable[[], Awaitable[None]] | None = None
    if graph_config.worker is not None:
        from tenxgraph_api.src.app.routers.tasks import stop_broker

    await close_resources(
        container,
        graph,
        settings,
        meter_provider=getattr(app.state, "meter_provider", None),
        stop_broker=stop_broker,
    )


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.MODE != "production",
    summary=settings.SUMMARY,
    docs_url=settings.DOCS_PATH if settings.DOCS_PATH else None,
    redoc_url=settings.REDOCS_PATH if settings.REDOCS_PATH else None,
    # The schema lists every route and model; serve it only alongside the docs.
    openapi_url="/openapi.json" if settings.DOCS_PATH or settings.REDOCS_PATH else None,
    lifespan=lifespan,
    root_path=settings.ROOT_PATH,
    **native_telemetry_off(),
)

setup_middleware(app, graph_config=graph_config, container=container)

# attach_injector(app, injector=injector)
setup_fastapi(container=container, app=app)

init_logger(settings.LOG_LEVEL, settings.LOG_FORMAT)

# init error handler
init_errors_handler(app)

# A malformed "media" section should stop the boot, not surface on the first delete.
graph_config.media  # noqa: B018
graph_config.metrics  # noqa: B018
graph_config.audit  # noqa: B018

# init routes
# The AG-UI endpoint is opt-in through 10xgraph.json; the WebSocket routes can be turned off there.
init_routes(
    app,
    ag_ui=graph_config.ag_ui.enabled,
    websocket=graph_config.websocket.enabled,
    tasks=graph_config.worker is not None,
)

# The scrape path is open to the auth guard and rate limit: no real route may share it.
check_scrape_route(app, getattr(app.state, "scrape_route", None))

# Secure by construction: refuse to boot if any non-public route forgot its
# RequirePermission guard (a forgotten guard would otherwise ship an open endpoint).
# The Prometheus scrape route is public too (scrapers send no tokens).
_scrape_path = graph_config.metrics.scrape_path
assert_all_routes_protected(
    app, DEFAULT_PUBLIC_PATHS | {_scrape_path} if _scrape_path else DEFAULT_PUBLIC_PATHS
)

# instrumentator = Instrumentator().instrument(app)  # Instrument first
# instrumentator.expose(app)  # Then expose
