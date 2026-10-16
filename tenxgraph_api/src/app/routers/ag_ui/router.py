from typing import Any

from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from injectq.integrations import InjectAPI

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.request_config import (
    client_error_detail,
    normalize_thread_id,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.routers.graph.run_limits import acquire_run_slot, release_when_done
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

from .converter import check_client_tools, parse_run_input
from .service import stream_ag_ui


router = APIRouter(
    tags=["AG-UI"],
)


@router.post(
    "/v1/ag-ui",
    summary="Run the graph over the AG-UI protocol",
    description=(
        "Accepts an AG-UI RunAgentInput and streams the graph run as AG-UI events over "
        "server-sent events, for AG-UI clients such as CopilotKit. Enabled with "
        '"ag_ui": {"enabled": true} in 10xgraph.json.'
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def run_ag_ui(
    request: Request,
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    # Abstract type as the DI key, as in graph/router.py (see the mypy backlog in pyproject).
    authz: AuthorizationBackend = InjectAPI(AuthorizationBackend),  # type: ignore[type-abstract]
    config: GraphConfig = InjectAPI(GraphConfig),
):
    """
    Stream one AG-UI run.

    AG-UI names the thread ``threadId``, which the generic permission check does not read, so
    thread ownership is checked here, the same way the WebSocket stream checks it: only when
    auth is configured, since an unauthenticated server has no owners to tell apart.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be JSON") from exc
    try:
        run_input = parse_run_input(body)
        check_client_tools(run_input.tools or [])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=client_error_detail(exc)) from exc

    thread_id = normalize_thread_id(run_input.thread_id)
    if thread_id is None:
        raise HTTPException(status_code=422, detail="threadId is required")
    run_input.thread_id = thread_id
    if config.auth_config() and not await authz.authorize(
        user, "graph", "stream", resource_id=thread_id
    ):
        raise HTTPException(status_code=403, detail="Not allowed to access this thread")

    logger.info("AG-UI run %s received for thread %s", run_input.run_id, thread_id)
    encoder = EventEncoder(accept=request.headers.get("accept") or "")
    slot = await acquire_run_slot(user, service.runs_policy())
    return StreamingResponse(
        release_when_done(
            stream_ag_ui(
                service,
                run_input,
                user,
                encoder,
                allow_client_tools=config.ag_ui.allow_client_tools,
            ),
            slot,
        ),
        media_type=encoder.get_content_type(),
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Content-Encoding": "identity",  # Disable any content encoding (bypasses GZip)
        },
    )
