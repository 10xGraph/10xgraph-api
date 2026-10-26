import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.logger import logger
from fastapi.responses import StreamingResponse
from injectq import InjectQ
from injectq.integrations import InjectAPI
from pydantic import ValidationError
from tenxgraph.core.realtime.base import ErrorEvent
from tenxgraph.core.realtime.queue import LiveInputQueue
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
from tenxgraph_api.src.app.core.auth.permissions import (
    RequirePermission,
    ws_bearer_subprotocol,
)
from tenxgraph_api.src.app.core.auth.request_config import normalize_thread_id
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationUnavailableError,
    token_revoked,
)
from tenxgraph_api.src.app.core.concurrency import Lease
from tenxgraph_api.src.app.routers.graph.realtime_guard import (
    realtime_connection_guard,
    ws_identity_still_valid,
    ws_run_allowed,
)
from tenxgraph_api.src.app.routers.graph.run_limits import (
    acquire_run_slot,
    release_when_done,
    run_slot,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    FixGraphRequestSchema,
    GraphInputSchema,
    GraphInvokeOutputSchema,
    GraphSchema,
    GraphStopSchema,
    GraphToolsSchema,
    ObservabilitySchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.graph.thread_lock import (
    THREAD_BUSY_DETAIL,
    acquire_thread_lock,
    combine_leases,
    lock_requested_thread,
)
from tenxgraph_api.src.app.utils import success_response
from tenxgraph_api.src.app.utils.swagger_helper import generate_swagger_responses


# Grace period (seconds) to let the model's final response drain to the client after the
# client side ends (``close`` control frame or disconnect). Closing the input queue makes
# the live agent finish its turn and stop once the provider goes idle, so this normally
# returns well within the window; it only bounds a provider that never goes idle.
REALTIME_DRAIN_TIMEOUT = 30.0

# ``config.thread_id`` value a WebSocket client sends to start a fresh thread.
WS_NEW_THREAD = "new"

# Bound the upstream audio queue. WebSocket frames bypass RequestSizeLimitMiddleware (it is
# HTTP-only), so the realtime path must guard memory itself. At ~50 input frames/sec a depth
# of 1000 is ~20s of buffered audio; if the provider send stalls past that the oldest frames
# are dropped (logged) instead of growing memory without bound -- the audio was already behind.
REALTIME_INPUT_QUEUE_MAXSIZE = 1000

# Hard cap on a single binary (PCM16) input frame. Normal client chunks are tens of KB
# (16kHz PCM16 = 32KB/s); 1 MiB is ~30s of audio in one frame, far beyond any real chunk.
# Oversized frames are dropped rather than enqueued (again, the middleware does not see them).
REALTIME_MAX_FRAME_BYTES = 1024 * 1024

# RFC 6455 close code 1009 "Message Too Big".
WS_MESSAGE_TOO_BIG = 1009


class WsFrameTooLarge(Exception):
    """A JSON text frame passed the size cap; the socket must be closed with 1009."""


async def _receive_json_frame(websocket: WebSocket, max_chars: int) -> Any:
    """Receive one JSON text frame, refusing it before parsing when it is over ``max_chars``.

    WebSocket frames bypass ``RequestSizeLimitMiddleware`` (it only sees HTTP), and uvicorn
    accepts frames up to 16 MiB by default, so without this a client could hand the JSON parser
    frames far larger than the 10 MB HTTP body cap. ``websocket.receive_text`` raises
    ``WebSocketDisconnect`` on disconnect, like ``receive_json``.
    """
    text = await websocket.receive_text()
    if len(text) > max_chars:
        raise WsFrameTooLarge(f"Frame of {len(text)} characters exceeds the {max_chars} limit")
    return json.loads(text)


def _ws_max_frame_chars() -> int:
    """The JSON frame cap for WebSocket input: the same ``MAX_REQUEST_SIZE`` as HTTP bodies."""
    from tenxgraph_api.src.app.core.config.settings import get_settings

    return get_settings().MAX_REQUEST_SIZE


# Returned by _next_ws_input for a bad frame the socket survives (an error was sent).
_WS_SKIP: Any = object()


async def _send_ws_error(websocket: WebSocket, reason: str) -> None:
    await websocket.send_text(
        StreamChunk(event=StreamEvent.ERROR, data={"reason": reason}).model_dump_json()
    )


async def _next_ws_input(websocket: WebSocket, max_frame: int) -> Any:
    """Read the next run request from ``/v1/graph/ws``.

    Returns the parsed ``WsGraphInputSchema``, ``_WS_SKIP`` for a frame that was rejected with
    an error message (the socket stays open), or ``None`` when the socket is done: the client
    left, or sent a frame over the size cap (closed with 1009).
    """
    try:
        data = await _receive_json_frame(websocket, max_frame)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
        return None
    except WsFrameTooLarge as exc:
        await _close_too_big(websocket, exc)
        return None
    except json.JSONDecodeError as exc:
        logger.warning("WebSocket received non-JSON frame: %s", exc)
        await _send_ws_error(websocket, "Invalid request: frame is not valid JSON")
        return _WS_SKIP
    try:
        return WsGraphInputSchema(**data)
    except Exception as e:
        logger.warning("WebSocket received invalid input: %s", e)
        await _send_ws_error(websocket, f"Invalid request: {e}")
        return _WS_SKIP


async def _close_too_big(websocket: WebSocket, exc: WsFrameTooLarge) -> None:
    logger.warning("WebSocket frame refused: %s", exc)
    with contextlib.suppress(Exception):
        await websocket.send_text(
            StreamChunk(event=StreamEvent.ERROR, data={"reason": str(exc)}).model_dump_json()
        )
    with contextlib.suppress(Exception):
        await websocket.close(code=WS_MESSAGE_TOO_BIG)


router = APIRouter(
    tags=["Graph"],
)


async def _bind_ws_container() -> None:
    """Set the active InjectQ container for a WebSocket handshake.

    ``setup_fastapi`` installs its container-activation via Starlette middleware,
    which runs for HTTP scopes only — WebSocket handshakes bypass it, so every
    ``InjectAPI`` dependency (here and inside ``RequirePermission``) would raise
    "No InjectQ container in current request context". Declaring this dependency
    *before* the ``InjectAPI`` ones makes FastAPI resolve it first, so the
    ContextVar is set by the time the container-backed deps resolve.

    Idempotent and cheap; harmless if the container is already active.
    """
    try:
        from injectq.integrations.fastapi import _request_container

        _request_container.set(InjectQ.get_instance())
    except Exception:  # pragma: no cover - defensive: never block the handshake here
        logger.exception("Failed to bind InjectQ container for WebSocket scope")


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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "invoke")),
):
    """
    Invoke the graph with the provided input and return the final result.
    """
    logger.info(f"Graph invoke request received with {len(graph_input.messages)} messages")
    service.check_media_urls(graph_input.messages)

    # One run at a time per thread; 409 when the thread stays busy.
    thread_lock = await lock_requested_thread(
        graph_input.config,
        graph_input.on_busy,
        lambda thread_id: service.stop_graph(thread_id, user),
    )
    try:
        async with run_slot(user, service.runs_policy()):
            result: GraphInvokeOutputSchema = await service.invoke_graph(
                graph_input,
                user,
            )
    finally:
        thread_lock.release()

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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
):
    """
    Stream the graph execution with real-time output.
    """
    logger.info(f"Graph stream request received with {len(graph_input.messages)} messages")
    # Before the response starts, so a blocked URL is a real 400 and not an error chunk.
    service.check_media_urls(graph_input.messages)

    # Both taken before the response starts, so a busy thread still gets a real 409 and an
    # over-cap request a 429.
    thread_lock = await lock_requested_thread(
        graph_input.config,
        graph_input.on_busy,
        lambda thread_id: service.stop_graph(thread_id, user),
    )
    try:
        slot = await acquire_run_slot(user, service.runs_policy())
    except BaseException:
        thread_lock.release()
        raise
    result = release_when_done(
        service.stream_graph(graph_input, user), combine_leases(slot, thread_lock)
    )

    return StreamingResponse(
        result,
        media_type="text/event-stream",
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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "read", introspection=True)),
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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "read", introspection=True)),
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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "read")),
):
    """Return the reconstructed observability trace for a thread."""
    logger.info(f"Observability requested for thread {thread_id}")

    result: ObservabilitySchema = await service.get_observability(thread_id, run_id)

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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "read", introspection=True)),
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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "stop")),
):
    """
    Stop the graph execution for a specific thread.

    Args:
        stop_request: Request containing thread_id and optional config

    Returns:
        Status information about the stop operation
    """
    logger.info(f"Graph stop request received for thread: {stop_request.thread_id}")

    result = await service.stop_graph(stop_request.thread_id, user, stop_request.config)

    logger.info(f"Graph stop completed for thread {stop_request.thread_id}")

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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "fix")),
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
        user: Current authenticated user

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
    logger.info(f"Graph fix request received for thread: {fix_request.thread_id}")

    result = await service.fix_graph(
        fix_request.thread_id,
        user,
        fix_request.config,
    )

    logger.info(f"Graph fix completed for thread {fix_request.thread_id}")

    return success_response(
        result,
        request,
    )


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ──────────────────────────────────────────────────────────────────────────────


