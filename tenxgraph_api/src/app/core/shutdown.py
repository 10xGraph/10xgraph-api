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
import contextvars
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
from tenxgraph.core.state import StreamChunk, StreamEvent


try:
    from tenxgraph.core.graph.utils.utils import FINALIZE_TIMEOUT_SECONDS
except ImportError:  # an older core; same value
    FINALIZE_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("tenxgraph_api.shutdown")


INTERRUPTED_REASON = (
    "The server is shutting down and this run was interrupted. "
    "The thread is saved: send a new request on the same thread_id to resume."
)

# Sent on an idle stream so a proxy's idle timeout does not cut a run that is still working.
# A blank line: NDJSON readers skip it (the TypeScript client does), and it is still bytes on
# the wire.
KEEPALIVE_LINE = "\n"
KEEPALIVE_SECONDS = 15.0

# How long a cancelled run task is waited for: core saves a cancelled run within
# ``FINALIZE_TIMEOUT_SECONDS``, then releases its run marker.
RUN_CLEANUP_WAIT_SECONDS = FINALIZE_TIMEOUT_SECONDS + 5.0

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


async def cancel_and_wait(task: asyncio.Future, timeout: float = RUN_CLEANUP_WAIT_SECONDS) -> None:
    """Cancel ``task`` once and wait until it is done, up to ``timeout`` seconds.

    For a task that runs a graph: core saves a cancelled run before the task ends, and a lock
    the caller releases afterwards (the thread lock) must not be free before that save is.
    ``await task`` would forward every cancel the caller gets to ``task``; under a cancelled
    anyio scope (Starlette on a client disconnect) that is one cancel per event-loop turn. The
    wait runs in a shielded anyio scope instead, and a plain cancel that still reaches the
    caller is raised once ``task`` is done or the time is up. The task's own result or error
    is not raised.
    """
    if task.done():
        return
    task.cancel()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cancel: asyncio.CancelledError | None = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("A cancelled run did not finish within %ss", timeout)
                break
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as e:
                cancel = e
    if task.done() and not task.cancelled():
        task.exception()  # retrieved: it is the run's own error, reported by the run
    if cancel is not None:
        raise cancel


async def end_on_shutdown(
    stream: AsyncIterator[str],
    final: Callable[[], str] = interrupted_line,
    keepalive_seconds: float | None = None,
) -> AsyncIterator[str]:
    """Yield ``stream``; when shutdown is signalled, stop it and yield ``final()`` last.

    With ``keepalive_seconds``, a blank line is yielded each time the stream has been silent
    that long, so idle proxies keep the connection open.

    Each step of ``stream`` runs in a task of its own, all in one shared context: a context
    variable the stream sets (the graph's current run, which carries its stop flag and
    shutdown reason) stays set for every later step. Only one step runs at a time.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    waiter: asyncio.Future[None] = loop.create_future()
    if _interrupted:
        waiter.set_result(None)
    _waiters.add(waiter)
    pending: asyncio.Future[str] | None = None
    try:
        while True:
            # Eager: a chunk that is ready without waiting costs no event-loop turn.
            pending = asyncio.Task(_next(stream), loop=loop, eager_start=True, context=context)
            while not pending.done() and not waiter.done():
                watched: set[asyncio.Future[Any]] = {pending, waiter}
                await asyncio.wait(
                    watched,
                    timeout=keepalive_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not pending.done() and not waiter.done():
                    yield KEEPALIVE_LINE
            if not pending.done():
                await cancel_and_wait(pending)
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
        try:
            if pending is not None and not pending.done():
                await cancel_and_wait(pending)
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
