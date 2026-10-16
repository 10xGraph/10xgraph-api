"""Connection guard for the WebSocket endpoints.

The rate-limit and request-size middleware only act on HTTP scopes, so WebSocket handshakes
bypass them entirely. This module re-applies
two protections at the handshake, as a FastAPI dependency:

1. The same global rate limit as REST (shared backend + bucket), so opening a socket counts
   like any other request.
2. A per-process cap on concurrent WebSocket connections (``websocket.max_connections`` in
   10xgraph.json), and a per-user cap (``websocket.max_connections_per_user``) so one
   account cannot hold every slot.

A socket lives much longer than its handshake, so two helpers apply for its whole lifetime:
:func:`ws_run_allowed` counts every graph run against the same rate-limit bucket, and
:func:`ws_identity_still_valid` re-verifies the credential so an expired or revoked token
cannot keep a socket working.

Rejections raise ``WebSocketException`` before ``accept()``, so the handshake fails with a
close code instead of leaving a half-open socket. The concurrency slot is released on
teardown of the (yield) dependency, i.e. when the handler returns or the client disconnects.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import WebSocket, WebSocketException
from injectq.integrations import InjectAPI

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.concurrency import (
    ConcurrencyLimiter,
    Lease,
    shared_concurrency_limiter,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import keying
from tenxgraph_api.src.app.core.middleware.rate_limit.keying import client_key_for
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


# RFC 6455 close code 1013 "Try Again Later" -- the right signal for shed-load rejections.
WS_TRY_AGAIN_LATER = 1013


class _ConnectionRegistry:
    """Per-process counter of active WebSocket connections.

    The event loop is single-threaded, so the check-and-increment in :meth:`try_acquire` is
    atomic (no ``await`` between the test and the mutation) and needs no lock. This counter is
    per process, matching the in-memory rate-limit backend's scope; for a multi-worker
    deployment, set ``max_connections`` per worker accordingly.
    """

    def __init__(self) -> None:
        self._active = 0
        self._per_user: dict[str, int] = {}

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self, max_connections: int | None) -> bool:
        if max_connections is not None and self._active >= max_connections:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1

    def try_acquire_user(self, user_key: str, max_per_user: int | None) -> bool:
        held = self._per_user.get(user_key, 0)
        if max_per_user is not None and held >= max_per_user:
            return False
        self._per_user[user_key] = held + 1
        return True

    def release_user(self, user_key: str) -> None:
        held = self._per_user.get(user_key, 0)
        if held <= 1:
            self._per_user.pop(user_key, None)
        else:
            self._per_user[user_key] = held - 1


# Module-level singleton (per process).
_registry = _ConnectionRegistry()


async def realtime_connection_guard(
    websocket: WebSocket,
    config: GraphConfig = InjectAPI(GraphConfig),
) -> AsyncIterator[None]:
    """Gate a WebSocket handshake on the global rate limit and the concurrent-connection cap."""
    # 1) Shared global rate limit (same backend/bucket as the REST middleware).
    rl_config = config.rate_limit
    backend = getattr(getattr(websocket, "app", None), "state", None)
    backend = getattr(backend, "rate_limit_backend", None)
    if rl_config is not None and backend is not None:
        # Kept on the connection so every run on this socket counts too (ws_run_allowed).
        websocket.state.ws_rate_limit = (rl_config, backend)
        key = client_key_for(websocket, rl_config)
        decision = await backend.check(key, limit=rl_config.requests, window=rl_config.window)
        if not decision.allowed:
            logger.warning("WebSocket rate limit exceeded for %s", log_safe(key))
            raise WebSocketException(code=WS_TRY_AGAIN_LATER, reason="Rate limit exceeded")

    # 2) Concurrent-connection cap, then the per-user share of it. Shared across workers when a
    # ConcurrencyLimiter is bound (Redis configured); otherwise counted in this process.
    ws_config = config.websocket
    shared = shared_concurrency_limiter()
    if shared is not None:
        async with _shared_connection_slots(shared, websocket, ws_config):
            yield
        return

    max_conn = ws_config.max_connections
    if not _registry.try_acquire(max_conn):
        logger.warning(
            "WebSocket connection limit reached (active=%d, max=%s)",
            _registry.active,
            max_conn,
        )
        raise WebSocketException(code=WS_TRY_AGAIN_LATER, reason="Too many connections")

    user_key = keying.verified_user_id(websocket)
    if user_key and not _registry.try_acquire_user(user_key, ws_config.max_connections_per_user):
        _registry.release()
        logger.warning(
            "WebSocket per-user connection limit reached for %s (max=%s)",
            log_safe(user_key),
            ws_config.max_connections_per_user,
        )
        raise WebSocketException(
            code=WS_TRY_AGAIN_LATER, reason="Too many connections for this user"
        )

    try:
        yield
    finally:
        _registry.release()
        if user_key:
            _registry.release_user(user_key)


@asynccontextmanager
async def _shared_connection_slots(
    limiter: ConcurrencyLimiter, websocket: WebSocket, ws_config: Any
) -> AsyncIterator[None]:
    """Hold the global and per-user connection slots from a shared limiter for the socket."""
    leases: list[Lease] = []
    try:
        if ws_config.max_connections is not None:
            lease = await limiter.acquire("ws:all", ws_config.max_connections)
            if lease is None:
                logger.warning(
                    "WebSocket connection limit reached (max=%s)", ws_config.max_connections
                )
                raise WebSocketException(code=WS_TRY_AGAIN_LATER, reason="Too many connections")
            leases.append(lease)
        user_key = keying.verified_user_id(websocket)
        if user_key and ws_config.max_connections_per_user is not None:
            lease = await limiter.acquire(f"ws:user:{user_key}", ws_config.max_connections_per_user)
            if lease is None:
                logger.warning(
                    "WebSocket per-user connection limit reached for %s (max=%s)",
                    log_safe(user_key),
                    ws_config.max_connections_per_user,
                )
                raise WebSocketException(
                    code=WS_TRY_AGAIN_LATER, reason="Too many connections for this user"
                )
            leases.append(lease)
        yield
    finally:
        for lease in leases:
            lease.release()


async def ws_run_allowed(websocket: WebSocket) -> int | None:
    """Count one graph run against the rate limit.

    Returns ``None`` when the run may start, or the seconds to wait when the caller is over
    the limit. Runs share the handshake's bucket, which is also the REST bucket.
    """
    limiter = getattr(websocket.state, "ws_rate_limit", None)
    if not isinstance(limiter, tuple):  # the guard stored none: rate limiting is off
        return None
    rl_config, backend = limiter
    key = client_key_for(websocket, rl_config)
    decision = await backend.check(key, limit=rl_config.requests, window=rl_config.window)
    if decision.allowed:
        return None
    logger.warning("WebSocket run rate limit exceeded for %s", log_safe(key))
    return decision.reset_after


def ws_identity_still_valid(websocket: Any, user: dict[str, Any]) -> bool:
    """Whether the socket's credential still verifies as the user it was opened by.

    Checked before each run: without it, a socket keeps working after its token expires or
    is revoked. Always true when auth is not configured (``user`` has no ``user_id``).
    """
    user_id = user.get("user_id") if isinstance(user, dict) else None
    if not user_id:
        return True
    return keying.verified_user_id(websocket) == str(user_id)
