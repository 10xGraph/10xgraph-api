# Names the socket modules (ws.py, live.py, ws_frames.py) use are imported here too, so every
# name this module offered before the split stays importable from it.
import asyncio  # noqa: F401
import contextlib  # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
from collections import deque  # noqa: F401
from typing import Any

from fastapi import (  # noqa: F401
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.logger import logger
from fastapi.responses import StreamingResponse
from injectq import InjectQ  # noqa: F401
from injectq.integrations.fastapi import InjectFastAPI
from pydantic import ValidationError  # noqa: F401
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ModelNotFoundError, ThreadNotFoundError  # noqa: F401
from tenxgraph.core.realtime.base import ErrorEvent  # noqa: F401
from tenxgraph.core.realtime.queue import LiveInputQueue  # noqa: F401
from tenxgraph.core.state import StreamChunk, StreamEvent  # noqa: F401

from tenxgraph_api.src.app.core.audit import audit_event
from tenxgraph_api.src.app.core.auth.permissions import (
    RequirePermission,
    authorize_resource,  # noqa: F401
    run_credential,
    ws_bearer_subprotocol,  # noqa: F401
)
from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.exceptions.client_errors import (  # noqa: F401
    _sanitize_error_message,
    client_error,
)
from tenxgraph_api.src.app.core.leases import Lease
from tenxgraph_api.src.app.core.threads import require_thread
from tenxgraph_api.src.app.core.utils.log_sanitizer import (
    log_safe,
    summarize_validation_error,  # noqa: F401
)
from tenxgraph_api.src.app.core.validation.request_config import (  # noqa: F401
    ClientInputError,
    normalize_thread_id,
    resource_id_error,
)
from tenxgraph_api.src.app.routers.graph import idempotency
from tenxgraph_api.src.app.routers.graph.realtime_guard import (
    realtime_connection_guard,  # noqa: F401
)
from tenxgraph_api.src.app.routers.graph.run_guard import acquire_run_guard
from tenxgraph_api.src.app.routers.graph.run_limits import release_when_done
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    FixGraphRequestSchema,
    GraphInputSchema,
    GraphInvokeOutputSchema,
    GraphSchema,
    GraphStopSchema,
    GraphToolsSchema,
    ObservabilitySchema,
    WsGraphInputSchema,  # noqa: F401
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import (
    SERVICE_UNAVAILABLE_CODE,  # noqa: F401
    GraphService,
    RunChecks,  # noqa: F401
)
from tenxgraph_api.src.app.routers.graph.thread_lock import (
    THREAD_BUSY_DETAIL,  # noqa: F401
    requested_thread_id,
    thread_write_guard,
)
from tenxgraph_api.src.app.routers.graph.ws_frames import (  # noqa: F401
    _WS_SKIP,
    REALTIME_DRAIN_TIMEOUT,
    REALTIME_INPUT_QUEUE_MAX_BYTES,
    REALTIME_INPUT_QUEUE_MAXSIZE,
    REALTIME_MAX_FRAME_BYTES,
    REALTIME_RECHECK_INTERVAL,
    WS_MAX_INVALID_FRAMES,
    WS_MESSAGE_TOO_BIG,
    WS_NEW_THREAD,
    WS_READ_AHEAD_FRAMES,
    WS_SEND_TIMEOUT_SECONDS,
    WS_SERVICE_RESTART,
    WS_TRY_AGAIN_LATER,
    WsFrameTooLarge,
    WsSendTimeout,
    _cancel_tasks,
    _client_error,
    _close_expired,
    _close_too_big,
    _error_summary,
    _guard_refusal,
    _parse_json_frame,
    _receive_json_frame,
    _resolve_ws_thread,
    _seconds_until_expiry,
    _send_thread_not_found,
    _send_ws_error,
    _SessionExpired,
    _thread_not_found,
    _ws_credential_error,
    _ws_max_frame_chars,
    _ws_run_gate,
    _ws_send,
    _ws_send_bytes,
    _ws_token_revoked,
    _WsFrames,
)
from tenxgraph_api.src.app.utils import success_response
from tenxgraph_api.src.app.utils.swagger_helper import generate_swagger_responses


router = APIRouter(
    tags=["Graph"],
)


async def _http_run_guard(
    service: GraphService,
    graph_input: GraphInputSchema,
    identity: Identity | None,
    request: Request | None = None,
) -> Lease:
    """The thread lock and run slot for an HTTP run (``on_busy`` as the request set it)."""
    thread_id = requested_thread_id(graph_input.config)
    return await acquire_run_guard(
        user=identity,
        runs=service.runs_policy(),
        thread_id=thread_id,
        on_busy=graph_input.on_busy,
        stop=lambda: service.stop_graph(thread_id, identity),
        is_disconnected=request.is_disconnected if request is not None else None,
        recheck=lambda: service.require_run_thread(thread_id, identity),
    )


async def _idempotency(
    request: Request, identity: Identity | None, kind: str
) -> tuple[idempotency.IdempotentRun | None, dict[str, Any] | None]:
    """Take the request's ``Idempotency-Key``, if it sent one: ``(run, replay_record)``."""
    key = request.headers.get(idempotency.IDEMPOTENCY_HEADER) if request is not None else None
    if not isinstance(key, str):
        return None, None
    return await idempotency.check(identity, key, kind, await request.body())


@router.post(
    "/v1/graph/invoke",
    summary="Invoke graph execution",
    responses=generate_swagger_responses(GraphInvokeOutputSchema),
    description="Execute the graph with the provided input and return the final result",
    openapi_extra={},
)
async def invoke_graph(
    request: Request,
    graph_input: GraphInputSchema,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "invoke")),
):
    """
    Invoke the graph with the provided input and return the final result.
    """
    logger.info(f"Graph invoke request received with {len(graph_input.messages)} messages")
    checks = service.check_run_input(graph_input.messages, graph_input.config)
    # A thread the caller cannot run on is a 404 before anything is held.
    await service.require_run_thread(requested_thread_id(graph_input.config), identity)
    # Inline media the run would store gets the upload checks (signature, quota): 422.
    await service.check_inline_media(graph_input.messages, identity)

    # Opt-in Idempotency-Key: a finished run's stored result is returned, a running one is 409.
    idem, replay = await _idempotency(request, identity, "invoke")
    if replay is not None:
        if not replay.get("stored"):
            raise idempotency.refuse_repeat()
        return success_response(
            GraphInvokeOutputSchema.model_validate_json(replay["result"]), request
        )

    # One run at a time per thread (409 when it stays busy) and the per-user run cap (429).
    try:
        guard = await _http_run_guard(service, graph_input, identity, request)
    except BaseException:
        if idem:
            idem.release()
        raise
    try:
        result: GraphInvokeOutputSchema = await service.invoke_graph(
            graph_input,
            identity,
            credential=run_credential(request),
            checks=checks,
        )
        if idem:
            await idem.complete(result.model_dump_json())
    finally:
        guard.release()
        if idem:
            idem.release()

    logger.info("Graph invoke completed successfully")

    return success_response(
        result,
        request,
    )