async def _ws_token_revoked(user: dict[str, Any]) -> bool:
    """Whether the socket's token was revoked since the handshake (an outage counts as yes)."""
    try:
        return await token_revoked(user)
    except TokenRevocationUnavailableError:
        return True


async def _ws_run_gate(websocket: WebSocket, user: dict[str, Any]) -> str:
    """Per-run checks for a WebSocket: ``"run"``, ``"skip"`` (rate limited) or ``"close"``.

    The handshake guard runs once, so a socket that stays open must be re-checked before each
    run: the token may have expired or been revoked, and every run counts against the limit.
    """
    if not ws_identity_still_valid(websocket, user) or await _ws_token_revoked(user):
        logger.info("WebSocket credential no longer valid; closing")
        await websocket.send_text(
            StreamChunk(
                event=StreamEvent.ERROR,
                data={"reason": "Session expired. Reconnect with a valid token."},
            ).model_dump_json()
        )
        await websocket.close(code=1008)
        return "close"

    retry_after = await ws_run_allowed(websocket)
    if retry_after is not None:
        await websocket.send_text(
            StreamChunk(
                event=StreamEvent.ERROR,
                data={
                    "reason": f"Rate limit exceeded. Retry after {retry_after}s.",
                    "retry_after_seconds": retry_after,
                },
            ).model_dump_json()
        )
        return "skip"
    return "run"


