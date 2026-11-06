"""One attempt: run the body (on the loop or in an executor), then write its outcome.

``AttemptRunner`` is a mixin of ``Worker``; it owns the attempt's lifecycle from start to the
final fenced ``transition``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from tenxgraph_api.jobs.core.context import TaskContext
from tenxgraph_api.jobs.core.emit import TaskEmitter
from tenxgraph_api.jobs.core.hooks import call_safely
from tenxgraph_api.jobs.core.identity import current_task_context
from tenxgraph_api.jobs.core.models import LEASED_STATES, AttemptOutcome, RunState, TaskRun
from tenxgraph_api.jobs.core.runtime import JobsRuntime
from tenxgraph_api.jobs.core.task import Task
from tenxgraph_api.jobs.errors import TaskValidationError, ValueNotStorable
from tenxgraph_api.jobs.store.base import BaseRunStore

from . import outcome as oc
from .logs import log_run


logger = logging.getLogger("tenxgraph_api.jobs")

#: Retry delays for a failed final write.
WRITE_RETRY_BASE = 0.05
WRITE_RETRY_MAX = 2.0


class Attempt(BaseModel):
    """Worker-side state of one running attempt (not stored). Compared by identity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: TaskRun
    task: Task
    ctx: TaskContext
    emitter: TaskEmitter
    job: asyncio.Task | None = None
    inner: asyncio.Task | None = None
    #: The executor's ``ExecutionHandle`` of a ``thread`` / ``process`` attempt.
    handle: Any = None
    #: Why the worker stops the attempt: "cancel", "lost", "shutdown" or "abort".
    stop_reason: str | None = None
    fenced_out: bool = False
    #: The body returned; the outcome is being written.
    finished: bool = False
    #: The final transition was written (or given up): nothing more to renew or write.
    written: bool = False
    timed_out: bool = False
    #: Worker-monotonic time after which the attempt stops unless a renewal succeeds.
    lease_deadline: float = math.inf
    last_flush: float = -math.inf
    flush_lock: asyncio.Lock = Field(default_factory=asyncio.Lock)

    __eq__ = object.__eq__
    __hash__ = object.__hash__

    @property
    def run_id(self) -> str:
        return self.run.run_id


