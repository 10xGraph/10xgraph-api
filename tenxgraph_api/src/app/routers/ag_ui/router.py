from typing import Any

from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from injectq.integrations import InjectAPI

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.request_config import (
    ClientInputError,
    client_error_detail,
    normalize_thread_id,
    resource_id_error,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.routers.graph import idempotency
from tenxgraph_api.src.app.routers.graph.run_guard import acquire_run_guard
from tenxgraph_api.src.app.routers.graph.run_limits import release_when_done
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    MediaURLBlockedError,
)

from .converter import check_client_tools, parse_run_input, to_tenxgraph_messages
from .service import stream_ag_ui


# A threadId that asks the server to make the thread (same word as the WebSocket routes).
NEW_THREAD = "new"

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

    # Opt-in Idempotency-Key: a running run is 409; a finished one is 409 too (SSE output is
    # not stored); a different body under the same key is 422.
    idem = None
    idem_key = request.headers.get(idempotency.IDEMPOTENCY_HEADER)
    if isinstance(idem_key, str):
        idem, _ = await idempotency.check(user, idem_key, "ag-ui", body)
        if idem is None:
            raise idempotency.refuse_repeat()
    try:
        return await _run_ag_ui(request, run_input, service, user, authz, config, idem)
    except BaseException:
        if idem is not None:
            idem.release()
        raise


async def _run_ag_ui(request, run_input, service, user, authz, config, idem):
    thread_id = normalize_thread_id(run_input.thread_id)
    if thread_id not in (None, NEW_THREAD) and (
        problem := resource_id_error(thread_id, "threadId")
    ):
        raise ClientInputError(problem)
    if thread_id is None or thread_id == NEW_THREAD:
        # The protocol makes threadId required, so a client that wants a server-made thread
        # sends it blank or "new". The new id comes back in RUN_STARTED.
        thread_id = await service.new_thread_id()
    elif config.auth_config() and not await authz.authorize(
        user, "graph", "stream", resource_id=thread_id
    ):
        # Same as REST: with runs.thread_ids "server" (the default) an id the server has never
        # seen is refused; "client" lets the caller name a new thread.
        raise HTTPException(status_code=403, detail="Not allowed to access this thread")
    run_input.thread_id = thread_id

    # External media URLs in the request, checked before the response starts so a blocked
    # one is a 400. Anything the converter rejects is left to the run, which reports it.
    try:
        service.check_media_urls(to_tenxgraph_messages(run_input.messages))
    except MediaURLBlockedError:
        raise
    except ValueError:
        pass

    if get_settings().MODE != "production":
        logger.info(
            "AG-UI run %s received for thread %s", log_safe(run_input.run_id), log_safe(thread_id)
        )
    encoder = EventEncoder(accept=request.headers.get("accept") or "")
    # One run at a time per thread (AG-UI always waits); taken before the response starts so a
    # busy thread is a real 409.
    guard = await acquire_run_guard(user=user, runs=service.runs_policy(), thread_id=thread_id)
    events = stream_ag_ui(
        service, run_input, user, encoder, allow_client_tools=config.ag_ui.allow_client_tools
    )
    if idem is not None:
        events = idempotency.mark_when_done(events, idem, failed_marker="RUN_ERROR")
    return StreamingResponse(
        release_when_done(events, guard),
        media_type=encoder.get_content_type(),
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Content-Encoding": "identity",  # Disable any content encoding (bypasses GZip)
        },
    )
