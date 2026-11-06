"""Executor interface: how the worker runs a ``thread`` or ``process`` task body.

The worker keeps owning claims, leases, flushes and the final transition. An executor only
runs the body of one attempt and reports how it ended. The worker calls::

    executor = ThreadExecutor(threads=4)  # or ProcessExecutor(processes=2, ...)
    await executor.astart()  # once, at worker start (warms children)

    # per attempt, inside the attempt's asyncio task (so contextvars are visible):
    if executor.free_slots == 0:
        ...  # do not claim a run for this executor
    handle = await executor.start(
        ExecutionSpec(
            task=task, stored_kwargs=run.kwargs, ctx=ctx, max_result_bytes=runtime.max_result_bytes
        )
    )
    handle.request_cancel()  # a cancel was asked (cooperative first)
    handle.kill("lost")  # stop now: lost lease, shutdown, abort
    result = await handle.result()  # always returns, never raises

    await executor.aclose(grace=30.0)  # once, at worker shutdown

Progress: ``ctx.emit`` is the attempt's ``TaskEmitter``. A ``thread`` task writes to it
directly (it is lock-protected). A ``process`` child sends each emit call over its pipe and the
executor replays it into the same ``TaskEmitter`` on the loop. Either way the worker keeps
flushing ``attempt.emitter`` as for ``async`` tasks; nothing else is needed.

Cancel: ``request_cancel()`` makes ``ctx.cancelled`` true in the parent and in the body
(thread: the same ``TaskContext``; process: a flag in shared memory). A ``thread`` body runs on
until it returns; a ``process`` child is killed after ``cancel_grace`` seconds if it is still
running. ``kill(reason)`` resolves the result at once with ``kind="stopped"``.

Timeout: the executor enforces ``spec.timeout`` (default ``task.timeout``) itself and resolves
``kind="timeout"``. A timed-out or killed ``thread`` keeps its slot (``free_slots`` stays
lower) until the function returns; its result is discarded.
"""

from __future__ import annotations

import abc
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tenxgraph_api.jobs.context import TaskContext
from tenxgraph_api.jobs.task import Task


ResultKind = Literal["returned", "raised", "invalid", "timeout", "crashed", "stopped"]


class ExecutionSpec(BaseModel):
    """What the worker hands an executor for one attempt."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: Task
    #: The stored JSON kwargs (``run.kwargs``). The executor loads them with
    #: ``task.load_kwargs`` (in the child for ``process``) and adds ``ctx``.
    stored_kwargs: dict[str, Any]
    #: The attempt's context. Its ``emit`` receives every progress call.
    ctx: TaskContext
    #: Seconds for this attempt; None uses ``task.timeout`` (None there means no limit).
    timeout: float | None = None
    #: Result size limit, applied in the child for ``process`` tasks.
    max_result_bytes: int = 1024 * 1024

    def effective_timeout(self) -> float | None:
        return self.timeout if self.timeout is not None else self.task.timeout


class ExecutionResult(BaseModel):
    """How the body ended. ``error`` is ready to store (``code``, ``type``, ``message``).

    - ``returned``: ``value`` is the return value. When ``value_is_json`` is true (process)
      it was already dumped and size-checked with ``task.dump_result`` in the child: store it
      as is. Otherwise (thread) dump it with ``task.dump_result`` as for ``async`` tasks.
    - ``raised``: ``exception`` is what the body raised. ``Fail`` and ``RetryAfter`` keep
      their types (also from a child); any other child exception is a ``ChildTaskError``.
      ``error`` holds the original type name and message (``code="fail"`` for ``Fail``,
      plus ``data``); use it instead of building one from ``exception``.
    - ``invalid``: stored kwargs no longer match (``code="validation"``) or the child's
      result failed ``dump_result`` (``result_invalid`` / ``result_too_large``). Fail at once.
    - ``timeout``: ``spec.timeout`` passed (``code="timeout"``). Apply the retry policy.
    - ``crashed``: a ``process`` child died, went over the memory limit, could not import the
      task or could not start (``code`` ``crashed`` / ``memory_limit`` / ``child_import`` /
      ``child_start``). Apply the retry policy.
    - ``stopped``: ``kill(reason)`` ended it first; ``reason`` is the one passed. The worker
      decides from its own stop reason (cancel, shutdown, lost, abort).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: ResultKind
    value: Any = None
    value_is_json: bool = False
    exception: BaseException | None = None
    error: dict[str, Any] | None = None
    reason: str | None = None


class ChildTaskError(Exception):
    """An exception raised by a ``process`` task in its child, rebuilt in the parent."""

    def __init__(self, type_name: str, message: str, traceback_text: str = "") -> None:
        super().__init__(message)
        self.type_name = type_name
        self.message = message
        self.traceback_text = traceback_text


class ExecutionHandle(abc.ABC):
    """One running attempt. Every method is safe to call more than once and after the end."""

    @abc.abstractmethod
    async def result(self) -> ExecutionResult:
        """Wait for the end of the attempt. Never raises (cancelling the awaiting task only
        stops the wait; call ``kill`` to stop the body)."""

    @abc.abstractmethod
    def request_cancel(self) -> None:
        """Set ``ctx.cancelled``; a ``process`` child is killed after ``cancel_grace`` s."""

    @abc.abstractmethod
    def kill(self, reason: str) -> None:
        """Stop now: resolve ``kind="stopped"``. Thread: abandoned (keeps its slot until it
        returns). Process: the child is killed and replaced on the next start."""

    @property
    @abc.abstractmethod
    def done(self) -> bool:
        """True once ``result()`` would return without waiting."""


class Executor(abc.ABC):
    """A bounded pool of slots for one executor kind (``thread`` or ``process``)."""

    kind: str = ""

    @property
    @abc.abstractmethod
    def capacity(self) -> int:
        """Total slots (``--threads`` / ``--processes``)."""

    @property
    @abc.abstractmethod
    def free_slots(self) -> int:
        """Slots a new attempt can use now (abandoned threads still count as busy)."""

    async def astart(self) -> None:  # noqa: B027 - optional warm-up hook
        """Prepare slots before the first run (start children). Optional."""

    @abc.abstractmethod
    async def start(self, spec: ExecutionSpec) -> ExecutionHandle:
        """Start the body. Waits for a free slot when none is free. Raises ``RuntimeError``
        after ``aclose``."""

    @abc.abstractmethod
    async def aclose(self, *, grace: float = 30.0) -> None:
        """Stop accepting work, wait up to ``grace`` s for running bodies, then kill
        (process) or abandon (thread) the rest. Leaves no child process behind."""


def error_dict(code: str, type_name: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "type": type_name, "message": message, **extra}


def raised_result(exc: BaseException) -> ExecutionResult:
    """``raised`` result for an exception caught in this process."""
    from tenxgraph_api.jobs.control import Fail

    error = error_dict("fail" if isinstance(exc, Fail) else "error", type(exc).__name__, str(exc))
    if isinstance(exc, Fail) and exc.data is not None:
        error["data"] = exc.data
    return ExecutionResult(kind="raised", exception=exc, error=error)