def _ws_run_thread_id(ws_input: WsGraphInputSchema) -> str | None:
    """Resolve the thread a WebSocket run targets, and make the run use exactly that thread.

    Normalises ``config.thread_id`` the way ``GraphService`` does. ``"new"`` (or a missing
    or blank id) means "start a fresh thread": the key is removed so the service generates
    one, instead of running on a literal thread named ``"new"`` shared by every client.
    """
    config = dict(ws_input.config or {})
    thread_id = _resolve_ws_thread(config)
    ws_input.config = config
    return thread_id


def _resolve_ws_thread(frame: dict[str, Any]) -> str | None:
    """Normalise ``frame["thread_id"]`` in place; ``"new"``, blank or missing removes it."""
    thread_id = normalize_thread_id(frame.get("thread_id"))
    if thread_id == WS_NEW_THREAD:
        thread_id = None
    if thread_id is None:
        frame.pop("thread_id", None)
    else:
        frame["thread_id"] = thread_id
    return thread_id


async def _ws_thread_authorized(
    authz: AuthorizationBackend,
    user: dict[str, Any],
    thread_id: str | None,
    action: str,
) -> bool:
    """Owner-check ``thread_id`` for a WebSocket run.

    Returns ``True`` when the run may proceed. Ownership is only enforced when auth is
    configured (``GraphConfig.auth_config``); an unauthenticated dev graph and fresh
    (``"new"``/absent) threads always pass. If the config cannot be read, the check runs
    anyway: a lookup failure must not switch authorization off.
    """
    if not thread_id or thread_id == WS_NEW_THREAD:
        return True

    from injectq import InjectQ

    has_auth = True
    try:
        from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

        cfg = InjectQ.get_instance().get(GraphConfig)
        has_auth = bool(cfg and cfg.auth_config())
    except Exception as exc:
        logger.warning("Could not resolve GraphConfig for WS auth check; enforcing it: %s", exc)

    if not has_auth:
        return True

    return await authz.authorize(user, "graph", action, resource_id=str(thread_id))


