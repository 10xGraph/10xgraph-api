"""Enqueue, run handles and run lookup (the caller side)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import registry
from .clock import to_epoch
from .errors import RunNotFound, TaskCancelled, TaskFailed
from .identity import current_task_context, resolve_identity
from .models import (
    REPUBLISH_AFTER,
    CancelStatus,
    KeyMode,
    NewRun,
    RunFilter,
    RunPage,
    RunState,
    TaskEvent,
    TaskRun,
)
from .runtime import JobsRuntime, get_runtime
from .transport.base import TransportMessage


if TYPE_CHECKING:
    from .task import TaskCall


logger = logging.getLogger("tenxgraph_api.jobs")

POLL_START = 0.05
POLL_MAX = 1.0


async def publish_run(
    runtime: JobsRuntime, run: TaskRun, *, republish_after: float = REPUBLISH_AFTER
) -> bool:
    """Publish the wake-up for ``run`` and mark it published. A failure is logged and left to
    the sweeper (the run is already safe in the store)."""
    try:
        await runtime.transport.publish(
            TransportMessage(run_id=run.run_id, queue=run.queue, priority=run.priority)
        )
        await runtime.store.mark_published([run.run_id], republish_after=republish_after)
    except Exception:
        logger.exception("publish of run %s failed; the sweeper will republish it", run.run_id)
        return False
    return True


async def enqueue_call(call: TaskCall, kwargs: dict[str, Any]) -> RunHandle:
    runtime = get_runtime()
    task = call.task
    data = task.validate_kwargs(kwargs, max_bytes=runtime.max_kwargs_bytes)
    identity = resolve_identity(call.identity)
    parent = current_task_context.get()
    retention = task.retention if task.retention is not None else runtime.result_ttl
    new = NewRun(
        run_id=runtime.new_run_id(),
        task=task.name,
        queue=task.queue,
        priority=call.priority or task.priority,  # type: ignore[arg-type]
        kwargs=data,
        identity=identity,
        owner_user_id=identity["user_id"],
        idempotency_key=call.key,
        concurrency_key=task.concurrency_key_for(data, identity),
        max_retries=task.retries,
        max_lost=task.max_lost,
        retention=retention,
        trigger=call.trigger or {"type": "code"},
        parent_run_id=parent.run_id if parent is not None else None,
        delay=call.delay,
        at=to_epoch(call.at) if call.at is not None else None,
    )
    result = await runtime.store.create_run(new, key_mode=call.key_mode)
    run = result.run
    if result.due:
        await publish_run(runtime, run)
    if result.created:
        logger.info(
            "task run queued run_id=%s task=%s queue=%s priority=%s",
            run.run_id,
            run.task,
            run.queue,
            run.priority,
        )
        await runtime.hook_runner.emit("on_task_queued", run)
    return RunHandle(run.run_id, runtime)


async def enqueue(
    name: str,
    kwargs: dict[str, Any] | None = None,
    *,
    identity: Any = None,
    key: str | None = None,
    key_mode: KeyMode = "keep",
    priority: str | None = None,
    delay: float | None = None,
    at: datetime | float | None = None,
    trigger: dict[str, Any] | None = None,
) -> RunHandle:
    """Enqueue a task by its registered name. Raises ``TaskNotFound`` for an unknown name."""
    task = registry.get_task(name)
    call = task.options(
        identity=identity,
        key=key,
        key_mode=key_mode,
        priority=priority,
        delay=delay,
        at=at,
        trigger=trigger,
    )
    return await call.enqueue(**(kwargs or {}))


class RunHandle:
    """A handle on one run. Cheap: it holds only the id and the runtime."""

    def __init__(self, run_id: str, runtime: JobsRuntime | None = None) -> None:
        self.id = run_id
        self._runtime = runtime

    @property
    def runtime(self) -> JobsRuntime:
        return self._runtime or get_runtime()

    async def status(self) -> TaskRun:
        """The stored record. Raises ``RunNotFound`` once retention removed it."""
        run = await self.runtime.store.get_run(self.id)
        if run is None:
            raise RunNotFound(f"run {self.id} not found")
        return run

    async def result(self, timeout: float | None = None) -> Any:
        """Wait for the run to end and return its result (JSON form).

        Raises ``TaskFailed``, ``TaskCancelled``, or ``TimeoutError`` after ``timeout`` seconds.
        Polls the store (50 ms growing to 1 s).
        """
        delay = POLL_START
        async with asyncio.timeout(timeout):
            while True:
                run = await self.status()
                if run.state == RunState.SUCCEEDED:
                    return run.result
                if run.state == RunState.FAILED:
                    raise TaskFailed(run)
                if run.state == RunState.CANCELLED:
                    raise TaskCancelled(run)
                await asyncio.sleep(delay)
                delay = min(POLL_MAX, delay * 2)

    async def cancel(self) -> TaskRun:
        """Cancel: a queued or retrying run is ``cancelled`` at once; a running one becomes
        ``cancelling`` and the worker stops it. A finished run is returned unchanged."""
        runtime = self.runtime
        outcome = await runtime.store.request_cancel(self.id)
        if outcome.status == CancelStatus.NOT_FOUND or outcome.run is None:
            raise RunNotFound(f"run {self.id} not found")
        if outcome.status == CancelStatus.CANCELLED:
            logger.info("task run cancelled run_id=%s task=%s", self.id, outcome.run.task)
            await runtime.hook_runner.emit("on_task_cancel", outcome.run)
        return outcome.run

    async def events(self, after: int = 0, *, page_size: int = 100) -> AsyncIterator[TaskEvent]:
        """Stored events with ``seq > after``, oldest first, until the log is exhausted.

        This reads what is stored now; it does not wait for new events.
        """
        cursor = after
        while True:
            batch = await self.runtime.store.read_events(self.id, after=cursor, limit=page_size)
            for event in batch:
                yield event
            if len(batch) < page_size:
                return
            cursor = batch[-1].seq

    def __repr__(self) -> str:
        return f"RunHandle({self.id!r})"


class Runs:
    """``runs.get`` / ``runs.list``. Python access is trusted code, not owner-scoped."""

    async def get(self, run_id: str) -> RunHandle:
        """A handle on an existing run. Raises ``RunNotFound``."""
        runtime = get_runtime()
        if await runtime.store.get_run(run_id) is None:
            raise RunNotFound(f"run {run_id} not found")
        return RunHandle(run_id, runtime)

    async def list(
        self,
        *,
        task: str | None = None,
        state: str | tuple[str, ...] | None = None,
        queue: str | None = None,
        owner_user_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        """Runs newest first; pass ``page.next_cursor`` back as ``cursor`` for more."""
        filters = RunFilter(task=task, state=state, queue=queue, owner_user_id=owner_user_id)
        return await get_runtime().store.list_runs(filters, limit=limit, cursor=cursor)


runs = Runs()
