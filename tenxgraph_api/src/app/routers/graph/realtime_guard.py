"""Connection guard for the WebSocket endpoints.

The rate-limit and request-size middleware only act on HTTP scopes, so WebSocket handshakes
bypass them entirely. This module re-applies
two protections at the handshake, as a FastAPI dependency:

1. The same global rate limit as REST (same cache store and bucket), so opening a socket
   counts like any other request.
2. A cap on concurrent WebSocket connections (``websocket.max_connections`` in
   10xgraph.json), and a per-user cap (``websocket.max_connections_per_user``) so one
   account cannot hold every slot. Both are leases (``core.leases``): shared across workers
   when the cache store is shared (Redis), per process otherwise.

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
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.leases import Lease, LeaseManager, lease_manager
from tenxgraph_api.src.app.core.middleware.rate_limit import keying
from tenxgraph_api.src.app.core.middleware.rate_limit.keying import client_key_for
from tenxgraph_api.src.app.core.middleware.rate_limit.limiter import bound_cache_store, check
from tenxgraph_api.src.app.core.redis_client import key_part
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


# RFC 6455 close code 1013 "Try Again Later" -- the right signal for shed-load rejections.
WS_TRY_AGAIN_LATER = 1013


async def realtime_connection_guard(
    websocket: WebSocket,
    config: GraphConfig = InjectAPI(GraphConfig),
) -> AsyncIterator[None]:
    """Gate a WebSocket handshake on the global rate limit and the concurrent-connection cap."""
    # 1) Shared global rate limit (same cache store and bucket as the REST middleware).
    rl_config = config.rate_limit
    if rl_config is not None:
        cache = bound_cache_store()
        # Kept on the connection so every run on this socket counts too (ws_run_allowed).
        websocket.state.ws_rate_limit = (rl_config, cache)
        key = client_key_for(websocket, rl_config)
        decision = await check(
            cache, key, rl_config.requests, rl_config.window, fail_open=rl_config.fail_open
        )
        if not decision.allowed:
            logger.warning("WebSocket rate limit exceeded for %s", log_safe(key))
            raise WebSocketException(code=WS_TRY_AGAIN_LATER, reason="Rate limit exceeded")

    # 2) Concurrent-connection cap, then the per-user share of it. Leases on the cache store:
    # shared across workers when it is shared (Redis), otherwise counted in this process.
    async with _connection_slots(lease_manager(), websocket, config.websocket):
        yield


@asynccontextmanager
async def _connection_slots(
    manager: LeaseManager, websocket: WebSocket, ws_config: Any
) -> AsyncIterator[None]:
    """Hold the global and per-user connection slots for the socket's lifetime."""
    leases: list[Lease] = []
    try:
        if ws_config.max_connections is not None:
            lease = await manager.acquire("ws:all", ws_config.max_connections)
            if lease is None:
                logger.warning(
                    "WebSocket connection limit reached (max=%s)", ws_config.max_connections
                )
                raise WebSocketException(code=WS_TRY_AGAIN_LATER, reason="Too many connections")
            leases.append(lease)
        user_key = keying.verified_user_id(websocket)
        if user_key and ws_config.max_connections_per_user is not None:
            lease = await manager.acquire(
                f"ws:user:{key_part(user_key)}", ws_config.max_connections_per_user
            )
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
    rl_config, cache = limiter
    key = client_key_for(websocket, rl_config)
    decision = await check(
        cache, key, rl_config.requests, rl_config.window, fail_open=rl_config.fail_open
    )
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