@router.post(
    "/v1/graph/stream",
    summary="Stream graph execution",
    description="Execute the graph with streaming output for real-time results",
    responses=generate_swagger_responses(StreamChunk),
    openapi_extra={},
)
async def stream_graph(
    graph_input: GraphInputSchema,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "stream")),
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
):
    """
    Stream the graph execution with real-time output.
    """
    logger.info(f"Graph stream request received with {len(graph_input.messages)} messages")
    # Before the response starts, so a blocked URL is a real 400 and a thread the caller
    # cannot run on a real 404, not an error chunk.
    checks = service.check_run_input(graph_input.messages, graph_input.config)
    await service.require_run_thread(requested_thread_id(graph_input.config), identity)
    # Inline media the run would store gets the upload checks (signature, quota): a real 422.
    await service.check_inline_media(graph_input.messages, identity)

    # Opt-in Idempotency-Key: a finished run's final state is replayed, a running one is 409.
    idem, replay = await _idempotency(request, identity, "stream")
    if replay is not None:
        if not replay.get("stored"):
            raise idempotency.refuse_repeat()

        async def replayed():
            for line in idempotency.replay_lines(replay):
                yield line

        return StreamingResponse(replayed(), media_type="application/x-ndjson")

    # Taken before the response starts, so a busy thread still gets a real 409 and an
    # over-cap request a 429.
    try:
        guard = await _http_run_guard(service, graph_input, identity, request)
    except BaseException:
        if idem:
            idem.release()
        raise
    try:
        # Same for a resume aimed at the wrong interrupt: a 409, as invoke answers it. The
        # state read here is the run's one read (kept on ``checks``).
        await service.check_resume_target(graph_input, identity, checks)
    except BaseException:
        guard.release()
        if idem:
            idem.release()
        raise
    lines = service.stream_graph(
        graph_input, identity, credential=run_credential(request), checks=checks
    )
    if idem:
        lines = idempotency.stream_with_record(lines, idem)
    result = release_when_done(lines, guard)

    return StreamingResponse(
        result,
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Content-Encoding": "identity",  # Disable any content encoding (bypasses GZip)
        },
    )


