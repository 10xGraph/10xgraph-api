import asyncio
import contextlib
import json
import time
from collections import deque
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
from tenxgraph_api.src.app.core.auth.request_config import (
    ClientInputError,
    normalize_thread_id,
    resource_id_error,
)
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenRevocationUnavailableError,
    token_revoked,
)
from tenxgraph_api.src.app.core.leases import Lease
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe, summarize_validation_error
from tenxgraph_api.src.app.routers.graph import idempotency
from tenxgraph_api.src.app.routers.graph.realtime_guard import (
    realtime_connection_guard,
    ws_identity_still_valid,
    ws_run_allowed,
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
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import (
    SERVICE_UNAVAILABLE_CODE,
    GraphService,
)
from tenxgraph_api.src.app.routers.graph.thread_lock import (
    THREAD_BUSY_DETAIL,
    requested_thread_id,
    thread_write_guard,
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

# Total bytes of input (audio, text, image) a live session may have buffered at once, on top of
# the frame-count bound. 1000 frames of up to 1 MiB would otherwise be ~1 GiB per socket. 16 MiB
# is ~8 minutes of 16 kHz PCM16 audio (32 KB/s), far more than the ~20 s the count bound allows
# in normal use, so a healthy session never reaches it. Over the budget a frame is dropped (logged).
REALTIME_INPUT_QUEUE_MAX_BYTES = 16 * 1024 * 1024

# Seconds between credential re-checks during a live session (token expiry and revocation).
# A live session has no per-run boundary to hook into, so a timer bounds how long a token that
# expired or was revoked keeps streaming. Each check is one local token verification plus one
# revocation lookup (a single Redis GET when Redis is used), so 30 s costs ~2 lookups a minute per
# session while keeping the exposure window short next to a session that can run for hours.
REALTIME_RECHECK_INTERVAL = 30.0

# RFC 6455 close code 1013 "Try Again Later", for a refused or shed run.
WS_TRY_AGAIN_LATER = 1013

# RFC 6455 close code 1012 "Service Restart", for a run refused because the graph is closing.
WS_SERVICE_RESTART = 1012

# Longest one write to a client socket may take. A reader that stops reading would otherwise
# park the run (and the thread lock and run slot it holds) on a full send buffer for good. This
# bounds our own socket write only, never the run: a quiet run, however long, is not affected.
WS_SEND_TIMEOUT_SECONDS = 30.0

# Frames of a ``/v1/graph/ws`` socket read ahead while a run is in progress. Reading is what
# notices a client that left, so its run can be stopped; the cap keeps a chatty client from
# buffering without bound (past it the socket is left to TCP back-pressure).
WS_READ_AHEAD_FRAMES = 2

# Frames in a row that fail to parse or validate before the socket is closed (1008). Valid
# frames reset the count. Each one also counts against the rate limit, like a run does.
WS_MAX_INVALID_FRAMES = 20


class WsFrameTooLarge(Exception):
    """A JSON text frame passed the size cap; the socket must be closed with 1009."""


class WsSendTimeout(Exception):
    """A client socket did not take a write in time; the socket must be closed."""


async def _ws_send(websocket: WebSocket, text: str) -> None:
    """``send_text`` that gives up on a client that is not reading (``WsSendTimeout``)."""
    try:
        await asyncio.wait_for(websocket.send_text(text), WS_SEND_TIMEOUT_SECONDS)
    except TimeoutError:
        raise WsSendTimeout from None


async def _ws_send_bytes(websocket: WebSocket, data: bytes) -> None:
    try:
        await asyncio.wait_for(websocket.send_bytes(data), WS_SEND_TIMEOUT_SECONDS)
    except TimeoutError:
        raise WsSendTimeout from None


async def _cancel_tasks(*tasks: asyncio.Task[Any]) -> None:
    """Cancel ``tasks`` and wait until they are gone.

    Their own cancellation is absorbed; a cancel aimed at the caller still propagates (it is
    not swallowed the way ``suppress(CancelledError)`` around ``await task`` would).
    """
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class _WsFrames:
    """The text frames of a ``/v1/graph/ws`` socket.

    While a run is in progress :meth:`read_ahead` keeps reading, so a client that leaves is
    noticed (``gone``) and its run can be stopped; frames it sent meanwhile are kept, in order,
    for the next :meth:`next`.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._ahead: deque[str] = deque()
        self.gone = False

    async def next(self) -> str:
        if self._ahead:
            return self._ahead.popleft()
        if self.gone:
            raise WebSocketDisconnect
        return await self._websocket.receive_text()

    async def read_ahead(self) -> None:
        try:
            while len(self._ahead) < WS_READ_AHEAD_FRAMES:
                self._ahead.append(await self._websocket.receive_text())
        except Exception:  # the client left (or the socket is already closed)
            self.gone = True


def _parse_json_frame(text: str, max_chars: int) -> Any:
    if len(text) > max_chars:
        raise WsFrameTooLarge(f"Frame of {len(text)} characters exceeds the {max_chars} limit")
    return json.loads(text)


async def _receive_json_frame(websocket: WebSocket, max_chars: int) -> Any:
    """Receive one JSON text frame, refusing it before parsing when it is over ``max_chars``.

    WebSocket frames bypass ``RequestSizeLimitMiddleware`` (it only sees HTTP), and uvicorn
    accepts frames up to 16 MiB by default, so without this a client could hand the JSON parser
    frames far larger than the 10 MB HTTP body cap. ``websocket.receive_text`` raises
    ``WebSocketDisconnect`` on disconnect, like ``receive_json``.
    """
    return _parse_json_frame(await websocket.receive_text(), max_chars)


def _ws_max_frame_chars() -> int:
    """The JSON frame cap for WebSocket input: the same ``MAX_REQUEST_SIZE`` as HTTP bodies."""
    from tenxgraph_api.src.app.core.config.settings import get_settings

    return get_settings().MAX_REQUEST_SIZE


def _is_production() -> bool:
    """True in production, where request log lines that carry client thread ids are not emitted."""
    from tenxgraph_api.src.app.core.config.settings import get_settings

    return get_settings().MODE == "production"


# Returned by _next_ws_input for a bad frame the socket survives (an error was sent).
_WS_SKIP: Any = object()


async def _send_ws_error(websocket: WebSocket, reason: str) -> None:
    await websocket.send_text(
        StreamChunk(event=StreamEvent.ERROR, data={"reason": reason}).model_dump_json()
    )


def _seconds_until_expiry(user: dict[str, Any]) -> float | None:
    """Seconds until the identity's ``exp`` claim, or ``None`` when it has none."""
    exp = user.get("exp") if isinstance(user, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    return max(float(exp) - time.time(), 0.0) + 0.5


class _SessionExpired(Exception):
    """The socket sat idle past its token's expiry."""


async def _next_frame(frames: _WsFrames, websocket: WebSocket, user: dict[str, Any]) -> str:
    """The next text frame; an idle socket is cut once its credential stops verifying.

    Without this, a socket that sends nothing keeps its slot long after the token that opened
    it expired (the per-run check only runs when a frame arrives).
    """
    while True:
        wait = _seconds_until_expiry(user)
        if wait is None:
            return await frames.next()
        try:
            return await asyncio.wait_for(frames.next(), wait)
        except TimeoutError:
            if not ws_identity_still_valid(websocket, user):
                raise _SessionExpired from None


async def _close_expired(websocket: WebSocket) -> None:
    logger.info("WebSocket credential expired while idle; closing")
    with contextlib.suppress(Exception):
        await websocket.send_text(
            StreamChunk(
                event=StreamEvent.ERROR,
                data={"reason": "Session expired. Reconnect with a valid token."},
            ).model_dump_json()
        )
    with contextlib.suppress(Exception):
        await websocket.close(code=1008)


async def _next_ws_input(
    frames: _WsFrames,
    websocket: WebSocket,
    max_frame: int,
    user: dict[str, Any] | None = None,
) -> Any:
    """Read the next run request from ``/v1/graph/ws``.

    Returns the parsed ``WsGraphInputSchema``, ``_WS_SKIP`` for a frame that was rejected with
    an error message (the socket stays open), or ``None`` when the socket is done: the client
    left, sent a frame over the size cap (closed with 1009), or its token expired while idle.
    """
    try:
        text = await _next_frame(frames, websocket, user or {})
        data = _parse_json_frame(text, max_frame)
    except _SessionExpired:
        await _close_expired(websocket)
        return None
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
        ws_input = WsGraphInputSchema(**data)
        if problem := resource_id_error((ws_input.config or {}).get("thread_id"), "thread_id"):
            raise ClientInputError(problem)
        return ws_input
    except Exception as e:
        logger.warning("WebSocket received invalid input: %s", _error_summary(e))
        # The validation text can quote the submitted values; production sends the generic one.
        await _send_ws_error(websocket, f"Invalid request: {_client_error(e)}")
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


async def _http_run_guard(
    service: GraphService,
    graph_input: GraphInputSchema,
    user: dict[str, Any],
    request: Request | None = None,
) -> Lease:
    """The thread lock and run slot for an HTTP run (``on_busy`` as the request set it)."""
    thread_id = requested_thread_id(graph_input.config)
    return await acquire_run_guard(
        user=user,
        runs=service.runs_policy(),
        thread_id=thread_id,
        on_busy=graph_input.on_busy,
        stop=lambda: service.stop_graph(thread_id, user),
        is_disconnected=request.is_disconnected if request is not None else None,
    )


async def _idempotency(
    request: Request, user: dict[str, Any], kind: str
) -> tuple[idempotency.IdempotentRun | None, dict[str, Any] | None]:
    """Take the request's ``Idempotency-Key``, if it sent one: ``(run, replay_record)``."""
    key = request.headers.get(idempotency.IDEMPOTENCY_HEADER) if request is not None else None
    if not isinstance(key, str):
        return None, None
    return await idempotency.check(user, key, kind, await request.body())


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

    # Opt-in Idempotency-Key: a finished run's stored result is returned, a running one is 409.
    idem, replay = await _idempotency(request, user, "invoke")
    if replay is not None:
        if not replay.get("stored"):
            raise idempotency.refuse_repeat()
        return success_response(
            GraphInvokeOutputSchema.model_validate_json(replay["result"]), request
        )

    # One run at a time per thread (409 when it stays busy) and the per-user run cap (429).
    try:
        guard = await _http_run_guard(service, graph_input, user, request)
    except BaseException:
        if idem:
            idem.release()
        raise
    try:
        result: GraphInvokeOutputSchema = await service.invoke_graph(
            graph_input,
            user,
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
    service: GraphService = InjectAPI(GraphService),
    user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
):
    """
    Stream the graph execution with real-time output.
    """
    logger.info(f"Graph stream request received with {len(graph_input.messages)} messages")
    # Before the response starts, so a blocked URL is a real 400 and not an error chunk.
    service.check_media_urls(graph_input.messages)

    # Opt-in Idempotency-Key: a finished run's final state is replayed, a running one is 409.
    idem, replay = await _idempotency(request, user, "stream")
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
        guard = await _http_run_guard(service, graph_input, user, request)
    except BaseException:
        if idem:
            idem.release()
        raise
    try:
        # Same for a resume aimed at the wrong interrupt: a 409, as invoke answers it.
        await service.check_resume_target(graph_input, user)
    except BaseException:
        guard.release()
        if idem:
            idem.release()
        raise
    lines = service.stream_graph(graph_input, user)
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
    if not _is_production():
        logger.info("Observability requested for thread %s", log_safe(thread_id))

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
    if not _is_production():
        logger.info("Graph stop request received for thread: %s", log_safe(stop_request.thread_id))

    result = await service.stop_graph(stop_request.thread_id, user, stop_request.config)

    if not _is_production():
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
    if not _is_production():
        logger.info("Graph fix request received for thread: %s", log_safe(fix_request.thread_id))

    async with thread_write_guard(fix_request.thread_id):
        result = await service.fix_graph(
            fix_request.thread_id,
            user,
            fix_request.config,
        )

    if not _is_production():
        logger.info("Graph fix completed for thread %s", log_safe(fix_request.thread_id))

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

    Follows ``runs.thread_ids`` like REST: under ``"server"`` (the default) an id the server
    has never seen is refused; ``"client"`` lets the caller name a new thread.

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


def _guard_refusal(exc: HTTPException) -> str:
    """The reason a run was refused (thread busy, run cap) as a client-facing sentence."""
    return THREAD_BUSY_DETAIL if exc.status_code == 409 else str(exc.detail)  # noqa: PLR2004


class _ServiceClosing(Exception):
    """A run was refused because the graph is closing; the error frame is already sent."""


def _is_service_unavailable_chunk(chunk_json: str) -> bool:
    """Whether a serialized chunk is the error chunk of a run refused while closing."""
    if not chunk_json.startswith('{"event":"error"'):
        return False
    try:
        data = json.loads(chunk_json).get("data")
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("code") == SERVICE_UNAVAILABLE_CODE


async def _ws_run(
    websocket: WebSocket,
    service: GraphService,
    graph_input: GraphInputSchema,
    user: dict[str, Any],
    frames: _WsFrames,
) -> bool:
    """Stream one run to the socket; ``False`` when the client left and the run was stopped.

    The run is its own task, so a client that leaves while the run is quiet (a long tool call)
    is noticed and the run cancelled, instead of holding the lock and slot until its next
    write. The run generator is closed before this returns, so the caller can release the
    thread lock knowing the run is over.
    """
    stream = service.stream_graph(graph_input, user=user)

    async def pump() -> None:
        async with contextlib.aclosing(stream):
            async for chunk_json in stream:
                await _ws_send(websocket, chunk_json)
                if _is_service_unavailable_chunk(chunk_json):
                    raise _ServiceClosing

    run_task = asyncio.create_task(pump())
    reader = asyncio.create_task(frames.read_ahead())
    try:
        await asyncio.wait({run_task, reader}, return_when=asyncio.FIRST_COMPLETED)
        if not run_task.done() and frames.gone:
            logger.info("WebSocket client left during a run; stopping the run")
            run_task.cancel()
        await asyncio.wait({run_task})
    finally:
        await _cancel_tasks(run_task, reader)
    if run_task.cancelled():
        return False
    run_task.result()
    return True


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
    frames = _WsFrames(websocket)
    invalid_frames = 0
    try:
        while True:
            ws_input = await _next_ws_input(frames, websocket, max_frame, user)
            if ws_input is None:
                break
            if ws_input is _WS_SKIP:
                # A frame that was refused still cost a parse: it counts against the rate
                # limit, and a socket that only sends junk is closed.
                invalid_frames += 1
                retry_after = await ws_run_allowed(websocket)
                if retry_after is not None or invalid_frames > WS_MAX_INVALID_FRAMES:
                    code = 1008 if retry_after is None else WS_TRY_AGAIN_LATER
                    logger.warning("WebSocket closed after too many invalid frames")
                    with contextlib.suppress(Exception):
                        await websocket.close(code=code)
                    break
                continue
            invalid_frames = 0

            # The handshake checks happen once; these apply to every run on the socket.
            gate = await _ws_run_gate(websocket, user)
            if gate == "close":
                break
            if gate == "skip":
                continue

            thread_id = _ws_run_thread_id(ws_input)
            if not await _ws_thread_authorized(authz, user, thread_id, "stream"):
                logger.warning(
                    "WebSocket authorization failed for user %s on thread %s",
                    log_safe(user.get("user_id")),
                    log_safe(thread_id),
                )
                await websocket.send_text(
                    StreamChunk(
                        event=StreamEvent.ERROR,
                        data={"reason": f"Not authorized to stream thread {thread_id}"},
                    ).model_dump_json()
                )
                continue

            if not _is_production():
                logger.info(
                    "WebSocket graph run: invoke_type=%s, thread_id=%s",
                    ws_input.invoke_type,
                    log_safe(thread_id),
                )

            graph_input = ws_input.to_graph_input()
            if not await _ws_media_urls_allowed(websocket, service, graph_input):
                continue
            idem = None
            if ws_input.idempotency_key is not None:
                try:
                    idem, _ = await idempotency.check(
                        user,
                        ws_input.idempotency_key,
                        "ws",
                        ws_input.model_dump(mode="json", exclude={"idempotency_key"}),
                    )
                    if idem is None:  # finished before: a socket run's output is not replayable
                        raise idempotency.refuse_repeat()
                except HTTPException as exc:
                    await _send_ws_error(websocket, str(exc.detail))
                    continue
                except ClientInputError as exc:
                    await _send_ws_error(websocket, str(exc))
                    continue
            try:
                guard = await acquire_run_guard(
                    user=user,
                    runs=service.runs_policy(),
                    thread_id=thread_id,
                    on_busy=ws_input.on_busy,
                    stop=lambda tid=thread_id: service.stop_graph(tid, user),
                )
            except HTTPException as exc:
                if idem:
                    idem.release()
                await websocket.send_text(
                    StreamChunk(
                        event=StreamEvent.ERROR, data={"reason": _guard_refusal(exc)}
                    ).model_dump_json()
                )
                continue
            try:
                finished = await _ws_run(websocket, service, graph_input, user, frames)
                if idem and finished:
                    await idem.complete(None)
            finally:
                guard.release()
                if idem:
                    idem.release()
            if not finished:
                break

            await _ws_send(
                websocket,
                StreamChunk(
                    event=StreamEvent.UPDATES,
                    data={"status": "done"},
                ).model_dump_json(),
            )

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except _ServiceClosing:
        # The SERVICE_UNAVAILABLE error frame went out; now tell the client to reconnect.
        logger.info("WebSocket run refused: the graph is closing")
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_SERVICE_RESTART)
    except WsSendTimeout:
        logger.warning("WebSocket client is not reading; closing the socket")
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_TRY_AGAIN_LATER)
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
    return _sanitize_error_message(_error_summary(exc), "VALIDATION_ERROR", is_production)


def _error_summary(exc: Exception) -> str:
    """``exc`` as text; a pydantic validation error without the values it quotes."""
    if isinstance(exc, ValidationError):
        return summarize_validation_error(exc)
    return str(exc)


class _ByteBoundedLiveInputQueue(LiveInputQueue):
    """``LiveInputQueue`` that also bounds the bytes it holds, not just the frame count.

    A frame that would push the buffered payload over ``max_bytes`` is dropped (logged), like a
    frame that does not fit the count bound. Bytes are released as the consumer takes frames.
    """

    def __init__(self, maxsize: int = 0, max_bytes: int = REALTIME_INPUT_QUEUE_MAX_BYTES) -> None:
        super().__init__(maxsize=maxsize)
        self._max_bytes = max_bytes
        self._buffered = 0

    @staticmethod
    def _size(item: Any) -> int:
        if item.data is not None:
            return len(item.data)
        return len(item.text.encode()) if item.text is not None else 0

    def _put(self, item: Any) -> None:
        size = self._size(item)
        if self._buffered + size > self._max_bytes:
            logger.warning(
                "Realtime input queue over its byte budget (%d + %d > %d); dropping %s frame",
                self._buffered,
                size,
                self._max_bytes,
                item.kind,
            )
            return
        before = self._queue.qsize()
        super()._put(item)
        if self._queue.qsize() > before:  # not dropped as full or closed
            self._buffered += size

    def _released(self, item: Any) -> Any:
        self._buffered = max(0, self._buffered - self._size(item))
        return item

    async def get(self) -> Any:
        return self._released(await super().get())

    def get_nowait(self) -> Any:
        return self._released(super().get_nowait())

    async def __aiter__(self) -> Any:
        while True:
            item = self._released(await self._queue.get())
            if item.kind == "close":
                return
            yield item


async def _live_credential_error(websocket: WebSocket, user: dict[str, Any]) -> bool:
    """Whether a live session's credential no longer holds (expired, revoked or backend down).

    Same two checks as ``_ws_run_gate`` uses before each ``/v1/graph/ws`` run: the token is
    re-verified (catches expiry) and the revocation backend is asked (an outage counts as
    revoked, so it fails closed).
    """
    return not ws_identity_still_valid(websocket, user) or await _ws_token_revoked(user)


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
async def realtime_graph_ws(  # noqa: PLR0911, PLR0912, PLR0915
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
                   (``thread_id`` absent, blank or ``"new"`` makes the server create the thread)
    First reply  : JSON ``{type:"session_started", thread_id}``; reconnect with that id to
                   continue the thread
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

    thread_id: str | None = None
    if isinstance(init, dict):
        # Same rules as /v1/graph/ws: "new" starts a fresh thread, never a shared one.
        thread_id = _resolve_ws_thread(init)
        if problem := resource_id_error(thread_id, "thread_id"):
            logger.warning("Realtime init frame refused: %s", problem)
            with contextlib.suppress(Exception):
                await websocket.send_text(
                    _realtime_event_json(
                        ErrorEvent(code="VALIDATION_ERROR", message=problem, fatal=True)
                    )
                )
            with contextlib.suppress(Exception):
                await websocket.close(code=1003)
            return
        if not await _ws_thread_authorized(authz, user, thread_id, "stream"):
            logger.warning(
                "Realtime WebSocket authorization failed for user %s on thread %s",
                log_safe(user.get("user_id")),
                log_safe(thread_id),
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

    if thread_id is None:
        # No id (absent, blank or "new"): the server makes the thread, as AG-UI does, so there
        # is nothing to owner-check. The id goes back to the client in ``session_started``.
        thread_id = await service.new_thread_id()
        init["thread_id"] = thread_id

    async def close_session_expired() -> None:
        logger.info("Realtime credential no longer valid; closing")
        with contextlib.suppress(Exception):
            await websocket.send_text(
                _realtime_event_json(
                    ErrorEvent(
                        code="session_expired",
                        message="Session expired. Reconnect with a valid token.",
                        fatal=True,
                    )
                )
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=1008, reason="Session expired")

    # The handshake guard ran before the init frame; re-check before the session (the run)
    # starts, then on a timer for as long as it lasts.
    if await _live_credential_error(websocket, user):
        await close_session_expired()
        return

    # The same guard as every other run: one session at a time on a thread, and the session
    # counts against the user's run cap for as long as it lasts.
    try:
        guard = await acquire_run_guard(
            user=user, runs=service.runs_policy(), thread_id=thread_id, on_busy="wait"
        )
    except HTTPException as exc:
        busy = exc.status_code == 409  # noqa: PLR2004
        with contextlib.suppress(Exception):
            await websocket.send_text(
                _realtime_event_json(
                    ErrorEvent(
                        code="thread_busy" if busy else "too_many_runs",
                        message=_guard_refusal(exc),
                        fatal=True,
                    )
                )
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_TRY_AGAIN_LATER)
        return

    queue = _ByteBoundedLiveInputQueue(maxsize=REALTIME_INPUT_QUEUE_MAXSIZE)

    # First server event of the session: the thread it runs on (server-made when the client
    # sent none). A reconnect sends this id back to continue the same thread.
    try:
        await _ws_send(websocket, json.dumps({"type": "session_started", "thread_id": thread_id}))
    except Exception as e:
        # The client went away (or stopped reading) before the session began.
        logger.info("Realtime client gone before session start: %s", type(e).__name__)
        queue.close()
        guard.release()
        with contextlib.suppress(Exception):
            await websocket.close()
        return

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
                    await _ws_send_bytes(websocket, event.data)
                else:
                    await _ws_send(websocket, _realtime_event_json(event))
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

    async def credential_watchdog() -> None:
        """End the session once its token expires or is revoked."""
        while True:
            await asyncio.sleep(REALTIME_RECHECK_INTERVAL)
            if await _live_credential_error(websocket, user):
                await close_session_expired()
                return

    tasks: list[asyncio.Task[None]] = []
    try:
        up_task = asyncio.create_task(upstream())
        tasks.append(up_task)
        down_task = asyncio.create_task(downstream())
        tasks.append(down_task)
        watch_task = asyncio.create_task(credential_watchdog())
        tasks.append(watch_task)
        done, pending = await asyncio.wait(
            {up_task, down_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
        )

        # Credential gone: no grace period, stop at once.
        if watch_task in done:
            queue.close()
            await _cancel_tasks(up_task, down_task)
            return

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
        await _cancel_tasks(up_task, down_task, watch_task)

        # asyncio.wait never re-raises; surface any failure from the finished task(s)
        # (e.g. arealtime rejecting a non-live graph, or a provider/checkpointer error).
        for task in (up_task, down_task):
            if task.done() and not task.cancelled():
                task.result()
    except WebSocketDisconnect:
        # The client going away mid-session is a normal termination, not a server fault;
        # don't log it as an error or attempt a 1011 close on an already-closed socket.
        logger.info("Realtime WebSocket client disconnected")
    except WsSendTimeout:
        logger.warning("Realtime client is not reading; closing the socket")
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_TRY_AGAIN_LATER)
    except Exception as e:
        logger.error("Realtime WebSocket error: %s", e)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        queue.close()
        # Also when this handler itself is cancelled (shutdown): no child task outlives it.
        await _cancel_tasks(*tasks)
        guard.release()
        with contextlib.suppress(Exception):
            await websocket.close()
