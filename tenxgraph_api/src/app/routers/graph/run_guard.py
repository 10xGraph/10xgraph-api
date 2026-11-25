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

from tenxgraph.core.authz import Identity
from tenxgraph.utils.temp_perf import done as _perf_done  # TEMP-PERF
from tenxgraph.utils.temp_perf import now as _perf_now  # TEMP-PERF

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
    user: Identity | None,
    runs: RunsConfig,
    thread_id: str | None,
    on_busy: OnBusy = "wait",
    stop: Callable[[], Awaitable[Any]] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    recheck: Callable[[], Awaitable[Any]] | None = None,
) -> Lease:
    """Lock ``thread_id`` and take a run slot for ``user``; release both with the one lease.

    ``thread_id`` is ``None`` for a run that starts a new thread (the service locks that one
    once its id exists). Raises ``HTTPException``: 409 when the thread stays busy, 429 over the
    run cap, 499 when ``is_disconnected`` says the client left while waiting. Nothing stays held
    when it raises.

    ``recheck`` runs once the thread is locked: the scoped thread read again
    (``require_run_thread``), since under ``runs.thread_ids: "client"`` another user may have
    created the id while this run waited for the lock. What it raises is raised here.
    """
    thread_lock = Lease(None)
    if thread_id is not None:
        _tp0 = _perf_now()  # TEMP-PERF
        thread_lock = await acquire_thread_lock(
            thread_id, on_busy=on_busy, stop=stop, is_disconnected=is_disconnected
        )
        _perf_done("api_thread_lock", _tp0)  # TEMP-PERF
    try:
        if recheck is not None and thread_id is not None:
            _tp0 = _perf_now()  # TEMP-PERF
            await recheck()
            _perf_done("api_thread_recheck", _tp0)  # TEMP-PERF
        _tp0 = _perf_now()  # TEMP-PERF
        slot = await acquire_run_slot(user, runs)
        _perf_done("api_run_slot", _tp0)  # TEMP-PERF
    except BaseException:
        thread_lock.release()
        raise
    return combine_leases(slot, thread_lock)
