"""``TaskContext``: what a running task can see and do."""

from __future__ import annotations

import threading
from typing import Any

from .emit import TaskEmitter


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

    def __repr__(self) -> str:
        return f"TaskContext(run_id={self.run_id!r}, task={self.task!r}, attempt={self.attempt})"
