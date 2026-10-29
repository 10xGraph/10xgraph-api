"""Server shutdown signal shared by the worker, the lifespan and open response streams.

Uvicorn stops accepting connections on SIGTERM, waits for open responses up to
``timeout_graceful_shutdown`` and then cancels them, which a client sees as a cut connection.
The worker (``tenxgraph_api.src.app.uvicorn_worker``) calls :func:`interrupt_streams` shortly
before that deadline; :func:`end_on_shutdown` then stops each open stream and sends a final
chunk saying why, so the client can resume the thread instead of guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

from tenxgraph.core.state import StreamChunk, StreamEvent


INTERRUPTED_REASON = (
    "The server is shutting down and this run was interrupted. "
    "The thread is saved: send a new request on the same thread_id to resume."
)

# Sent on an idle stream so a proxy's idle timeout does not cut a run that is still working.
# A blank line: NDJSON readers skip it (the TypeScript client does), and it is still bytes on
# the wire.
KEEPALIVE_LINE = "\n"
KEEPALIVE_SECONDS = 15.0

_interrupted = False
_waiters: set[asyncio.Future[None]] = set()


def interrupt_streams() -> None:
    """Tell every open stream wrapped by :func:`end_on_shutdown` to finish now."""
    global _interrupted  # noqa: PLW0603
    _interrupted = True
    for waiter in list(_waiters):
        if not waiter.done():
            waiter.set_result(None)


def reset() -> None:
    """Clear the signal (tests)."""
    global _interrupted  # noqa: PLW0603
    _interrupted = False
    _waiters.clear()


def interrupted_line() -> str:
    """The final NDJSON line of a stream cut by shutdown."""
    chunk = StreamChunk(
        event=StreamEvent.ERROR,
        data={"reason": INTERRUPTED_REASON, "interrupted": True},
    )
    return chunk.model_dump_json(serialize_as_any=True) + "\n"


async def _next(stream: AsyncIterator[str]) -> str:
    return await anext(stream)


async def end_on_shutdown(
    stream: AsyncIterator[str],
    final: Callable[[], str] = interrupted_line,
    keepalive_seconds: float | None = None,
) -> AsyncIterator[str]:
    """Yield ``stream``; when shutdown is signalled, stop it and yield ``final()`` last.

    With ``keepalive_seconds``, a blank line is yielded each time the stream has been silent
    that long, so idle proxies keep the connection open.
    """
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[None] = loop.create_future()
    if _interrupted:
        waiter.set_result(None)
    _waiters.add(waiter)
    pending: asyncio.Future[str] | None = None
    try:
        while True:
            # Eager: a chunk that is ready without waiting costs no event-loop turn.
            pending = asyncio.Task(_next(stream), loop=loop, eager_start=True)
            while not pending.done() and not waiter.done():
                await asyncio.wait(
                    {pending, waiter},
                    timeout=keepalive_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not pending.done() and not waiter.done():
                    yield KEEPALIVE_LINE
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
                pending = None
                yield final()
                return
            try:
                item = pending.result()
            except StopAsyncIteration:
                pending = None
                return
            pending = None
            yield item
    finally:
        _waiters.discard(waiter)
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                await aclose()
