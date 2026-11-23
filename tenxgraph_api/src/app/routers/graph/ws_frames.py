"""Shared pieces of the two WebSocket endpoints (``/v1/graph/ws`` and ``/v1/graph/live``).

Constants, frame reading and sending, and error text. ``router.py`` re-exports every name here,
so ``from ...graph.router import X`` keeps working.
"""

import asyncio
import contextlib
import json
import time
from collections import deque
from typing import Any

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.logger import logger
from pydantic import ValidationError
from tenxgraph.core.authz import Identity
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core.audit import audit_event
from tenxgraph_api.src.app.core.auth.revocation import (
    TokenJtiMissingError,
    TokenRevocationUnavailableError,
    token_revoked,
)
from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    _sanitize_error_message,
    client_error,
)
from tenxgraph_api.src.app.core.utils.log_sanitizer import summarize_validation_error
from tenxgraph_api.src.app.core.validation.request_config import (
    ClientInputError,
    normalize_thread_id,
)
from tenxgraph_api.src.app.routers.graph.realtime_guard import (
    ws_identity_still_valid,
    ws_run_allowed,
)
from tenxgraph_api.src.app.routers.graph.thread_lock import THREAD_BUSY_DETAIL


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


# Returned by _next_ws_input for a bad frame the socket survives (an error was sent).
_WS_SKIP: Any = object()


async def _send_ws_error(websocket: WebSocket, reason: str, **data: Any) -> None:
    """Send an ERROR chunk: ``reason`` plus any extra ``data`` fields (``code``, ...)."""
    await websocket.send_text(
        StreamChunk(event=StreamEvent.ERROR, data={"reason": reason, **data}).model_dump_json()
    )


async def _send_thread_not_found(websocket: WebSocket) -> None:
    code, message = _thread_not_found()
    await _send_ws_error(websocket, message, code=code)


def _thread_not_found() -> tuple[str, str]:
    """``(code, message)`` of the 404 for a thread the caller cannot see, for socket frames."""
    from tenxgraph_api.src.app.core.exceptions.handle_errors import (
        THREAD_NOT_FOUND_CODE,
        THREAD_NOT_FOUND_MESSAGE,
    )

    return THREAD_NOT_FOUND_CODE, THREAD_NOT_FOUND_MESSAGE


def _seconds_until_expiry(identity: Identity | None) -> float | None:
    """Seconds until the identity's ``exp`` claim, or ``None`` when it has none."""
    exp = identity.claims.get("exp") if identity is not None else None
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    return max(float(exp) - time.time(), 0.0) + 0.5


class _SessionExpired(Exception):
    """The socket sat idle past its token's expiry."""


async def _close_expired(websocket: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await _send_ws_error(websocket, "Session expired. Reconnect with a valid token.")
    with contextlib.suppress(Exception):
        await websocket.close(code=1008)


async def _close_too_big(websocket: WebSocket, exc: WsFrameTooLarge) -> None:
    logger.warning("WebSocket frame refused: %s", exc)
    with contextlib.suppress(Exception):
        await _send_ws_error(websocket, str(exc))
    with contextlib.suppress(Exception):
        await websocket.close(code=WS_MESSAGE_TOO_BIG)


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


def _guard_refusal(exc: HTTPException) -> str:
    """The reason a run was refused (thread busy, run cap) as a client-facing sentence."""
    return THREAD_BUSY_DETAIL if exc.status_code == 409 else str(exc.detail)  # noqa: PLR2004


def _client_error(exc: Exception) -> str:
    """The error text a client may see, by the same rule as the HTTP answer for ``exc``.

    A ValueError here can come from anywhere under the session (storage, provider SDK), not
    only from the init frame, and its text may name internal hosts, paths or queries. A
    pydantic error is summarized without the values it quotes; in production it is the generic
    validation text unless it wraps a ``ClientInputError`` (as the HTTP 422 handler does).
    """
    if not isinstance(exc, ValidationError):
        return client_error(exc).message
    if not is_production():
        return summarize_validation_error(exc)
    for error in exc.errors():
        wrapped = (error.get("ctx") or {}).get("error")
        if isinstance(wrapped, ClientInputError):
            return str(wrapped)
    return _sanitize_error_message(str(exc), "VALIDATION_ERROR", True)


def _error_summary(exc: Exception) -> str:
    """``exc`` as text; a pydantic validation error without the values it quotes."""
    if isinstance(exc, ValidationError):
        return summarize_validation_error(exc)
    return str(exc)


async def _ws_token_revoked(identity: Identity | None) -> bool:
    """Whether the socket's token was revoked since the handshake (an outage counts as yes).

    Never revoked when auth is off (``identity`` is ``None``). A refusal is an ``auth`` audit
    event.
    """
    if identity is None:
        return False
    try:
        revoked = await token_revoked(identity)
        reason = "revoked"
    except TokenRevocationUnavailableError:
        revoked, reason = True, "revocation_unavailable"
    except TokenJtiMissingError:
        revoked, reason = True, "missing_jti"
    if revoked:
        await audit_event("auth", "failed", reason=reason, user_id=identity.user_id)
    return revoked


async def _ws_credential_error(websocket: WebSocket, identity: Identity | None) -> bool:
    """Whether the socket's credential no longer holds (expired, revoked or backend down).

    Checked before each ``/v1/graph/ws`` run and on a timer during a ``/v1/graph/live``
    session: the token is re-verified (catches expiry) and the revocation backend is asked.
    """
    if not await ws_identity_still_valid(websocket, identity):
        user_id = identity.user_id if identity is not None else None
        await audit_event("auth", "failed", reason="invalid_token", user_id=user_id)
        return True
    return await _ws_token_revoked(identity)


async def _ws_run_gate(websocket: WebSocket, identity: Identity | None) -> str:
    """Per-run checks for a WebSocket: ``"run"``, ``"skip"`` (rate limited) or ``"close"``.

    The handshake guard runs once, so a socket that stays open must be re-checked before each
    run: the token may have expired or been revoked, and every run counts against the limit.
    """
    if await _ws_credential_error(websocket, identity):
        logger.info("WebSocket credential no longer valid; closing")
        await _close_expired(websocket)
        return "close"

    retry_after = await ws_run_allowed(websocket)
    if retry_after is not None:
        await _send_ws_error(
            websocket,
            f"Rate limit exceeded. Retry after {retry_after}s.",
            retry_after_seconds=retry_after,
        )
        return "skip"
    return "run"
