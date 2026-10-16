"""Per-user cap on graph runs in flight over HTTP (``runs.max_concurrent_per_user``).

Every invoke, stream and AG-UI run costs model calls for as long as it lasts, so one account
holding many of them open is the cheapest way to exhaust a server. WebSocket runs are already
bounded by the per-user connection cap; this module bounds the HTTP ones.

A slot is taken before the response starts, so an over-cap request still gets a real 429.
For a streaming response the slot is held until the stream ends, the client disconnects, or
the stream object is discarded without ever being iterated.

Slots come from :func:`~tenxgraph_api.src.app.core.concurrency.concurrency_limiter`: shared
through Redis when it is configured, otherwise counted per process (size the cap per worker).
"""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.concurrency import Lease, concurrency_limiter
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


HTTP_TOO_MANY_REQUESTS = 429

# A slot that was never counted (no identity, or no cap).
RunSlot = Lease


def run_slot_key(user_id: str) -> str:
    return f"runs:user:{user_id}"


async def acquire_run_slot(user: dict[str, Any], runs: RunsConfig) -> Lease:
    """Reserve a run slot for ``user`` or raise 429.

    Requests without a verified ``user_id`` (auth off) are not counted: there is no identity
    to share a budget across, and capping them would cap every caller together.
    """
    user_id = user.get("user_id") if isinstance(user, dict) else None
    limit = runs.max_concurrent_per_user
    if not user_id or limit is None:
        return Lease(None)
    lease = await concurrency_limiter().acquire(run_slot_key(str(user_id)), limit)
    if lease is None:
        logger.warning(
            "Concurrent run limit reached for user %s (max=%s)", log_safe(user_id), limit
        )
        raise HTTPException(
            status_code=HTTP_TOO_MANY_REQUESTS,
            detail=(
                f"Too many runs in progress for this user (limit {limit}). "
                "Wait for one to finish and retry."
            ),
        )
    return lease


@asynccontextmanager
async def run_slot(user: dict[str, Any], runs: RunsConfig) -> AsyncIterator[None]:
    """Hold a run slot for the duration of the block (used by invoke)."""
    slot = await acquire_run_slot(user, runs)
    try:
        yield
    finally:
        slot.release()


def release_when_done(stream: AsyncIterator[Any], slot: Lease) -> AsyncIterator[Any]:
    """Wrap a response stream so ``slot`` is released when the stream is finished with.

    Covers a normal end, an error, a client disconnect (the stream is cancelled or closed) and
    a stream that is dropped before it was ever iterated (garbage collected).
    """

    async def _stream() -> AsyncIterator[Any]:
        try:
            async for item in stream:
                yield item
        finally:
            # Close the run before giving the slot back, so a disconnected client's run is
            # not left working without a slot until garbage collection reaches it.
            aclose = getattr(stream, "aclose", None)
            try:
                if callable(aclose):
                    await aclose()
            except Exception as exc:
                logger.debug("Closing a released run stream failed: %s", exc)
            finally:
                slot.release()

    wrapped = _stream()
    weakref.finalize(wrapped, slot.release)
    return wrapped