@router.get(
    "/v1/graph",
    summary="Invoke graph execution",
    responses=generate_swagger_responses(GraphSchema),
    description="Execute the graph with the provided input and return the final result",
    openapi_extra={},
)
async def graph_details(
    request: Request,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "read")),
):
    """
    Invoke the graph with the provided input and return the final result.
    """
    logger.info("Graph getting details")

    result: GraphSchema = await service.graph_details()

    logger.info("Graph invoke completed successfully")

    return success_response(
        result,
        request,
    )


@router.get(
    "/v1/graph/tools",
    summary="List graph tools",
    responses=generate_swagger_responses(GraphToolsSchema),
    description=(
        "List the tools exposed by every ToolNode in the graph, grouped by node. "
        "Each tool is tagged with its source (local, mcp, or remote)."
    ),
    openapi_extra={},
)
async def graph_tools(
    request: Request,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "read")),
):
    """List the tools exposed by the graph's tool nodes."""
    logger.info("Graph getting tools")

    result: GraphToolsSchema = await service.get_tools()

    logger.info("Graph tools listed successfully")

    return success_response(
        result,
        request,
    )


@router.get(
    "/v1/observability/{thread_id}",
    summary="Get observability trace for a thread",
    responses=generate_swagger_responses(ObservabilitySchema),
    description=(
        "Reconstruct a run trace (spans, events, token cost) for a thread from the "
        "events its runs emitted. Returns the latest run by default, or ?run_id=..."
    ),
    openapi_extra={},
)
async def observability(
    request: Request,
    thread_id: str,
    run_id: str | None = None,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "read")),
):
    """Return the reconstructed observability trace for a thread."""
    if not is_production():
        logger.info("Observability requested for thread %s", log_safe(thread_id))

    result: ObservabilitySchema = await service.get_observability(thread_id, identity, run_id)

    return success_response(
        result,
        request,
    )


@router.get(
    "/v1/graph:StateSchema",
    summary="Invoke graph execution",
    responses=generate_swagger_responses(GraphSchema),
    description="Execute the graph with the provided input and return the final result",
    openapi_extra={},
)
async def state_schema(
    request: Request,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "read")),
):
    """
    Invoke the graph with the provided input and return the final result.
    """
    logger.info("Graph getting details")

    result: dict = await service.get_state_schema()

    logger.info("Graph invoke completed successfully")

    return success_response(
        result,
        request,
    )


