"""``jobs_harness``: run tasks in a test with a memory store, a memory transport, a fake clock
and an in-process worker.

Example::

    from tenxgraph_api.jobs.testing import jobs_harness


    async def test_summary():
        async with jobs_harness() as jobs:
            call = summarize_interview.options(identity=jobs.user("u1"))
            run = await call.enqueue(interview_id="42")
            await jobs.drain()
            assert (await run.status()).state == "succeeded"
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from tenxgraph_api.jobs.core.clock import FakeClock
from tenxgraph_api.jobs.core.hooks import TaskLifecycleHook
from tenxgraph_api.jobs.core.identity import user_identity
from tenxgraph_api.jobs.core.models import CLAIMABLE_STATES
from tenxgraph_api.jobs.core.runtime import (
    DEFAULT_LEASE_TTL,
    DEFAULT_MAX_KWARGS_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_RESULT_TTL,
    JobsRuntime,
    use_runtime,
)
from tenxgraph_api.jobs.store.memory import MemoryRunStore
from tenxgraph_api.jobs.transport.memory import MemoryTransport
from tenxgraph_api.jobs.worker.sweeper import SweepStats, sweep_once
from tenxgraph_api.jobs.worker.worker import Worker


class JobsHarness:
    """What ``jobs_harness`` yields."""

    def __init__(self, runtime: JobsRuntime, worker: Worker, clock: FakeClock) -> None:
        self.runtime = runtime
        self.worker = worker
        self.clock = clock
        self.store: MemoryRunStore = runtime.store  # type: ignore[assignment]
        self.transport: MemoryTransport = runtime.transport  # type: ignore[assignment]

    @staticmethod
    def user(user_id: str, **claims: Any) -> dict[str, Any]:
        """An identity for ``user_id`` (``{"user_id": ...}`` plus claims)."""
        return user_identity(user_id, **claims)

    async def sweep(self) -> SweepStats:
        """One sweeper pass: recover expired leases, publish due runs, purge."""
        return await sweep_once(self.runtime)

    async def drain(self, *, follow_delays: bool = False, timeout: float = 30.0) -> None:
        """Run everything due until nothing is left to do.

        Each round: heartbeat and flush, one sweeper pass, start what the transport holds,
        let running attempts make progress. Runs waiting on a concurrency cap are moved on by
        advancing the fake clock to their next try. With ``follow_delays`` the clock also
        jumps to every delayed or retrying run's ``available_at``, so retries run to the end.
        ``timeout`` is real seconds; a task that never returns raises ``TimeoutError``.
        """
        async with asyncio.timeout(timeout):
            while True:
                await self.worker.tick()
                await self.sweep()
                started = await self.worker.poll_once()
                jobs = self.worker.jobs()
                if jobs:
                    await asyncio.wait(jobs, timeout=0.001, return_when=asyncio.FIRST_COMPLETED)
                    continue
                if started or self.transport.size():
                    await asyncio.sleep(0)
                    continue
                target = self._next_wake(follow_delays)
                if target is None or target <= self.clock.now():
                    await self.worker.hooks_idle()
                    return
                self.clock.set(target)

    def _next_wake(self, follow_delays: bool) -> float | None:
        times = [
            run.available_at
            for run in self.store.snapshot().values()
            if run.state in CLAIMABLE_STATES and (follow_delays or run.cap_waits > 0)
        ]
        return min(times) if times else None


@contextlib.asynccontextmanager
async def jobs_harness(
    *,
    start: float | datetime | None = None,
    hooks: Sequence[TaskLifecycleHook] = (),
    concurrency: int = 16,
    lease_ttl: float = DEFAULT_LEASE_TTL,
    flush_interval: float = 1.0,
    result_ttl: float | None = DEFAULT_RESULT_TTL,
    max_kwargs_bytes: int = DEFAULT_MAX_KWARGS_BYTES,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> AsyncIterator[JobsHarness]:
    """Bind a memory jobs runtime for the block; tear it down after.

    The fake clock drives the store (``available_at``, leases), progress timestamps and the
    worker's flush rate. Attempt ``timeout`` uses the event loop's real clock.
    """
    clock = FakeClock(start)
    store = MemoryRunStore(clock)
    transport = MemoryTransport()
    runtime = JobsRuntime(
        store=store,
        transport=transport,
        hooks=hooks,
        clock=clock,
        lease_ttl=lease_ttl,
        result_ttl=result_ttl,
        max_kwargs_bytes=max_kwargs_bytes,
        max_result_bytes=max_result_bytes,
    )
    worker = Worker(
        runtime,
        queues=transport.queue_names,
        concurrency=concurrency,
        flush_interval=flush_interval,
        monotonic=clock,
        worker_id="harness-worker",
    )
    with use_runtime(runtime):
        try:
            yield JobsHarness(runtime, worker, clock)
        finally:
            await worker.abort()
            await transport.aclose()
            await store.aclose()
