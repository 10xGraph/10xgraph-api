"""``TaskContext``: what a running task can see and do."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from .emit import TaskEmitter


#: Namespace for ``thread_id()``; fixed forever so ids stay stable across releases.
THREAD_NAMESPACE = uuid.UUID("5b0f8e8e-6d44-4b8e-9f0e-10a9a7a0c0de")


class TaskContext:
    """Injected into a task by type (``ctx: TaskContext``); optional.

    ``run_id`` is a good idempotency key for external APIs: it is the same on every attempt.
    """

    def __init__(
        self,
        *,
        run_id: str,
        attempt: int,
        task: str,
        queue: str,
        identity: dict[str, Any],
        emit: TaskEmitter,
    ) -> None:
        self.run_id = run_id
        self.attempt = attempt
        self.task = task
        self.queue = queue
        self.identity = identity
        self.emit = emit
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        """True once a cancel was asked (or the attempt is being stopped). Check it in long
        loops; an ``async`` task is also cancelled with ``asyncio`` cancellation."""
        return self._cancelled.is_set()

    def _set_cancelled(self) -> None:
        self._cancelled.set()

    def thread_id(self) -> str:
        """Graph thread id for this run: a UUID derived from ``run_id``, same on every attempt."""
        return str(uuid.uuid5(THREAD_NAMESPACE, self.run_id))

    async def invoke_graph(self, input_data: dict[str, Any], config: dict | None = None) -> Any:
        """Run the loaded graph as this run (arrives in Sprint 3 with ``10xgraph worker``)."""
        raise NotImplementedError(
            "ctx.invoke_graph is not available yet: it needs the worker's loaded graph and "
            "arrives with `10xgraph worker` (jobs Sprint 3)"
        )

    def __repr__(self) -> str:
        return f"TaskContext(run_id={self.run_id!r}, task={self.task!r}, attempt={self.attempt})"
