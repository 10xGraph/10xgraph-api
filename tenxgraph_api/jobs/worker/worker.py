"""The in-process worker: consume, claim, start attempts, run loop and shutdown.

The attempt itself lives in ``attempt.py``, the tick (renewals, deadlines, flushes) in
``heartbeat.py``. JOBS_DESIGN "Worker behaviour" lists the guarantees.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from tenxgraph_api.jobs.core import registry
from tenxgraph_api.jobs.core.context import TaskContext
from tenxgraph_api.jobs.core.emit import TaskEmitter
from tenxgraph_api.jobs.core.models import PRIORITIES, ClaimStatus, TaskRun
from tenxgraph_api.jobs.core.runtime import JobsRuntime
from tenxgraph_api.jobs.core.task import Task
from tenxgraph_api.jobs.transport.base import Delivery

from .attempt import Attempt, AttemptRunner
from .heartbeat import LeaseKeeper
from .hooks_queue import ProgressHooks
from .logs import log_run


logger = logging.getLogger("tenxgraph_api.jobs")

QueuesSpec = Sequence[str] | Callable[[], Sequence[str]] | None

#: Seconds before a run of a task this worker does not know is published again.
UNKNOWN_TASK_RETRY = 30.0
#: A run read without a slot for it is published again after this many seconds, and the
#: worker pauses reading this long after giving one back.
RELEASE_RETRY = 2.0
RELEASE_PAUSE = 1.0
#: Backoff of the poll loop after a store or transport error.
POLL_ERROR_BASE = 0.1
POLL_ERROR_MAX = 5.0


def check_reserve(reserve: Mapping[str, int] | None, concurrency: int) -> dict[str, int]:
    """Validate ``--reserve``: levels above ``low``, counts >= 0, fewer than ``concurrency``."""
    out: dict[str, int] = {}
    for level, count in (reserve or {}).items():
        if level not in PRIORITIES[:-1]:
            raise ValueError(
                f"reserve: {level!r} is not a priority that can hold slots; use "
                + " or ".join(PRIORITIES[:-1])
            )
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"reserve: {level}={count!r} must be a whole number >= 0")
        if count:
            out[level] = count
    if sum(out.values()) >= concurrency:
        raise ValueError(
            f"reserve: {sum(out.values())} reserved slots leave none of the {concurrency} "
            "for low priority; reserve fewer or raise concurrency"
        )
    return out


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _takes_priorities(consume: Callable[..., Any]) -> bool:
    try:
        return "priorities" in inspect.signature(consume).parameters
    except (TypeError, ValueError):
        return False


class Worker(AttemptRunner, LeaseKeeper):
    """Runs tasks from the runtime's transport and store."""

    def __init__(  # noqa: PLR0913 - worker knobs
        self,
        runtime: JobsRuntime,
        *,
        queues: QueuesSpec = None,
        concurrency: int = 16,
        worker_id: str | None = None,
        lease_ttl: float | None = None,
        lease_margin: float | None = None,
        flush_interval: float = 1.0,
        monotonic: Callable[[], float] | None = None,
        reserve: Mapping[str, int] | None = None,
        executors: Mapping[str, Any] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.reserve = check_reserve(reserve, concurrency)
        #: Non-async executors by name (``"thread"``, ``"process"``).
        self.executors = dict(executors or {})
        self.runtime = runtime
        self.store = runtime.store
        self.transport = runtime.transport
        self._queues = queues
        self.concurrency = concurrency
        self.worker_id = worker_id or default_worker_id()
        self.lease_ttl = lease_ttl if lease_ttl is not None else runtime.lease_ttl
        #: Stop an attempt this long before its store lease would expire without a renewal.
        self.lease_margin = lease_margin if lease_margin is not None else self.lease_ttl / 5
        self.flush_interval = flush_interval
        self._monotonic = monotonic or time.monotonic
        #: False simulates renewals that do not reach the store (tests); deadlines still apply.
        self.heartbeats = True
        self._inflight = {}
        #: Attempts that lost their lease but have not returned: they keep a slot.
        self._stale = set()
        self._stopping = False
        self._slot_freed = asyncio.Event()
        self._hooks = ProgressHooks(runtime.hook_runner)
        self.poll_errors = 0
        #: Worker-monotonic time of the last finished tick (``/healthz``).
        self.last_tick: float | None = None
        #: Without ``priorities=`` on ``consume``, a reserved-slot read takes one message.
        self._filtered_reads = _takes_priorities(self.transport.consume)
        #: Runs the last ``poll_once`` read but gave back (see ``_release``).
        self.released = 0

    # ------------------------------------------------------------------ introspection

    @property
    def free_slots(self) -> int:
        return max(0, self.concurrency - len(self._inflight) - len(self._stale))

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def inflight(self) -> dict[str, Attempt]:
        return dict(self._inflight)

    @property
    def hooks_dropped(self) -> int:
        return self._hooks.dropped

    def slot_limit(self, priority: str) -> int:
        """Slots ``priority`` may use: ``concurrency`` minus those reserved for higher levels."""
        rank = PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES) - 1
        return self.concurrency - sum(self.reserve.get(p, 0) for p in PRIORITIES[:rank])

    def capacity(self, priority: str) -> int:
        """Slots still free for a run of ``priority``."""
        return max(0, self.slot_limit(priority) - len(self._inflight) - len(self._stale))

    def queues(self) -> list[str]:
        spec = self._queues
        if spec is None:
            return sorted({t.queue for t in registry.all_tasks().values()} | {"default"})
        return list(spec() if callable(spec) else spec)

    def jobs(self) -> list[asyncio.Task]:
        attempts = [*self._inflight.values(), *self._stale]
        return [a.job for a in attempts if a.job is not None]

    def executor_for(self, task: Task) -> Any:
        """The executor of a ``thread`` / ``process`` task; None for ``async`` or unknown."""
        return None if task.executor == "async" else self.executors.get(task.executor)

    async def hooks_idle(self, timeout: float | None = None) -> None:
        """Wait until every queued progress hook has run."""
        await self._hooks.idle(timeout)

    # ------------------------------------------------------------------ consume and claim

    async def poll_once(self, *, block: float = 0.0) -> int:
        """Read and start up to ``free_slots`` runs; returns how many started.

        A failing delivery stays unacked (``poll_errors`` goes up); a failing ``consume`` raises.
        """
        if self._stopping or self.free_slots == 0:
            return 0
        queues = self.queues()
        if not queues:
            return 0
        started = 0
        self.released = 0
        waited = False
        previous_limit: int | None = None
        # From the lowest level up, each tier reads its level and those above it for the slots
        # that level may use. Without reservations this is one read over all priorities.
        for index in range(len(PRIORITIES) - 1, -1, -1):
            limit = self.slot_limit(PRIORITIES[index])
            if limit == previous_limit:
                continue
            previous_limit = limit
            room = self.capacity(PRIORITIES[index])
            if room <= 0 or self._stopping:
                continue
            levels = PRIORITIES[: index + 1]
            options: dict[str, Any] = {}
            if len(levels) < len(PRIORITIES):
                if self._filtered_reads:
                    options["priorities"] = list(levels)
                else:
                    room = 1  # one at a time; a lower level is given back (_release)
            deliveries = await self.transport.consume(
                queues, max_messages=room, block=0.0 if waited else block, **options
            )
            waited = True
            for delivery in deliveries:
                started += await self._start_delivery(delivery)
        return started

    async def _start_delivery(self, delivery: Delivery) -> int:
        try:
            return 1 if await self._handle(delivery) else 0
        except Exception:
            self.poll_errors += 1
            logger.exception(
                "jobs worker could not handle run %s; it will be delivered again",
                delivery.message.run_id,
            )
            return 0

    async def _ack(self, delivery: Delivery) -> None:
        """Best effort: a failed ack only means the message may come again."""
        try:
            await self.transport.ack([delivery])
        except Exception:
            logger.warning(
                "jobs ack failed for run %s (harmless: a redelivery is skipped by the claim)",
                delivery.message.run_id,
                exc_info=True,
            )

    async def _handle(self, delivery: Delivery) -> bool:
        """Claim the delivered run and start it; False when it is skipped or given back."""
        claimed_at = self._monotonic()
        run_id = delivery.message.run_id
        current = await self.store.get_run(run_id)
        if current is None or current.terminal:
            await self._ack(delivery)
            return False
        task = registry.all_tasks().get(current.task)
        if task is None or not (task.executor == "async" or task.executor in self.executors):
            await self._ack(delivery)
            await self._leave_for_another_pod(current)
            return False
        executor = self.executor_for(task)
        if (
            self._stopping
            or self.capacity(current.priority) <= 0
            or (executor is not None and executor.free_slots <= 0)
        ):
            await self._release(delivery, current)
            return False
        result = await self.store.claim(
            run_id,
            worker_id=self.worker_id,
            lease_ttl=self.lease_ttl,
            concurrency_limit=task.concurrency,
        )
        await self._ack(delivery)
        if result.status != ClaimStatus.CLAIMED or result.run is None:
            if result.status == ClaimStatus.CAPPED:
                logger.debug("task run capped run_id=%s task=%s", run_id, current.task)
            return False
        previous = self._inflight.get(run_id)
        if previous is not None:  # our own stalled attempt: the store gave the run away
            self._lose(previous, "run was claimed again")
        self._start(result.run, task, claimed_at + self.lease_ttl - self.lease_margin)
        return True

    async def _release(self, delivery: Delivery, run: TaskRun) -> None:
        """Give back a run with no slot here: ack, and the sweeper publishes it again later."""
        self.released += 1
        await self._ack(delivery)
        await self.store.mark_published([run], republish_after=RELEASE_RETRY)
        logger.debug("task run released run_id=%s task=%s: no free slot", run.run_id, run.task)

    async def _leave_for_another_pod(self, run: TaskRun) -> None:
        """Unknown task here (rolling deploy): leave the run queued, publish it again later."""
        logger.warning(
            "task %r is not registered in worker %s; run %s stays queued and is published "
            "again in %s s",
            run.task,
            self.worker_id,
            run.run_id,
            UNKNOWN_TASK_RETRY,
        )
        await self.store.mark_published([run], republish_after=UNKNOWN_TASK_RETRY)

    def _start(self, run: TaskRun, task: Task, lease_deadline: float) -> None:
        emitter = TaskEmitter(clock=self.runtime.clock)
        ctx = TaskContext(
            run_id=run.run_id,
            attempt=run.attempt,
            task=run.task,
            queue=run.queue,
            identity=dict(run.identity),
            emit=emitter,
        )
        attempt = Attempt(
            run=run, task=task, ctx=ctx, emitter=emitter, lease_deadline=lease_deadline
        )
        self._inflight[run.run_id] = attempt
        log_run(logger, logging.INFO, "started", run, worker=self.worker_id, queue=run.queue)
        attempt.job = asyncio.create_task(self._execute(attempt), name=f"task-run:{run.run_id}")

    # ------------------------------------------------------------------ loop and shutdown

    async def run(self, *, block: float = 1.0, tick_interval: float | None = None) -> None:
        """Consume until ``aclose()``, then tick on until every attempt has written."""
        ticker = asyncio.create_task(self._tick_loop(tick_interval or min(1.0, self.lease_ttl / 3)))
        backoff = POLL_ERROR_BASE
        try:
            while not self._stopping:
                if self.free_slots == 0:
                    await self._wait_slot(block)
                    continue
                errors = self.poll_errors
                started = 0
                try:
                    started = await self.poll_once(block=block)
                except Exception:
                    self.poll_errors += 1
                    logger.exception("jobs worker poll failed; backing off %.1f s", backoff)
                if self.poll_errors != errors:
                    await asyncio.sleep(backoff)
                    backoff = min(POLL_ERROR_MAX, backoff * 2)
                    continue
                backoff = POLL_ERROR_BASE
                if self.released and not started:
                    await self._wait_slot(RELEASE_PAUSE)
            while jobs := self.jobs():
                await asyncio.wait(jobs)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            await self._hooks.close()

    async def _wait_slot(self, timeout: float) -> None:
        self._slot_freed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._slot_freed.wait(), timeout)

    async def _tick_loop(self, interval: float) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("jobs worker tick failed; retrying")
            else:
                self.last_tick = self._monotonic()
            await asyncio.sleep(interval)

    async def aclose(self, *, grace: float = 30.0) -> None:
        """Stop reading, wait ``grace`` s, then stop the rest and requeue them (no retry used).

        Leases are renewed meanwhile by ``run()``'s ticker; a hand-driven worker must ``tick()``.
        """
        self._stopping = True
        jobs = self.jobs()
        if jobs:
            _, pending = await asyncio.wait(jobs, timeout=grace)
            for attempt in list(self._inflight.values()):
                if attempt.job in pending:
                    self._stop(attempt, "shutdown")
            if pending:
                await asyncio.wait(pending)
        await self._hooks.idle(timeout=5.0)

    async def abort(self) -> None:
        """Stop at once and write nothing (a crash, as the store sees it)."""
        self._stopping = True
        for attempt in [*self._inflight.values(), *self._stale]:
            attempt.fenced_out = True
            self._stop(attempt, "abort")
            if attempt.job is not None:
                attempt.job.cancel()
        jobs = self.jobs()
        if jobs:
            await asyncio.wait(jobs)
        self._inflight.clear()
        self._stale.clear()
        await self._hooks.abort()

    def resume(self) -> None:
        """Accept work again after ``abort`` (same worker id, like a restarted pod)."""
        self._stopping = False
