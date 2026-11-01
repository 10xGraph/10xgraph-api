import asyncio
import logging
import os

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
from tenxgraph_api.src.app.core.auth.route_guard import assert_all_routes_protected
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, default_config_path
from tenxgraph_api.src.app.loader import attach_all_modules, load_container
from tenxgraph_api.src.app.routers import init_routes


logger = logging.getLogger("tenxgraph_api")

# Boot-time temp media cleanup is best effort. It must not hold the app (and so the first
# readiness probe) behind a slow or wedged checkpointer or media store.
BOOT_CLEANUP_TIMEOUT_SECONDS = 15.0

# GraphConfig loads the ``env`` file named in 10xgraph.json, so it must run before Settings
# is first built. `10xgraph dev` loads that file itself, but plain `gunicorn ...main:app`
# (the generated Dockerfile) does not, and Settings is cached after the first read.
graph_path = os.environ.get("GRAPH_PATH") or default_config_path()
graph_config = GraphConfig(graph_path)

settings = get_settings()
# Load the container
container: InjectQ = load_container(graph_config.injectq_path) or InjectQ.get_instance()

# Save config instance
container.bind_instance(GraphConfig, graph_config)


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
    without ``sweep_orphans`` (memory, cloud, older core) makes this return at once.
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


async def _check_backends(app: FastAPI) -> None:
    """Refuse to start while the checkpointer, the store or a required cache store is down.

    Calls ``aping()`` on each, retrying for ``STARTUP_CHECK_TIMEOUT_SECONDS``, then raises
    ``StartupCheckError``, which stops uvicorn and gunicorn with the failed components in the
    log. The cache store is required only by fail-closed features (token revocation, a rate
    limiter with ``fail_open: false``); otherwise an outage is one warning. Off with
    ``STARTUP_CHECKS=false``.
    """
    if not settings.STARTUP_CHECKS:
        logger.warning(
            "STARTUP_CHECKS is off: starting without checking that the configured backends "
            "are reachable."
        )
        return
    from tenxgraph_api.src.app.routers.ping.startup_checks import check_backends_at_startup

    await check_backends_at_startup(container, timeout=settings.STARTUP_CHECK_TIMEOUT_SECONDS)


async def _close_cache_store() -> None:
    """Give back the leases whose releases are still running, then close the cache store.

    A release that can no longer reach the store leaves its slot (a run lock, a cap) held
    until the lease ttl, so the next pod would answer 409 for that thread meanwhile.
    """
    try:
        from tenxgraph_api.src.app.core.cache_store import BaseCacheStore
        from tenxgraph_api.src.app.core.leases import LeaseManager, wait_for_releases

        leases = container.try_get(LeaseManager)
        if leases is not None:
            await leases.aclose()
        else:
            await wait_for_releases()
        cache_store = container.try_get(BaseCacheStore)
        if cache_store is not None:
            await cache_store.aclose()
    except Exception as exc:
        logger.debug("Cache store close failed: %s", exc)


async def _cleanup_temp_media_cache() -> None:
    """Run best-effort cleanup of expired temporary media cache entries."""
    try:
        from tenxgraph.storage.media.temp_cache import TemporaryMediaCache

        checkpointer = container.try_get("checkpointer") or container.try_get("BaseCheckpointer")
        media_store = (
            container.try_get("media_store")
            or container.try_get("BaseMediaStore")
            or _media_store()
        )

        if checkpointer is None:
            logger.debug("No checkpointer available, skipping temp media cache cleanup")
            return

        cache = TemporaryMediaCache()
        cleaned = await cache.cleanup(checkpointer, media_store)
        if cleaned:
            logger.info("Cleaned up %d expired temporary media cache entries on startup", cleaned)
        else:
            logger.debug("No expired temporary media cache entries to clean up")
    except Exception as e:
        logger.warning("Failed to clean up temporary media cache on startup: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    graph: CompiledGraph | None = await attach_all_modules(
        graph_config,
        container=container,
    )

    await _prepare_checkpointer()

    # Fail fast, in development and production, while a configured backend is unreachable.
    await _check_backends(app)

    # Clean up expired temporary media cache on startup
    try:
        await asyncio.wait_for(_cleanup_temp_media_cache(), BOOT_CLEANUP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Temporary media cache cleanup timed out after %ss; continuing startup",
            BOOT_CLEANUP_TIMEOUT_SECONDS,
        )

    # load Store
    # store = load_store(graph_config.store_path)
    # injector.binder.bind(BaseStore, store)

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

    # Shutdown. Order matters: the sweeper is already stopped, so nothing touches the media
    # store while the graph closes. One core call ends active runs in phases (finish, stop,
    # cancel; each saved INTERRUPTED), drains its own background work (events, checkpoint
    # writes) and then releases the checkpointer, publisher and store. The API's own clients
    # close after it because that work may still use them.
    if graph:
        try:
            stats = await graph.aclose(
                grace=settings.SHUTDOWN_GRACE_SECONDS,
                stop_timeout=settings.SHUTDOWN_STOP_SECONDS,
                timeout=settings.SHUTDOWN_TIMEOUT_SECONDS,
            )
            logger.info("Graph closed: %s", stats)
        except Exception as exc:
            logger.warning("Graph close did not finish cleanly: %s", exc)

    # The rate limiter, ownership cache and token revocation share the cache store: closed
    # once here.
    await _close_cache_store()

    # A custom authorization backend may hold its own clients.
    try:
        from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend

        authz = container.try_get(AuthorizationBackend)
        if authz is not None and hasattr(authz, "aclose"):
            await authz.aclose()
    except Exception as exc:
        logger.debug("Authorization backend close failed: %s", exc)


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
)

setup_middleware(app, graph_config=graph_config, container=container)

# attach_injector(app, injector=injector)
setup_fastapi(container=container, app=app)

init_logger(settings.LOG_LEVEL)

# init error handler
init_errors_handler(app)

# A malformed "media" section should stop the boot, not surface on the first delete.
graph_config.media  # noqa: B018

# init routes
# The AG-UI endpoint is opt-in through 10xgraph.json; the WebSocket routes can be turned off there.
init_routes(
    app,
    ag_ui=graph_config.ag_ui.enabled,
    websocket=graph_config.websocket.enabled,
)

# Secure by construction: refuse to boot if any non-public route forgot its
# RequirePermission guard (a forgotten guard would otherwise ship an open endpoint).
assert_all_routes_protected(app)

# instrumentator = Instrumentator().instrument(app)  # Instrument first
# instrumentator.expose(app)  # Then expose
