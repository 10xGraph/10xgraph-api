import logging
import os

from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.core.graph import CompiledGraph
from tenxgraph.utils.background_task_manager import BackgroundTaskManager

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

# How long shutdown waits for in-flight background work (event publishing,
# checkpoint writes) before giving up. Bounded on purpose: a wedged sink must not
# hold the pod open until the kubelet SIGKILLs it, which would lose more than
# draining for a fixed window and then moving on.
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0

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


async def _cleanup_temp_media_cache() -> None:
    """Run best-effort cleanup of expired temporary media cache entries."""
    try:
        from tenxgraph.storage.media.temp_cache import TemporaryMediaCache

        checkpointer = container.try_get("checkpointer") or container.try_get("BaseCheckpointer")
        media_store = container.try_get("media_store") or container.try_get("BaseMediaStore")

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
    # Load the cache
    # RedisCacheBackend(settings.REDIS_URL)

    graph: CompiledGraph | None = await attach_all_modules(
        graph_config,
        container=container,
    )

    # Clean up expired temporary media cache on startup
    await _cleanup_temp_media_cache()

    # load Store
    # store = load_store(graph_config.store_path)
    # injector.binder.bind(BaseStore, store)

    yield

    # Shutdown. Order matters: drain in-flight work BEFORE tearing down the
    # resources that work depends on.
    #
    # Previously this went straight to `graph.aclose()`, which released the graph's
    # resources while background work (publisher emits, checkpoint writes) could
    # still be in flight -- so a rolling deploy could drop the tail end of a run's
    # events and its final checkpoint.
    await _drain_background_tasks()

    if graph:
        # release all the resources
        await graph.aclose()

    # Close rate-limit backend (e.g. Redis connection pool)
    backend = getattr(app.state, "rate_limit_backend", None)
    if backend is not None:
        await backend.close()

    # Close the shared concurrency limiter (its Redis client), if one is bound.
    try:
        from tenxgraph_api.src.app.core.concurrency import ConcurrencyLimiter

        limiter = container.try_get(ConcurrencyLimiter)
        if limiter is not None:
            await limiter.aclose()
    except Exception as exc:
        logger.debug("Concurrency limiter close failed: %s", exc)

    # Close the token revocation backend (its Redis client), if one is bound.
    try:
        from tenxgraph_api.src.app.core.auth.revocation import TokenRevocationBackend

        revocation = container.try_get(TokenRevocationBackend)
        if revocation is not None:
            await revocation.aclose()
    except Exception as exc:
        logger.debug("Token revocation backend close failed: %s", exc)

    # Close the authorization backend's L2 Redis client, if it created one.
    try:
        from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend

        authz = container.try_get(AuthorizationBackend)
        if authz is not None and hasattr(authz, "aclose"):
            await authz.aclose()
    except Exception as exc:
        logger.debug("Authorization backend close failed: %s", exc)


async def _drain_background_tasks(timeout: float = SHUTDOWN_DRAIN_TIMEOUT_SECONDS) -> None:
    """Wait for in-flight background tasks (event publishing, checkpoint writes).

    Bounded: if the sink is wedged we still shut down, rather than hanging the pod
    until the kubelet SIGKILLs it -- which would lose strictly more than draining
    for a bounded window and then giving up.
    """
    try:
        task_manager = InjectQ.get_instance().try_get(BackgroundTaskManager)
    except Exception:  # container not configured; nothing to drain
        return

    if task_manager is None:
        return

    pending = getattr(task_manager, "pending_count", 0)
    if not pending:
        return

    logger.info("Draining %d in-flight background tasks before shutdown", pending)
    try:
        await task_manager.wait_for_all(timeout=timeout)
        logger.info("Background tasks drained")
    except Exception as e:
        logger.warning("Background task drain did not finish cleanly: %s", e)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.MODE == "DEVELOPMENT",
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

# init routes
# The evals viewer reads local report files and has no auth, so it is a development tool only.
# The AG-UI endpoint is opt-in through 10xgraph.json; the WebSocket routes can be turned off there.
init_routes(
    app,
    evals=settings.MODE != "production",
    ag_ui=graph_config.ag_ui.enabled,
    websocket=graph_config.websocket.enabled,
)

# Secure by construction: refuse to boot if any non-public route forgot its
# RequirePermission guard (a forgotten guard would otherwise ship an open endpoint).
assert_all_routes_protected(app)

# instrumentator = Instrumentator().instrument(app)  # Instrument first
# instrumentator.expose(app)  # Then expose
