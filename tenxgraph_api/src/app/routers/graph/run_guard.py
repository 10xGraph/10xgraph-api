"""The guard every run entry point takes before a graph run starts.

A run needs two things held for as long as it lasts: the thread's one-run lock
(``thread_lock``) and the user's run slot (``run_limits``). Invoke, stream, AG-UI,
``/v1/graph/ws`` and ``/v1/graph/live`` all take both through :func:`acquire_run_guard`, so a
new entry point cannot pick up one and forget the other. The result is one lease; release it
when the run is over (``release_when_done`` for a response stream, ``finally`` otherwise).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.leases import Lease
from tenxgraph_api.src.app.routers.graph.run_limits import acquire_run_slot
from tenxgraph_api.src.app.routers.graph.thread_lock import (
    OnBusy,
    acquire_thread_lock,
    combine_leases,
)


async def acquire_run_guard(
    *,
    user: dict[str, Any],
    runs: RunsConfig,
    thread_id: str | None,
    on_busy: OnBusy = "wait",
    stop: Callable[[], Awaitable[Any]] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> Lease:
    """Lock ``thread_id`` and take a run slot for ``user``; release both with the one lease.

    ``thread_id`` is ``None`` for a run that starts a new thread (the service locks that one
    once its id exists). Raises ``HTTPException``: 409 when the thread stays busy, 429 over the
    run cap, 499 when ``is_disconnected`` says the client left while waiting. Nothing stays held
    when it raises.
    """
    thread_lock = Lease(None)
    if thread_id is not None:
        thread_lock = await acquire_thread_lock(
            thread_id, on_busy=on_busy, stop=stop, is_disconnected=is_disconnected
        )
    try:
        slot = await acquire_run_slot(user, runs)
    except BaseException:
        thread_lock.release()
        raise
    return combine_leases(slot, thread_lock)
