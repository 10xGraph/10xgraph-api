"""The ``/v1/graph/live`` endpoint: realtime (audio-to-audio) bridge over a WebSocket."""

import asyncio
import contextlib
import json
from typing import Any

from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.logger import logger
from injectq.integrations.fastapi import InjectFastAPI
from pydantic import ValidationError
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ThreadNotFoundError
from tenxgraph.core.realtime.base import ErrorEvent
from tenxgraph.core.realtime.queue import LiveInputQueue

from tenxgraph_api.src.app.core.auth.permissions import (
    RequirePermission,
    authorize_resource,
    run_credential,
    ws_bearer_subprotocol,
)
from tenxgraph_api.src.app.core.validation.request_config import resource_id_error
from tenxgraph_api.src.app.routers.graph.realtime_guard import realtime_connection_guard
from tenxgraph_api.src.app.routers.graph.router import router
from tenxgraph_api.src.app.routers.graph.run_guard import acquire_run_guard
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.graph.ws_frames import (
    REALTIME_DRAIN_TIMEOUT,
    REALTIME_INPUT_QUEUE_MAX_BYTES,
    REALTIME_INPUT_QUEUE_MAXSIZE,
    REALTIME_MAX_FRAME_BYTES,
    REALTIME_RECHECK_INTERVAL,
    WS_MESSAGE_TOO_BIG,
    WS_TRY_AGAIN_LATER,
    WsFrameTooLarge,
    WsSendTimeout,
    _cancel_tasks,
    _client_error,
    _guard_refusal,
    _receive_json_frame,
    _resolve_ws_thread,
    _thread_not_found,
    _ws_credential_error,
    _ws_send,
    _ws_send_bytes,
)


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


async def _close_live(websocket: WebSocket, code: str, message: str) -> None:
    """End a live session before it starts: a fatal error event, then close 1008."""
    with contextlib.suppress(Exception):
        await websocket.send_text(
            _realtime_event_json(ErrorEvent(code=code, message=message, fatal=True))
        )
    with contextlib.suppress(Exception):
        await websocket.close(code=1008)


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
    _guard: None = Depends(realtime_connection_guard),
    service: GraphService = InjectFastAPI(GraphService),
    identity: Identity | None = Depends(RequirePermission("graph", "stream")),
):
    """Realtime (audio-to-audio) WebSocket bridge over ``CompiledGraph.arealtime``.

    Protocol (provider-neutral; the client never sees Gemini vs OpenAI)
    ------------------------------------------------------------------
    First frame  : JSON control  ``{model, thread_id?, voice?, modalities?, vad?, ...}``
                   (``thread_id`` absent, blank or ``"new"`` makes the server create the thread)
    First reply  : JSON ``{type:"session_started", thread_id}``; reconnect with that id to
                   continue the thread. A thread the caller cannot run on gets a fatal
                   ``THREAD_NOT_FOUND`` error event and close 1008 instead
    Upstream     : binary frame   = PCM16 input audio
                   JSON control   = ``{type:"activity_start"|"activity_end"|"text"|"close", ...}``
    Downstream   : binary frame   = PCM16 model audio (``audio_delta``)
                   JSON text frame = every other event (transcripts, turn_complete,
                                     interrupted, tool_call, session/go_away, error)

    Auth: ``RequirePermission("graph","stream")``: bearer via the ``Authorization`` header or
    the ``10xgraph-bearer`` Sec-WebSocket-Protocol (browser-safe); ``?token=`` is not accepted.
    Handshakes are subject to the global rate limit and the
    ``websocket.max_connections`` cap (rejected with close code 1013). A non-live
    (turn-based) graph is rejected up front with close code 1008 — use ``/v1/graph/ws``.
    """
    await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))
    logger.info("Realtime WebSocket connection accepted")

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

    if not isinstance(init, dict):
        logger.warning("Realtime init frame must be a JSON object, got %s", type(init).__name__)
        with contextlib.suppress(Exception):
            await websocket.close(code=1003)
        return

    named_thread = thread_id is not None
    if thread_id is None:
        # No id (absent, blank or "new"): the server makes the thread, as AG-UI does, so there
        # is nothing to check. The id goes back to the client in ``session_started``.
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
    if await _ws_credential_error(websocket, identity):
        await close_session_expired()
        return

    # A thread the caller cannot run on ends the session before anything is held. A storage
    # error closes it too (1011): the check fails closed.
    if named_thread:
        try:
            # The handshake authorized with no thread; the init frame names one.
            await authorize_resource(identity, "graph:stream", thread_id)
            await service.require_run_thread(thread_id, identity)
        except HTTPException as exc:
            await _close_live(websocket, "forbidden", str(exc.detail))
            return
        except ThreadNotFoundError:
            await _close_live(websocket, *_thread_not_found())
            return
        except Exception as e:
            logger.error("Realtime thread check failed: %s", e)
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
            return

    # The same guard as every other run: one session at a time on a thread, and the session
    # counts against the user's run cap for as long as it lasts. A named thread is read again
    # once locked.
    try:
        guard = await acquire_run_guard(
            user=identity,
            runs=service.runs_policy(),
            thread_id=thread_id,
            on_busy="wait",
            recheck=(lambda: service.require_run_thread(thread_id, identity))
            if named_thread
            else None,
        )
    except ThreadNotFoundError:
        await _close_live(websocket, *_thread_not_found())
        return
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
            credential = run_credential(websocket)
            async for event in service.realtime_graph(queue, init, identity, credential=credential):
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
            if await _ws_credential_error(websocket, identity):
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
