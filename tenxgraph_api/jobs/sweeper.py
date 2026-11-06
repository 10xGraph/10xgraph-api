"""One sweeper pass: recover expired leases, publish due runs, purge expired runs.

Sprint 1 ships the pass itself (the harness runs it on every ``drain`` round). Sprint 2 runs
it every second on the leader, with the fallback described in JOBS_DESIGN "Scheduler". Every
step is a store CAS, so running it twice, or on two pods, is safe.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from . import registry
from .client import publish_run
from .hooks import call_safely
from .models import REPUBLISH_AFTER, RunState
from .runtime import JobsRuntime


logger = logging.getLogger("tenxgraph_api.jobs")


class SweepStats(BaseModel):
    """Counts from one sweeper pass."""

    lost: int = 0
    published: int = 0
    purged: int = 0


async def sweep_once(
    runtime: JobsRuntime,
    *,
    limit: int = 100,
    republish_after: float = REPUBLISH_AFTER,
    purge: bool = True,
) -> SweepStats:
    store = runtime.store
    hooks = runtime.hook_runner
    stats = SweepStats()
    for lease in await store.find_expired_leases(limit=limit):
        recovered = await store.mark_lost(lease.run_id, attempt=lease.attempt)
        if recovered is None:
            continue
        run = recovered.run
        stats.lost += 1
        logger.warning(
            "task run lost run_id=%s task=%s attempt=%s lost_count=%s now=%s",
            run.run_id,
            run.task,
            lease.attempt,
            run.lost_count,
            run.state.value,
        )
        await hooks.emit("on_task_lost", run)
        if run.state == RunState.FAILED:
            await hooks.emit("on_task_failure", run, run.error)
            task = registry.all_tasks().get(run.task)
            if task is not None and task.on_failure is not None:
                await call_safely(task.on_failure, run, label=f"{task.name}.on_failure")
        elif run.state == RunState.CANCELLED:
            await hooks.emit("on_task_cancel", run)
    for run in await store.due_runs(limit=limit):
        if await publish_run(runtime, run, republish_after=republish_after):
            stats.published += 1
    if purge:
        stats.purged = await store.purge_expired(limit=limit * 10)
    return stats
