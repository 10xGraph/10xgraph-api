from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tenxgraph_api.src.app.utils.response_helper import success_response
from tenxgraph_api.src.app.utils.swagger_helper import generate_swagger_responses


router = APIRouter(
    tags=["Ping"],
)


@router.get(
    "/ping",
    summary="Ping the server",
    responses=generate_swagger_responses(str),  # type: ignore
    description="Check the server's health",
    openapi_extra={},
)
async def ping_server(
    request: Request,
):
    """
    Invoke the graph with the provided input and return the final result.
    """
    return success_response(
        "pong",
        request,
    )


@router.get(
    "/health/ready",
    summary="Readiness probe",
    description=(
        "Checks the dependencies a request needs (checkpointer, Redis, store). Answers 200 "
        "when every configured one is reachable and 503 otherwise, and 503 while the server is "
        "shutting down. A Redis outage alone stays 200 unless a configured feature (token "
        "revocation, a fail-closed rate limiter) needs it. /ping stays the liveness "
        "probe. Public, like /ping: it reports only ok or fail per configured component, and the "
        "result is cached for a few seconds."
    ),
)
async def readiness_probe(request: Request):
    from injectq import InjectQ
    from tenxgraph.storage.checkpointer import BaseCheckpointer
    from tenxgraph.storage.store import BaseStore

    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.utils.container import current_container

    from .readiness import cached_readiness, is_ready, redis_required

    if getattr(request.app.state, "draining", False):
        # Shutting down: leave the load balancer's rotation before the sockets close.
        return JSONResponse(status_code=503, content={"status": "draining", "checks": {}})

    container: InjectQ = current_container()
    graph_config = container.try_get(GraphConfig) if GraphConfig in container else None
    redis_url = getattr(graph_config, "redis_url", None) or get_settings().REDIS_URL
    checks = await cached_readiness(
        container.try_get(BaseCheckpointer) if BaseCheckpointer in container else None,
        container.try_get(BaseStore) if BaseStore in container else None,
        redis_url if isinstance(redis_url, str) else None,
    )
    ready = is_ready(checks, redis_is_required=redis_required(graph_config))
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
