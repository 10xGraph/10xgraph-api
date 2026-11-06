"""``on_task_progress`` hooks on one background task, fed by a bounded queue (off the tick)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from tenxgraph_api.jobs.core.hooks import HookRunner
from tenxgraph_api.jobs.core.models import TaskRun


logger = logging.getLogger("tenxgraph_api.jobs")

#: Pending ``on_task_progress`` calls; more are dropped with a warning.
HOOK_QUEUE_SIZE = 1000


class ProgressHooks:
    """Queue progress events for the hooks; drop (and count) them when the queue is full."""

    def __init__(self, runner: HookRunner) -> None:
        self.runner = runner
        self.dropped = 0
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def put(self, run: TaskRun, event: dict[str, Any]) -> None:
        if not self.runner.hooks:
            return
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=HOOK_QUEUE_SIZE)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(self._queue))
        try:
            self._queue.put_nowait((run, event))
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                logger.warning(
                    "on_task_progress hooks are too slow: %s events dropped", self.dropped
                )

    async def _loop(self, queue: asyncio.Queue) -> None:
        while True:
            run, event = await queue.get()
            try:
                await self.runner.emit("on_task_progress", run, event)
            finally:
                queue.task_done()

    async def idle(self, timeout: float | None = None) -> None:
        """Wait until every queued hook call has run."""
        if self._queue is not None and self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=timeout)

    async def close(self, timeout: float = 5.0) -> None:
        """Let queued calls run for up to ``timeout`` s, then stop the task."""
        await self.idle(timeout)
        await self._cancel()

    async def abort(self) -> None:
        """Stop the task and drop what is queued."""
        await self._cancel()
        self._queue = None

    async def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
