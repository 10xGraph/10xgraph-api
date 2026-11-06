"""``thread`` executor: plain ``def`` tasks on a bounded thread pool (``--threads``).

- The body runs in ``contextvars.copy_context()`` of the caller, so InjectQ's container
  context (``Inject[...]`` defaults resolve through ``InjectQ.get_instance()``) and the
  current task context are the same as for an ``async`` task.
- ``ctx.emit`` is the attempt's lock-protected ``TaskEmitter``: the thread writes to the
  buffer, the worker's loop takes it in ``tick``.
- A running thread cannot be stopped. Timeout and ``kill`` resolve the result at once and set
  ``ctx.cancelled``; the thread keeps its slot until the function returns, and its return
  value or exception is discarded.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
from typing import Any

from tenxgraph_api.jobs.errors import TaskValidationError

from .base import (
    ExecutionHandle,
    ExecutionResult,
    ExecutionSpec,
    Executor,
    error_dict,
    raised_result,
)


logger = logging.getLogger("tenxgraph_api.jobs")


class ThreadHandle(ExecutionHandle):
    def __init__(self, spec: ExecutionSpec, loop: asyncio.AbstractEventLoop) -> None:
        self.spec = spec
        self._loop = loop
        self._outcome: asyncio.Future[ExecutionResult] = loop.create_future()
        self._timer: asyncio.TimerHandle | None = None

    async def result(self) -> ExecutionResult:
        return await asyncio.shield(self._outcome)

    @property
    def done(self) -> bool:
        return self._outcome.done()

    def request_cancel(self) -> None:
        self.spec.ctx._set_cancelled()

    def kill(self, reason: str) -> None:
        self.spec.ctx._set_cancelled()
        self._resolve(ExecutionResult(kind="stopped", reason=reason))

    def _on_timeout(self) -> None:
        self.spec.ctx._set_cancelled()
        timeout = self.spec.effective_timeout()
        self._resolve(
            ExecutionResult(
                kind="timeout",
                error=error_dict(
                    "timeout", "TimeoutError", f"attempt exceeded timeout of {timeout} s"
                ),
            )
        )

    def _resolve(self, result: ExecutionResult) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._outcome.done():
            self._outcome.set_result(result)


class ThreadExecutor(Executor):
    """``threads`` slots. A slot is busy from ``start`` until the function returns."""

    kind = "thread"

    def __init__(self, threads: int = 4) -> None:
        if threads < 1:
            raise ValueError("threads must be >= 1")
        self._threads = threads
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=threads, thread_name_prefix="tenxgraph-task"
        )
        self._busy = 0
        self._freed = asyncio.Event()
        self._running: set[asyncio.Future[Any]] = set()
        self._closed = False

    @property
    def capacity(self) -> int:
        return self._threads

    @property
    def free_slots(self) -> int:
        return max(0, self._threads - self._busy)

    async def start(self, spec: ExecutionSpec) -> ThreadHandle:
        if self._closed:
            raise RuntimeError("thread executor is closed")
        loop = asyncio.get_running_loop()
        handle = ThreadHandle(spec, loop)
        try:
            kwargs = spec.task.load_kwargs(spec.stored_kwargs)
        except TaskValidationError as exc:
            handle._resolve(
                ExecutionResult(
                    kind="invalid",
                    exception=exc,
                    error=error_dict("validation", type(exc).__name__, str(exc)),
                )
            )
            return handle
        if spec.task.context_param is not None:
            kwargs[spec.task.context_param] = spec.ctx
        while self._busy >= self._threads:
            self._freed.clear()
            await self._freed.wait()
            if self._closed:
                raise RuntimeError("thread executor is closed")
        self._busy += 1
        context = contextvars.copy_context()
        fn = spec.task.fn

        def call() -> Any:
            return context.run(fn, **kwargs)

        future = loop.run_in_executor(self._pool, call)
        self._running.add(future)
        timeout = spec.effective_timeout()
        if timeout is not None:
            handle._timer = loop.call_later(timeout, handle._on_timeout)
        future.add_done_callback(lambda f: self._returned(handle, f))
        return handle

    def _returned(self, handle: ThreadHandle, future: asyncio.Future[Any]) -> None:
        self._running.discard(future)
        self._busy -= 1
        self._freed.set()
        if handle.done:
            if not future.cancelled() and future.exception() is not None:
                logger.info(
                    "abandoned thread task %s raised after its attempt ended: %r",
                    handle.spec.task.name,
                    future.exception(),
                )
            return
        if future.cancelled():
            handle._resolve(ExecutionResult(kind="stopped", reason="shutdown"))
            return
        exc = future.exception()
        if exc is None:
            handle._resolve(ExecutionResult(kind="returned", value=future.result()))
        else:
            handle._resolve(raised_result(exc))

    async def aclose(self, *, grace: float = 30.0) -> None:
        """Wait up to ``grace`` s for running threads; threads still running are abandoned
        (Python cannot stop them) and the pool is shut down without waiting for them."""
        self._closed = True
        self._freed.set()
        if self._running:
            await asyncio.wait(set(self._running), timeout=grace)
        if self._running:
            logger.warning(
                "%s thread task(s) still running after %s s; abandoning them",
                len(self._running),
                grace,
            )
        self._pool.shutdown(wait=False, cancel_futures=True)