@router.post(
    "/v1/graph/stop",
    summary="Stop graph execution",
    description="Stop the currently running graph execution for a specific thread",
    responses=generate_swagger_responses(dict),  # type: ignore
    openapi_extra={},
)
async def stop_graph(
    request: Request,
    stop_request: GraphStopSchema,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "stop")),
):
    """
    Stop the graph execution for a specific thread.

    Allowed on any thread the caller's reach covers (an admin with reach ``all`` may stop
    another user's run). A thread the caller cannot reach or an unknown one answers 404; a
    visible thread with no run answers 200 with ``ok: false``.

    Args:
        stop_request: Request containing thread_id and optional config

    Returns:
        Status information about the stop operation
    """
    if not is_production():
        logger.info("Graph stop request received for thread: %s", log_safe(stop_request.thread_id))

    await require_thread(service.checkpointer, identity, stop_request.thread_id)
    result = await service.stop_graph(stop_request.thread_id, identity, stop_request.config)
    await audit_event(
        "change",
        "success",
        action="graph:stop",
        resource=f"thread/{stop_request.thread_id}",
        identity=identity,
    )

    if not is_production():
        logger.info("Graph stop completed for thread %s", log_safe(stop_request.thread_id))

    return success_response(
        result,
        request,
    )


@router.post(
    "/v1/graph/fix",
    summary="Fix graph state by removing messages with empty tool calls",
    description=(
        "Fix the graph state by identifying and removing messages that have tool "
        "calls with empty content. This is useful for cleaning up incomplete "
        "tool call messages that may have failed or been interrupted."
    ),
    responses=generate_swagger_responses(dict),  # type: ignore
    openapi_extra={},
)
async def fix_graph(
    request: Request,
    fix_request: FixGraphRequestSchema,
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "fix")),
):
    """
    Fix the graph execution state for a specific thread.

    This endpoint removes messages with empty tool call content from the state.
    Tool calls with empty content typically indicate interrupted or failed tool
    executions that should be cleaned up.

    Args:
        request: HTTP request object
        fix_request: Request containing thread_id and optional config
        service: Injected GraphService instance
        identity: The caller's identity, or None when auth is off

    Returns:
        Status information about the fix operation, including:
        - success: Whether the operation was successful
        - message: Descriptive message about the operation
        - removed_count: Number of messages that were removed
        - state: Updated state after fixing (or original if no changes)

    Raises:
        HTTPException: If the fix operation fails or if no state is found
            for the given thread_id
    """
    if not is_production():
        logger.info("Graph fix request received for thread: %s", log_safe(fix_request.thread_id))

    # Fix writes state, so it stays in the caller's own threads whatever their reach.
    await require_thread(service.checkpointer, identity, fix_request.thread_id, run=True)
    async with thread_write_guard(fix_request.thread_id):
        result = await service.fix_graph(
            fix_request.thread_id,
            identity,
            fix_request.config,
        )
    await audit_event(
        "change",
        "success",
        action="graph:fix",
        resource=f"thread/{fix_request.thread_id}",
        identity=identity,
    )

    if not is_production():
        logger.info("Graph fix completed for thread %s", log_safe(fix_request.thread_id))

    return success_response(
        result,
        request,
    )


# The socket endpoints register on ``router`` after the HTTP routes (the route order is part of
# the OpenAPI document), so ``ws`` comes before ``live``. They import ``router`` from this
# module, so they are imported last. The names tests and callers import from here stay
# importable.
from tenxgraph_api.src.app.routers.graph.ws import (  # noqa: E402, F401, I001
    _is_service_unavailable_chunk,
    _next_frame,
    _next_ws_input,
    _ServiceClosing,
    _ws_media_urls_allowed,
    _ws_run,
    _ws_run_thread_id,
    websocket_graph,
)
from tenxgraph_api.src.app.routers.graph.live import (  # noqa: E402, F401
    _ByteBoundedLiveInputQueue,
    _close_live,
    _dispatch_realtime_control,
    _realtime_event_json,
    realtime_graph_ws,
)