async def _ws_media_urls_allowed(
    websocket: WebSocket, service: GraphService, graph_input: GraphInputSchema
) -> bool:
    """Apply the media URL policy to a WebSocket run; a blocked URL is sent as an error chunk."""
    try:
        service.check_media_urls(graph_input.messages)
    except HTTPException as exc:
        await websocket.send_text(
            StreamChunk(event=StreamEvent.ERROR, data={"reason": str(exc.detail)}).model_dump_json()
        )
        return False
    return True


async def _ws_thread_lock(
    service: GraphService, user: dict[str, Any], thread_id: str | None, on_busy: Any
) -> Lease:
    """The run lock for a WebSocket run (a no-op lease for a thread that does not exist yet)."""
    if thread_id is None:
        return Lease(None)
    return await acquire_thread_lock(
        thread_id, on_busy=on_busy, stop=lambda: service.stop_graph(thread_id, user)
    )


@router.websocket("/v1/graph/ws")
async def websocket_graph(  # noqa: PLR0912, PLR0915
    websocket: WebSocket,
    _bind: None = Depends(_bind_ws_container),
    _guard: None = Depends(realtime_connection_guard),
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    authz: AuthorizationBackend = InjectAPI(AuthorizationBackend),
):
    """
    WebSocket endpoint for streaming graph execution.

    One connection, one run at a time.  The client drives the protocol:

    Fresh run
    ---------
    Client → Server  :  WsGraphInputSchema { invoke_type:"fresh", messages:[…], config:{} }
    Server → Client  :  StreamChunk JSON messages  (same format as POST /v1/graph/stream)
    Server → Client  :  StreamChunk { event:"updates", data:{status:"done"} }

    Resume after remote tool call
    -----------------------------
    (client detects the remote-tool-call chunk in the stream, executes the tool)
    Client → Server  :  WsGraphInputSchema { invoke_type:"resume", tool_result:[…],
                                             config:{thread_id:"<id>"} }
    Server → Client  :  StreamChunk JSON messages  (graph resumes from checkpoint)
    Server → Client  :  StreamChunk { event:"updates", data:{status:"done"} }

    The server does not inspect chunks for tool-call detection — that is the
    client library's responsibility.  invoke_type is only used for validation
    and logging.

    Authentication
    --------------
    Bearer token via the ``Authorization`` header, the ``10xgraph-bearer``
    Sec-WebSocket-Protocol (browser-safe), or the ``?token=`` query fallback —
    identical to the HTTP stream route. Handshakes are subject to the global rate
    limit and the ``websocket.max_connections`` cap.

    Every run on the socket counts against the same rate limit (an error chunk with
    ``retry_after_seconds`` is sent when over it), and the token is re-verified before each
    run.

    Close codes
    -----------
    1000  normal closure (client disconnected cleanly)
    1008  rejected: this graph is a live (realtime) agent — use ``/v1/graph/live``;
          or the token expired / was revoked since the socket opened
    1011  unexpected server error
    1013  rejected: rate limit or connection cap exceeded (try again later)
    """
    await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))
    logger.info("WebSocket graph connection accepted")

    if hasattr(authz, "dependency"):
        from injectq import InjectQ

        try:
            authz = InjectQ.get_instance().get(AuthorizationBackend)
        except Exception:
            from tenxgraph_api.src.app.core.auth.authorization import DefaultAuthorizationBackend

            authz = DefaultAuthorizationBackend()

    # Wrong agent type for this endpoint: a live (realtime) graph cannot be driven over
    # the turn-based stream socket. Reject up front with a clear error instead of failing
    # mid-run when the graph refuses invoke/stream.
    if service.is_live_agent:
        logger.warning("Rejected /v1/graph/ws connection: graph is a live agent")
        with contextlib.suppress(Exception):
            await websocket.send_text(
                StreamChunk(
                    event=StreamEvent.ERROR,
                    data={
                        "reason": "This graph is a live (realtime) agent; connect to "
                        "/v1/graph/live instead of /v1/graph/ws.",
                    },
                ).model_dump_json()
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
        return

    max_frame = _ws_max_frame_chars()
    try:
        while True:
            ws_input = await _next_ws_input(websocket, max_frame)
            if ws_input is None:
                break
            if ws_input is _WS_SKIP:
                continue

            # The handshake checks happen once; these apply to every run on the socket.
            gate = await _ws_run_gate(websocket, user)
            if gate == "close":
                break
            if gate == "skip":
                continue

            thread_id = _ws_run_thread_id(ws_input)
            if not await _ws_thread_authorized(authz, user, thread_id, "stream"):
                logger.warning(
                    f"WebSocket authorization failed for user {user.get('user_id')} "
                    f"on thread {thread_id}"
                )
                await websocket.send_text(
                    StreamChunk(
                        event=StreamEvent.ERROR,
                        data={"reason": f"Not authorized to stream thread {thread_id}"},
                    ).model_dump_json()
                )
                continue

            logger.info(
                "WebSocket graph run: invoke_type=%s, thread_id=%s",
                ws_input.invoke_type,
                thread_id,
            )

            graph_input = ws_input.to_graph_input()
            if not await _ws_media_urls_allowed(websocket, service, graph_input):
                continue
            try:
                thread_lock = await _ws_thread_lock(service, user, thread_id, ws_input.on_busy)
            except HTTPException:
                await websocket.send_text(
                    StreamChunk(
                        event=StreamEvent.ERROR, data={"reason": THREAD_BUSY_DETAIL}
                    ).model_dump_json()
                )
                continue
            try:
                async for chunk_json in service.stream_graph(graph_input, user=user):
                    await websocket.send_text(chunk_json)
            finally:
                thread_lock.release()

            await websocket.send_text(
                StreamChunk(
                    event=StreamEvent.UPDATES,
                    data={"status": "done"},
                ).model_dump_json()
            )

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error("WebSocket graph connection error: %s", e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)


def _client_error(exc: Exception) -> str:
    """The error text a client may see: the exception in development, generic in production.

    A ValueError here can come from anywhere under the session (storage, provider SDK), not
    only from the init frame, and its text may name internal hosts, paths or queries.
    """
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.exceptions.handle_errors import _sanitize_error_message

    is_production = get_settings().MODE == "production"
    return _sanitize_error_message(str(exc), "VALIDATION_ERROR", is_production)


def _dispatch_realtime_control(queue: Any, text: str) -> str | None:
    """Apply one realtime text control frame to the input queue; ``"close"`` ends the session.

    Oversized and non-JSON frames are dropped (logged), never parsed or forwarded.
    """
    if len(text) > REALTIME_MAX_FRAME_BYTES:
        logger.warning(
            "Realtime upstream: dropping oversized control frame (%d chars > %d)",
            len(text),
            REALTIME_MAX_FRAME_BYTES,
        )
        return None
    try:
        control = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Realtime upstream: non-JSON text frame ignored")
        return None
    ctype = control.get("type") if isinstance(control, dict) else None
    if ctype == "text":
        queue.send_text(control.get("text", ""))
    elif ctype == "activity_start":
        queue.send_activity_start()
    elif ctype == "activity_end":
        queue.send_activity_end()
    elif ctype == "close":
        return "close"
    return None


def _realtime_event_json(event: Any) -> str:
    """Serialize a non-audio RealtimeEvent to a JSON text frame for the client."""
    try:
        payload = event.model_dump(mode="json")
    except Exception as e:
        logger.warning("Realtime event serialization failed (%s): %s", type(event).__name__, e)
        payload = {"type": getattr(event, "type", "unknown")}
    return json.dumps(payload)


@router.websocket("/v1/graph/live")
async def realtime_graph_ws(  # noqa: PLR0912, PLR0915
    websocket: WebSocket,
    _bind: None = Depends(_bind_ws_container),
    _guard: None = Depends(realtime_connection_guard),
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    authz: AuthorizationBackend = InjectAPI(AuthorizationBackend),
):
    """Realtime (audio-to-audio) WebSocket bridge over ``CompiledGraph.arealtime``.

    Protocol (provider-neutral; the client never sees Gemini vs OpenAI)
    ------------------------------------------------------------------
    First frame  : JSON control  ``{model, thread_id?, voice?, modalities?, vad?, ...}``
    Upstream     : binary frame   = PCM16 input audio
                   JSON control   = ``{type:"activity_start"|"activity_end"|"text"|"close", ...}``
    Downstream   : binary frame   = PCM16 model audio (``audio_delta``)
                   JSON text frame = every other event (transcripts, turn_complete,
                                     interrupted, tool_call, session/go_away, error)

    Auth: ``RequirePermission("graph","stream")`` — bearer via the ``Authorization`` header,
    the ``10xgraph-bearer`` Sec-WebSocket-Protocol (browser-safe), or the ``?token=`` query
    fallback. Handshakes are subject to the global rate limit and the
    ``websocket.max_connections`` cap (rejected with close code 1013). A non-live
    (turn-based) graph is rejected up front with close code 1008 — use ``/v1/graph/ws``.
    """
    await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))
    logger.info("Realtime WebSocket connection accepted")

    if hasattr(authz, "dependency"):
        from injectq import InjectQ

        try:
            authz = InjectQ.get_instance().get(AuthorizationBackend)
        except Exception:
            from tenxgraph_api.src.app.core.auth.authorization import DefaultAuthorizationBackend

            authz = DefaultAuthorizationBackend()

    # Wrong agent type for this endpoint: the realtime bridge requires a graph rooted at a
    # LiveAgent. Reject a turn-based graph up front with a normalized fatal error instead
    # of accepting the init frame and failing later inside ``arealtime``.
    if not service.is_live_agent:
        logger.warning("Rejected /v1/graph/live connection: graph is not a live agent")
        with contextlib.suppress(Exception):
            await websocket.send_text(
                _realtime_event_json(
                    ErrorEvent(
                        code="not_live",
                        message="This graph is not a live (realtime) agent; use the "
                        "turn-based /v1/graph/ws endpoint instead of /v1/graph/live.",
                        fatal=True,
                    )
                )
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
        return

    try:
        init = await _receive_json_frame(websocket, REALTIME_MAX_FRAME_BYTES)
    except WebSocketDisconnect:
        logger.info("Realtime client disconnected before init")
        return
    except WsFrameTooLarge as exc:
        logger.warning("Realtime init frame refused: %s", exc)
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_MESSAGE_TOO_BIG)
        return
    except Exception as e:
        logger.warning("Realtime init frame invalid: %s", e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1003)
        return

    if isinstance(init, dict):
        # Same rules as /v1/graph/ws: "new" starts a fresh thread, never a shared one.
        thread_id = _resolve_ws_thread(init)
        if not await _ws_thread_authorized(authz, user, thread_id, "stream"):
            logger.warning(
                f"Realtime WebSocket authorization failed for user {user.get('user_id')} "
                f"on thread {thread_id}"
            )
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    _realtime_event_json(
                        ErrorEvent(
                            code="not_authorized",
                            message=f"Not authorized to stream thread {thread_id}",
                            fatal=True,
                        )
                    )
                )
            with contextlib.suppress(Exception):
                await websocket.close(code=1008)
            return

    if not isinstance(init, dict):
        logger.warning("Realtime init frame must be a JSON object, got %s", type(init).__name__)
        with contextlib.suppress(Exception):
            await websocket.close(code=1003)
        return

    queue = LiveInputQueue(maxsize=REALTIME_INPUT_QUEUE_MAXSIZE)

    async def upstream() -> None:
        """Pump client frames into the input queue until close/disconnect."""
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    if len(data) > REALTIME_MAX_FRAME_BYTES:
                        logger.warning(
                            "Realtime upstream: dropping oversized audio frame (%d bytes > %d)",
                            len(data),
                            REALTIME_MAX_FRAME_BYTES,
                        )
                        continue
                    queue.send_audio(data)
                    continue
                text = message.get("text")
                if text is not None and _dispatch_realtime_control(queue, text) == "close":
                    break
        except WebSocketDisconnect:
            logger.info("Realtime client disconnected (upstream)")
        finally:
            queue.close()

    async def downstream() -> None:
        """Stream normalized events back: audio as binary, everything else as JSON."""
        try:
            async for event in service.realtime_graph(queue, init, user):
                if getattr(event, "type", None) == "audio_delta":
                    await websocket.send_bytes(event.data)
                else:
                    await websocket.send_text(_realtime_event_json(event))
        except (ValidationError, ValueError) as e:
            # Bad session config from the init frame (e.g. an invalid ``modalities`` value)
            # is a client error, not a server fault. Send a normalized, fatal error event
            # so the client can show why instead of seeing an opaque 1011 close.
            logger.warning("Realtime session config rejected: %s", e)
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    _realtime_event_json(
                        ErrorEvent(code="invalid_config", message=_client_error(e), fatal=True)
                    )
                )

    up_task = asyncio.create_task(upstream())
    down_task = asyncio.create_task(downstream())
    try:
        _done, pending = await asyncio.wait(
            {up_task, down_task}, return_when=asyncio.FIRST_COMPLETED
        )

        # If the client side finished first (sent ``close`` or disconnected) while the
        # model is still responding, give downstream a bounded grace period to drain the
        # session's final events instead of cutting them off. Closing the input queue ends
        # the live agent's turn and stops it once the provider goes idle, so this returns
        # promptly; on a real disconnect the next send fails fast and downstream ends.
        if down_task in pending:
            queue.close()
            await asyncio.wait({down_task}, timeout=REALTIME_DRAIN_TIMEOUT)

        # Cancel whatever is still running: downstream that overran the grace window, or
        # upstream once the session ended.
        for task in (up_task, down_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        # asyncio.wait never re-raises; surface any failure from the finished task(s)
        # (e.g. arealtime rejecting a non-live graph, or a provider/checkpointer error).
        for task in (up_task, down_task):
            if task.done() and not task.cancelled():
                task.result()
    except WebSocketDisconnect:
        # The client going away mid-session is a normal termination, not a server fault;
        # don't log it as an error or attempt a 1011 close on an already-closed socket.
        logger.info("Realtime WebSocket client disconnected")
    except Exception as e:
        logger.error("Realtime WebSocket error: %s", e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        queue.close()
        with contextlib.suppress(Exception):
            await websocket.close()
