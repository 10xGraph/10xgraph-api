"""Per-thread run lock: one graph run at a time on a thread.

Two runs on one thread both load the state, both run and both save, so tools run twice and
the history interleaves (a double click on Send, two tabs). Every run path (invoke, stream,
WebSocket, AG-UI) takes this lock for the thread before the graph starts and holds it until
the run is over.

The lock is a one-slot lease from ``core.concurrency.concurrency_limiter``
(key ``runs:thread:<thread id>``): shared through Redis when it is configured, otherwise
per process. A Redis lease is renewed in the background while the run is alive and expires on
its own if the worker dies, so a crashed worker cannot hold a thread forever. A Redis error
falls back to a per-process lock (the limiter fails open to its own count), so one worker still
runs one thing at a time on a thread. Run entry points take this lock together with the run
slot through ``run_guard.acquire_run_guard``.

A run that finds the thread busy waits up to ``THREAD_BUSY_WAIT_SECONDS`` and is then
rejected with 409. With ``on_busy="cancel"`` it first asks the active run to stop (and
repeats the request while it waits, in case the run had not saved its state yet).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

from fastapi import HTTPException

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import normalize_thread_id
from tenxgraph_api.src.app.core.concurrency import Lease, concurrency_limiter
from tenxgraph_api.src.app.core.redis_client import key_part
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


HTTP_CONFLICT = 409
THREAD_BUSY_DETAIL = "thread is busy"
# nginx's code for a client that closed the request; the response is never read.
CLIENT_CLOSED_REQUEST = 499
# Seconds a client is told to wait before it tries a busy thread again.
BUSY_RETRY_AFTER_SECONDS = 5

# Longest a run waits for a busy thread before it is rejected.
THREAD_BUSY_WAIT_SECONDS = 30.0

# The wait polls, starting short (a finishing run is picked up quickly) and backing off.
THREAD_BUSY_POLL_MIN_SECONDS = 0.05
THREAD_BUSY_POLL_MAX_SECONDS = 0.5

# With on_busy="cancel", how often the stop request is repeated while waiting.
THREAD_BUSY_STOP_RETRY_SECONDS = 1.0

OnBusy = Literal["wait", "cancel"]


def thread_lock_key(thread_id: str) -> str:
    return f"runs:thread:{key_part(thread_id)}"


class ThreadBusyError(HTTPException):
    """The thread still has an active run after the wait limit."""

    def __init__(self) -> None:
        super().__init__(
            status_code=HTTP_CONFLICT,
            detail=THREAD_BUSY_DETAIL,
            headers={"Retry-After": str(BUSY_RETRY_AFTER_SECONDS)},
        )


class ClientGoneError(HTTPException):
    """The client closed the request while it waited for the thread."""

    def __init__(self) -> None:
        super().__init__(
            status_code=CLIENT_CLOSED_REQUEST, detail="client closed the request while waiting"
        )


async def acquire_thread_lock(
    thread_id: str,
    *,
    on_busy: OnBusy = "wait",
    stop: Callable[[], Awaitable[Any]] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    wait_seconds: float | None = None,
) -> Lease:
    """Take the run lock for ``thread_id``, waiting for the active run if there is one.

    ``stop`` asks the active run to stop; it is called only for ``on_busy="cancel"`` and only
    while the thread is busy. Raises :class:`ThreadBusyError` (409) after the wait limit.
    ``is_disconnected`` tells whether the client is gone: a waiter whose client left raises
    :class:`ClientGoneError` instead of taking the lock and running for nobody.
    ``wait_seconds`` defaults to ``THREAD_BUSY_WAIT_SECONDS``, read at call time.
    """
    if wait_seconds is None:
        wait_seconds = THREAD_BUSY_WAIT_SECONDS
    limiter = concurrency_limiter()
    key = thread_lock_key(thread_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    delay = THREAD_BUSY_POLL_MIN_SECONDS
    next_stop = 0.0
    waited = False
    while True:
        lease = await limiter.acquire(key, 1)
        if lease is not None:
            if waited and is_disconnected is not None and await is_disconnected():
                lease.release()
                raise ClientGoneError
            return lease
        waited = True
        if is_disconnected is not None and await is_disconnected():
            raise ClientGoneError
        now = loop.time()
        if now >= deadline:
            logger.warning("Thread %s is still busy after the wait limit", log_safe(thread_id))
            raise ThreadBusyError
        if on_busy == "cancel" and stop is not None and now >= next_stop:
            next_stop = now + THREAD_BUSY_STOP_RETRY_SECONDS
            try:
                await stop()
            except Exception as exc:  # the wait below still applies; a stop is best effort
                logger.warning(
                    "Stop request for busy thread %s failed: %s", log_safe(thread_id), exc
                )
        await asyncio.sleep(min(delay, max(deadline - loop.time(), 0)))
        delay = min(delay * 2, THREAD_BUSY_POLL_MAX_SECONDS)


@contextlib.asynccontextmanager
async def thread_write_guard(thread_id: Any) -> AsyncIterator[None]:
    """Hold the thread's run lock around a state-changing REST call.

    A state edit, message import, message or thread delete, or graph fix that races an active
    run is overwritten by that run or brings a deleted thread back. This takes the lock the run
    routes take, so the call is refused with 409 (``ThreadBusyError``) at once while a run holds
    it, instead of waiting behind a run that can last minutes.
    """
    key = normalize_thread_id(thread_id)
    if key is None:
        yield
        return
    lease = await acquire_thread_lock(key, wait_seconds=0)
    try:
        yield
    finally:
        lease.release()


def requested_thread_id(config: dict[str, Any] | None) -> str | None:
    """The thread a request names, or ``None`` when it starts a new one."""
    return normalize_thread_id((config or {}).get("thread_id"))


async def lock_requested_thread(
    config: dict[str, Any] | None,
    on_busy: OnBusy,
    stop: Callable[[str], Awaitable[Any]],
) -> Lease:
    """Lock the thread named in ``config`` before a run, or a no-op lease for a new thread.

    A request without a thread id is creating a thread: nobody can be running on it, and
    the service locks it as soon as the id exists (``GraphService``).
    """
    thread_id = requested_thread_id(config)
    if thread_id is None:
        return Lease(None)
    return await acquire_thread_lock(thread_id, on_busy=on_busy, stop=lambda: stop(thread_id))


def combine_leases(*leases: Lease) -> Lease:
    """One lease that releases all of ``leases`` (in the order given)."""

    def release() -> None:
        for lease in leases:
            lease.release()

    return Lease(release)
