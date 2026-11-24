"""The ``/v1/graph/ws`` endpoint: one run at a time over a WebSocket, streamed as chunks."""

import asyncio
import contextlib
import json
from typing import Any

from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.logger import logger
from injectq.integrations.fastapi import InjectFastAPI
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ModelNotFoundError, ThreadNotFoundError
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core.auth.permissions import (
    RequirePermission,
    authorize_resource,
    run_credential,
    ws_bearer_subprotocol,
)
from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (
    ClientInputError,
    resource_id_error,
)
from tenxgraph_api.src.app.routers.graph import idempotency
from tenxgraph_api.src.app.routers.graph.realtime_guard import (
    realtime_connection_guard,
    ws_identity_still_valid,
    ws_run_allowed,
)
from tenxgraph_api.src.app.routers.graph.router import router
from tenxgraph_api.src.app.routers.graph.run_guard import acquire_run_guard
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import (
    SERVICE_UNAVAILABLE_CODE,
    GraphService,
    RunChecks,
)
from tenxgraph_api.src.app.routers.graph.ws_frames import (
    _WS_SKIP,
    WS_MAX_INVALID_FRAMES,
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
    _resolve_ws_thread,
    _seconds_until_expiry,
    _send_thread_not_found,
    _send_ws_error,
    _SessionExpired,
    _ws_max_frame_chars,
    _ws_run_gate,
    _ws_send,
    _WsFrames,
)


async def _next_frame(frames: _WsFrames, websocket: WebSocket, identity: Identity | None) -> str:
    """The next text frame; an idle socket is cut once its credential stops verifying.

    Without this, a socket that sends nothing keeps its slot long after the token that opened
    it expired (the per-run check only runs when a frame arrives).
    """
    while True:
        wait = _seconds_until_expiry(identity)
        if wait is None:
            return await frames.next()
        try:
            return await asyncio.wait_for(frames.next(), wait)
        except TimeoutError:
            if not await ws_identity_still_valid(websocket, identity):
                raise _SessionExpired from None


async def _next_ws_input(
    frames: _WsFrames,
    websocket: WebSocket,
    max_frame: int,
    identity: Identity | None = None,
) -> Any:
    """Read the next run request from ``/v1/graph/ws``.

    Returns the parsed ``WsGraphInputSchema``, ``_WS_SKIP`` for a frame that was rejected with
    an error message (the socket stays open), or ``None`` when the socket is done: the client
    left, sent a frame over the size cap (closed with 1009), or its token expired while idle.
    """
    try:
        text = await _next_frame(frames, websocket, identity)
        data = _parse_json_frame(text, max_frame)
    except _SessionExpired:
        logger.info("WebSocket credential expired while idle; closing")
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


async def _ws_media_urls_allowed(
    websocket: WebSocket, service: GraphService, graph_input: GraphInputSchema
) -> RunChecks | None:
    """Apply the media URL policy and the model choice to a WebSocket run.

    A blocked URL or an unregistered model name is sent as an error chunk before the run, so
    the message is not replaced by the production error-chunk sanitizer. None when refused.
    """
    try:
        return service.check_run_input(graph_input.messages, graph_input.config)
    except HTTPException as exc:
        reason = str(exc.detail)
    except ModelNotFoundError as exc:
        reason = str(exc)
    await _send_ws_error(websocket, reason)
    return None


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
    identity: Identity | None,
    frames: _WsFrames,
    checks: RunChecks | None = None,
) -> bool:
    """Stream one run to the socket; ``False`` when the client left and the run was stopped.

    The run is its own task, so a client that leaves while the run is quiet (a long tool call)
    is noticed and the run cancelled, instead of holding the lock and slot until its next
    write. The run generator is closed before this returns, so the caller can release the
    thread lock knowing the run is over.
    """
    stream = service.stream_graph(
        graph_input, user=identity, credential=run_credential(websocket), checks=checks
    )

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
    _guard: None = Depends(realtime_connection_guard),
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "stream")),
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
    Bearer token via the ``Authorization`` header or the ``10xgraph-bearer``
    Sec-WebSocket-Protocol (browser-safe). A ``?token=`` in the URL is not accepted.
    Handshakes are subject to the global rate
    limit and the ``websocket.max_connections`` cap.

    Every run on the socket counts against the same rate limit (an error chunk with
    ``retry_after_seconds`` is sent when over it), and the token is re-verified before each
    run. A run naming a thread the caller cannot run on (another user's, or an unknown id
    under ``runs.thread_ids: "server"``) gets an error chunk with ``code: "THREAD_NOT_FOUND"``
    and the socket stays open.

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

    # Wrong agent type for this endpoint: a live (realtime) graph cannot be driven over
    # the turn-based stream socket. Reject up front with a clear error instead of failing
    # mid-run when the graph refuses invoke/stream.
    if service.is_live_agent:
        logger.warning("Rejected /v1/graph/ws connection: graph is a live agent")
        with contextlib.suppress(Exception):
            await _send_ws_error(
                websocket,
                "This graph is a live (realtime) agent; connect to "
                "/v1/graph/live instead of /v1/graph/ws.",
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=1008)
        return

    max_frame = _ws_max_frame_chars()
    frames = _WsFrames(websocket)
    invalid_frames = 0
    try:
        while True:
            ws_input = await _next_ws_input(frames, websocket, max_frame, identity)
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
            gate = await _ws_run_gate(websocket, identity)
            if gate == "close":
                break
            if gate == "skip":
                continue

            thread_id = _ws_run_thread_id(ws_input)
            if not is_production():
                logger.info(
                    "WebSocket graph run: invoke_type=%s, thread_id=%s",
                    ws_input.invoke_type,
                    log_safe(thread_id),
                )

            # A thread the caller may not use or cannot run on: an error chunk, the socket
            # stays open. The handshake authorized with no thread; this run names one.
            try:
                if thread_id is not None:
                    await authorize_resource(identity, "graph:stream", thread_id)
                await service.require_run_thread(thread_id, identity)
            except HTTPException as exc:
                await _send_ws_error(websocket, str(exc.detail))
                continue
            except ThreadNotFoundError:
                await _send_thread_not_found(websocket)
                continue

            graph_input = ws_input.to_graph_input()
            checks = await _ws_media_urls_allowed(websocket, service, graph_input)
            if checks is None:
                continue
            idem = None
            if ws_input.idempotency_key is not None:
                try:
                    idem, _ = await idempotency.check(
                        identity,
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
                    user=identity,
                    runs=service.runs_policy(),
                    thread_id=thread_id,
                    on_busy=ws_input.on_busy,
                    stop=lambda tid=thread_id: service.stop_graph(tid, identity),
                    recheck=lambda tid=thread_id: service.require_run_thread(tid, identity),
                )
            except HTTPException as exc:
                if idem:
                    idem.release()
                await _send_ws_error(websocket, _guard_refusal(exc))
                continue
            except ThreadNotFoundError:
                if idem:
                    idem.release()
                await _send_thread_not_found(websocket)
                continue
            try:
                finished = await _ws_run(websocket, service, graph_input, identity, frames, checks)
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