class AttemptRunner:
    """Mixin of ``Worker``: runs attempts and writes their outcomes."""

    runtime: JobsRuntime
    store: BaseRunStore
    worker_id: str
    executors: dict[str, Any]
    _monotonic: Callable[[], float]
    _inflight: dict[str, Attempt]
    _stale: set[Attempt]
    _slot_freed: asyncio.Event

    if TYPE_CHECKING:

        async def _flush(self, attempt: Attempt, *, force: bool = False) -> bool: ...

    # ------------------------------------------------------------------ run the body

    async def _execute(self, attempt: Attempt) -> None:
        """The attempt's asyncio task: hooks, body, outcome; never raises but cancel."""
        token = current_task_context.set(attempt.ctx)
        try:
            await self.runtime.hook_runner.emit("on_task_start", attempt.run)
            outcome = await self._run_body(attempt)
            if outcome is not None and not attempt.fenced_out:
                await self._finish(attempt, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The lease simply expires and the sweeper recovers the run.
            logger.exception(
                "jobs worker error in run_id=%s attempt=%s", attempt.run_id, attempt.run.attempt
            )
        finally:
            attempt.written = True
            current_task_context.reset(token)
            attempt.emitter.close()
            if self._inflight.get(attempt.run_id) is attempt:
                del self._inflight[attempt.run_id]
            self._stale.discard(attempt)
            self._slot_freed.set()

    async def _run_body(self, attempt: Attempt) -> AttemptOutcome | None:
        task = attempt.task
        kwargs: dict[str, Any] = {}
        if task.executor == "async":
            try:
                kwargs = task.load_kwargs(attempt.run.kwargs)
            except TaskValidationError as exc:
                return oc.failed(oc.error_info(exc, "validation"))
            if task.context_param is not None:
                kwargs[task.context_param] = attempt.ctx
        # A cancel, shutdown or lost lease may have arrived during on_task_start.
        if attempt.fenced_out or attempt.stop_reason is not None:
            return oc.stopped_before_body(attempt)
        if task.executor == "async":
            return await self._run_async(attempt, task, kwargs)
        return await self._run_in_executor(attempt, task)

    async def _run_async(
        self, attempt: Attempt, task: Task, kwargs: dict[str, Any]
    ) -> AttemptOutcome | None:
        """Run an ``async`` body on the loop with the task timeout."""
        inner = asyncio.create_task(task.fn(**kwargs), name=f"task-fn:{attempt.run_id}")
        attempt.inner = inner
        try:
            done, _ = await asyncio.wait({inner}, timeout=task.timeout)
            if not done:
                attempt.timed_out = True
                attempt.ctx._set_cancelled()
                inner.cancel()
                await asyncio.wait({inner})
        except asyncio.CancelledError:
            inner.cancel()
            with contextlib.suppress(BaseException):
                await inner
            raise
        try:
            return self._classify(attempt, inner)
        except Exception as exc:
            return self._classify_failed(attempt, exc)

    def _classify(self, attempt: Attempt, inner: asyncio.Task) -> AttemptOutcome | None:
        return oc.classify_async(attempt, inner, self.runtime.max_result_bytes)

    @staticmethod
    def _classify_failed(attempt: Attempt, exc: Exception) -> AttemptOutcome:
        logger.exception("jobs worker could not classify run %s", attempt.run_id)
        return oc.failed(oc.error_info(exc, "worker_error"))

    async def _run_in_executor(self, attempt: Attempt, task: Task) -> AttemptOutcome | None:
        """Run a ``thread`` / ``process`` body in its executor."""
        from tenxgraph_api.jobs.executors import ExecutionSpec

        spec = ExecutionSpec(
            task=task,
            stored_kwargs=attempt.run.kwargs,
            ctx=attempt.ctx,
            max_result_bytes=self.runtime.max_result_bytes,
        )
        try:
            handle = await self.executors[task.executor].start(spec)
        except RuntimeError as exc:  # the executor is closing (shutdown)
            if attempt.stop_reason in ("lost", "abort") or attempt.fenced_out:
                return None
            return oc.interrupted(str(exc))
        attempt.handle = handle
        if attempt.stop_reason is not None:  # arrived while waiting for a slot
            self._stop(attempt, attempt.stop_reason)
        try:
            result = await handle.result()
        except asyncio.CancelledError:
            handle.kill("abort")
            raise
        try:
            return oc.classify_execution(attempt, result, self.runtime.max_result_bytes)
        except Exception as exc:
            return self._classify_failed(attempt, exc)

    # ------------------------------------------------------------------ write the outcome

    async def _finish(self, attempt: Attempt, outcome: AttemptOutcome) -> None:
        """Final progress flush, the fenced transition, then the outcome hooks."""
        attempt.finished = True
        try:
            if not await self._flush(attempt, force=True):
                return
        except Exception:
            logger.warning(
                "final progress flush failed for run %s; writing the result anyway",
                attempt.run_id,
                exc_info=True,
            )
        run = await self._write_transition(attempt, outcome)
        if run is None:
            return
        log_run(
            logger,
            logging.INFO,
            run.state.value,
            run,
            attempt=attempt.run.attempt,
            error=(run.error or {}).get("code"),
        )
        await self._after_transition(run, attempt.task)

    async def _write_transition(self, attempt: Attempt, outcome: AttemptOutcome) -> TaskRun | None:
        """Write the outcome (CAS on the attempt), retrying store errors while the lease
        deadline holds; after a write that raised, a None is checked by reading back."""
        delay = WRITE_RETRY_BASE
        raised = False
        while not attempt.fenced_out:
            if self._monotonic() >= attempt.lease_deadline:
                self._lose(attempt, "lease deadline passed while writing the outcome")
                return None
            try:
                run = await self.store.transition(
                    attempt.run_id,
                    attempt=attempt.run.attempt,
                    worker_id=self.worker_id,
                    outcome=outcome,
                )
            except Exception as exc:
                if isinstance(exc, ValueNotStorable) and outcome.error != oc.NOT_SERIALIZABLE_ERROR:
                    # Retrying cannot help: fail with an error that does not echo the value.
                    log_run(logger, logging.WARNING, "result not storable", attempt.run)
                    outcome = oc.failed(dict(oc.NOT_SERIALIZABLE_ERROR))
                    continue
                raised = True
                logger.warning(
                    "writing the outcome of run %s failed; retrying in %.2f s",
                    attempt.run_id,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                delay = min(WRITE_RETRY_MAX, delay * 2)
                continue
            if run is None and raised:
                run = await self._read_back(attempt)
            if run is not None:
                attempt.written = True
                return run
            attempt.fenced_out = True
            log_run(logger, logging.WARNING, "result discarded (lease lost)", attempt.run)
            return None
        return None

    async def _read_back(self, attempt: Attempt) -> TaskRun | None:
        """The run, if an earlier transition of this attempt landed."""
        try:
            current = await self.store.get_run(attempt.run_id)
        except Exception:
            return None
        if (
            current is not None
            and current.attempt == attempt.run.attempt
            and current.state not in LEASED_STATES
            and current.worker_id is None
        ):
            return current
        return None

    async def _after_transition(self, run: TaskRun, task: Task) -> None:
        hooks = self.runtime.hook_runner
        if run.state == RunState.SUCCEEDED:
            await hooks.emit("on_task_success", run, run.result)
            if task.on_success is not None:
                await call_safely(task.on_success, run, label=f"{task.name}.on_success")
        elif run.state == RunState.FAILED:
            await hooks.emit("on_task_failure", run, run.error)
            if task.on_failure is not None:
                await call_safely(task.on_failure, run, label=f"{task.name}.on_failure")
        elif run.state == RunState.CANCELLED:
            await hooks.emit("on_task_cancel", run)
        elif run.state == RunState.RETRYING:
            await hooks.emit("on_task_retry", run, run.error)

    # ------------------------------------------------------------------ stop and lose

    def _stop(self, attempt: Attempt, reason: str) -> None:
        """Ask the body to stop: cancel flag, then cancel (async) or signal the executor."""
        if attempt.stop_reason is None:
            attempt.stop_reason = reason
        attempt.ctx._set_cancelled()
        if attempt.inner is not None and not attempt.inner.done():
            attempt.inner.cancel()
        if attempt.handle is not None:
            if reason == "cancel":
                attempt.handle.request_cancel()
            else:
                attempt.handle.kill(reason)

    def _lose(self, attempt: Attempt, why: str) -> None:
        """The lease is gone: stop the body, write nothing more, keep its slot until it returns."""
        if attempt.fenced_out:
            return
        attempt.fenced_out = True
        attempt.stop_reason = "lost"
        if self._inflight.get(attempt.run_id) is attempt:
            del self._inflight[attempt.run_id]
            if attempt.job is not None and not attempt.job.done():
                self._stale.add(attempt)
        log_run(
            logger, logging.WARNING, "lease lost", attempt.run, worker=self.worker_id, why=repr(why)
        )
        self._stop(attempt, "lost")
