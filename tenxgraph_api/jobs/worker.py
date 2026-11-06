"""The in-process worker loop (async executor).

One ``Worker`` consumes its queues, claims runs in the store, runs each attempt in its own
asyncio task and keeps writing for it only while it holds the lease:

- ``poll_once``: read up to ``free_slots`` messages, claim each run (CAS), ack (best effort),
  start it. A run whose task this worker does not know is left queued for another pod.
- ``tick``: one batched ``renew_leases`` call (heartbeat plus cancel watch), a local lease
  deadline check, then a fenced progress flush for each run whose buffer is dirty, at most
  once per ``flush_interval``. Progress hooks run on a separate task, never inside ``tick``.
- An attempt ends with one final fenced flush and one fenced ``transition``; a transition
  that raises is retried with backoff while the local lease deadline holds. A worker that
  finds itself fenced out (renew, flush or transition matched nothing) or whose lease was not
  renewed for ``lease_ttl - lease_margin`` cancels the attempt and writes nothing more for it.
- Store and transport errors are logged and retried; they never end ``run()``.

``10xgraph worker`` (Sprint 3) wraps this with config, signals, thread and process executors.
``run()`` / ``aclose()`` give a minimal background loop; ``jobs_harness`` drives ``tick``,
``poll_once`` and ``sweep_once`` by hand instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import registry
from .backoff import Backoff
from .context import TaskContext
from .control import Fail, RetryAfter
from .emit import TaskEmitter
from .errors import PayloadTooLarge, TaskValidationError
from .hooks import call_safely
from .identity import current_task_context
from .models import (
    LEASED_STATES,
    MAX_DEFERRALS,
    AttemptOutcome,
    ClaimStatus,
    HeldLease,
    RunState,
    TaskRun,
)
from .runtime import JobsRuntime
from .task import Task
from .transport.base import Delivery


logger = logging.getLogger("tenxgraph_api.jobs")

QueuesSpec = Sequence[str] | Callable[[], Sequence[str]] | None

#: Seconds before a run of a task this worker does not know is published again (rolling
#: deploys: a pod that has the task picks it up).
UNKNOWN_TASK_RETRY = 30.0
#: Pending ``on_task_progress`` calls; more are dropped with a warning.
HOOK_QUEUE_SIZE = 1000
#: Retry delays for a failed final write, and for the poll loop after a store or transport error.
WRITE_RETRY_BASE = 0.05
WRITE_RETRY_MAX = 2.0
POLL_ERROR_BASE = 0.1
POLL_ERROR_MAX = 5.0


def error_info(exc: BaseException, code: str = "error") -> dict[str, Any]:
    """The ``error`` stored on a run: code, exception type and message (no traceback)."""
    return {"code": code, "type": type(exc).__name__, "message": str(exc)}


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class _Attempt(BaseModel):
    """Worker-side state of one running attempt (mutable bookkeeping, not stored).

    Equality and hashing are by identity: attempts are kept in a set (``Worker._stale``) and
    two attempts of the same run must never compare equal.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: TaskRun
    task: Task | None
    ctx: TaskContext
    emitter: TaskEmitter
    job: asyncio.Task | None = None
    inner: asyncio.Task | None = None
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


class Worker:
    """Runs ``async`` tasks from the runtime's transport and store."""

    def __init__(  # - worker knobs
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
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
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
        #: Tests set this to False to simulate a worker whose renewals do not reach the store.
        #: The local lease deadline is still enforced (see ``check_deadlines``).
        self.heartbeats = True
        self._inflight: dict[str, _Attempt] = {}
        #: Attempts that lost their lease but whose code has not returned yet. They write
        #: nothing and still use a slot until they return.
        self._stale: set[_Attempt] = set()
        self._stopping = False
        self._slot_freed = asyncio.Event()
        self._hook_queue: asyncio.Queue | None = None
        self._hook_task: asyncio.Task | None = None
        #: Counters for tests and metrics.
        self.poll_errors = 0
        self.hooks_dropped = 0

    # ------------------------------------------------------------------ introspection

    @property
    def free_slots(self) -> int:
        return max(0, self.concurrency - len(self._inflight) - len(self._stale))

    @property
    def inflight(self) -> dict[str, _Attempt]:
        return dict(self._inflight)

    def queues(self) -> list[str]:
        spec = self._queues
        if spec is None:
            names = {t.queue for t in registry.all_tasks().values()} | {"default"}
            return sorted(names)
        if callable(spec):
            return list(spec())
        return list(spec)

    def jobs(self) -> list[asyncio.Task]:
        attempts = [*self._inflight.values(), *self._stale]
        return [a.job for a in attempts if a.job is not None]

    # ------------------------------------------------------------------ consume and claim

    async def poll_once(self, *, block: float = 0.0) -> int:
        """Read and start up to ``free_slots`` runs; returns how many started.

        A failing delivery is logged and left unacked (the transport delivers it again);
        ``poll_errors`` goes up. A failing ``consume`` raises (``run()`` backs off).
        """
        if self._stopping or self.free_slots == 0:
            return 0
        queues = self.queues()
        if not queues:
            return 0
        deliveries = await self.transport.consume(queues, max_messages=self.free_slots, block=block)
        started = 0
        for delivery in deliveries:
            try:
                if await self._handle(delivery):
                    started += 1
            except Exception:
                self.poll_errors += 1
                logger.exception(
                    "jobs worker could not handle run %s; it will be delivered again",
                    delivery.message.run_id,
                )
        return started

    async def _ack(self, delivery: Delivery) -> None:
        """Best effort: an ack that fails only means the message may come again."""
        try:
            await self.transport.ack([delivery])
        except Exception:
            logger.warning(
                "jobs ack failed for run %s (harmless: a redelivery is skipped by the claim)",
                delivery.message.run_id,
                exc_info=True,
            )

    async def _handle(self, delivery: Delivery) -> bool:
        claimed_at = self._monotonic()
        run_id = delivery.message.run_id
        current = await self.store.get_run(run_id)
        if current is None or current.terminal:
            await self._ack(delivery)
            return False
        task = registry.all_tasks().get(current.task)
        if task is None:
            await self._ack(delivery)
            await self._leave_for_another_pod(current)
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
        run = result.run
        previous = self._inflight.get(run_id)
        if previous is not None:  # our own stalled attempt: the store gave the run away
            self._lose(previous, "run was claimed again")
        self._start(run, task, lease_deadline=claimed_at + self.lease_ttl - self.lease_margin)
        return True

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
        await self.store.mark_published([run.run_id], republish_after=UNKNOWN_TASK_RETRY)

    def _start(self, run: TaskRun, task: Task, *, lease_deadline: float) -> None:
        emitter = TaskEmitter(clock=self.runtime.clock)
        ctx = TaskContext(
            run_id=run.run_id,
            attempt=run.attempt,
            task=run.task,
            queue=run.queue,
            identity=dict(run.identity),
            emit=emitter,
        )
        attempt = _Attempt(
            run=run, task=task, ctx=ctx, emitter=emitter, lease_deadline=lease_deadline
        )
        self._inflight[run.run_id] = attempt
        logger.info(
            "task run started run_id=%s task=%s attempt=%s worker=%s",
            run.run_id,
            run.task,
            run.attempt,
            self.worker_id,
        )
        attempt.job = asyncio.create_task(self._execute(attempt), name=f"task-run:{run.run_id}")

    # ------------------------------------------------------------------ execute

    async def _execute(self, attempt: _Attempt) -> None:
        token = current_task_context.set(attempt.ctx)
        try:
            await self.runtime.hook_runner.emit("on_task_start", attempt.run)
            outcome = await self._run_attempt(attempt)
            if outcome is not None and not attempt.fenced_out:
                await self._finish(attempt, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never escapes: the lease simply expires and the sweeper recovers the run.
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

    def _stopped_outcome(self, attempt: _Attempt) -> AttemptOutcome | None:
        """Outcome of an attempt stopped before its body ran."""
        if attempt.fenced_out or attempt.stop_reason in ("lost", "abort"):
            return None
        if attempt.stop_reason == "cancel":
            return AttemptOutcome(
                kind="cancelled", error={"code": "cancelled", "message": "cancelled"}
            )
        return AttemptOutcome(
            kind="requeue", error={"code": "interrupted", "message": "worker shut down"}
        )

    async def _run_attempt(self, attempt: _Attempt) -> AttemptOutcome | None:
        task = attempt.task
        if task is None:  # pragma: no cover - unknown tasks never start
            return None
        try:
            kwargs = task.load_kwargs(attempt.run.kwargs)
        except TaskValidationError as exc:
            return AttemptOutcome(kind="failed", error=error_info(exc, "validation"))
        if task.context_param is not None:
            kwargs[task.context_param] = attempt.ctx
        # A cancel, shutdown or lost lease may have arrived during on_task_start.
        if attempt.fenced_out or attempt.stop_reason is not None:
            return self._stopped_outcome(attempt)
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
            logger.exception("jobs worker could not classify run %s", attempt.run_id)
            return AttemptOutcome(kind="failed", error=error_info(exc, "worker_error"))

    def _classify(  # noqa: PLR0911 - one return per outcome
        self, attempt: _Attempt, inner: asyncio.Task
    ) -> AttemptOutcome | None:
        task = attempt.task
        assert task is not None  # noqa: S101 - narrowed by the caller
        reason = attempt.stop_reason
        if reason in ("lost", "abort"):
            return None
        if reason == "shutdown" and (attempt.timed_out or inner.cancelled()):
            return AttemptOutcome(
                kind="requeue", error={"code": "interrupted", "message": "worker shut down"}
            )
        if attempt.timed_out:
            error: dict[str, Any] = {
                "code": "timeout",
                "type": "TimeoutError",
                "message": f"attempt exceeded timeout of {task.timeout} s",
            }
            return self._failure(attempt, error)
        if reason == "cancel":
            return AttemptOutcome(
                kind="cancelled", error={"code": "cancelled", "message": "cancelled"}
            )
        if inner.cancelled():
            return self._failure(attempt, {"code": "cancelled", "message": "task was cancelled"})
        exc = inner.exception()
        if exc is None:
            return self._success(attempt, inner.result())
        if isinstance(exc, Fail):
            error = error_info(exc, "fail")
            if exc.data is not None:
                error["data"] = exc.data
            return AttemptOutcome(kind="failed", error=error)
        if isinstance(exc, RetryAfter):
            if attempt.run.defer_count >= MAX_DEFERRALS:
                error = {
                    "code": "too_many_deferrals",
                    "type": "RetryAfter",
                    "message": f"RetryAfter used more than {MAX_DEFERRALS} times",
                }
                return AttemptOutcome(kind="failed", error=error)
            return AttemptOutcome(kind="defer", delay=exc.seconds)
        logger.warning(
            "task run attempt failed run_id=%s task=%s attempt=%s",
            attempt.run_id,
            attempt.run.task,
            attempt.run.attempt,
            exc_info=exc,
        )
        return self._failure(attempt, error_info(exc))

    def _success(self, attempt: _Attempt, value: Any) -> AttemptOutcome:
        task = attempt.task
        assert task is not None  # noqa: S101 - narrowed by the caller
        try:
            data = task.dump_result(value, max_bytes=self.runtime.max_result_bytes)
        except TaskValidationError as exc:
            code = "result_too_large" if isinstance(exc, PayloadTooLarge) else "result_invalid"
            return AttemptOutcome(kind="failed", error=error_info(exc, code))
        return AttemptOutcome(kind="succeeded", result=data)

    def _failure(self, attempt: _Attempt, error: dict[str, Any]) -> AttemptOutcome:
        task = attempt.task
        assert task is not None  # noqa: S101 - narrowed by the caller
        number = attempt.run.failure_count + 1
        if number > attempt.run.max_retries:
            return AttemptOutcome(kind="failed", error=error)
        try:
            delay = float(task.backoff(number))
            if not math.isfinite(delay):
                raise ValueError(f"backoff returned {delay}")
        except Exception:
            logger.exception("backoff of task %s failed; using the default backoff", task.name)
            delay = Backoff().delay(number)
        return AttemptOutcome(kind="retry", error=error, delay=max(0.0, delay))

    async def _finish(self, attempt: _Attempt, outcome: AttemptOutcome) -> None:
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
        logger.info(
            "task run %s run_id=%s task=%s attempt=%s",
            run.state.value,
            run.run_id,
            run.task,
            attempt.run.attempt,
        )
        await self._after_transition(run, attempt.task)

    async def _write_transition(self, attempt: _Attempt, outcome: AttemptOutcome) -> TaskRun | None:
        """Write the outcome, retrying store errors while the lease deadline holds.

        Safe to repeat: the transition is a CAS on the attempt. A None after a write that
        raised may mean the earlier write landed and only its reply was lost, so the run is
        read back before giving up.
        """
        delay = WRITE_RETRY_BASE
        raised = False
        while True:
            if attempt.fenced_out:
                return None
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
            except Exception:
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
            if run is not None:
                attempt.written = True
                return run
            if raised:
                landed = await self._read_back(attempt)
                if landed is not None:
                    attempt.written = True
                    return landed
            attempt.fenced_out = True
            logger.warning(
                "task run result discarded run_id=%s attempt=%s: lease lost",
                attempt.run_id,
                attempt.run.attempt,
            )
            return None

    async def _read_back(self, attempt: _Attempt) -> TaskRun | None:
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

    async def _after_transition(self, run: TaskRun, task: Task | None) -> None:
        hooks = self.runtime.hook_runner
        if run.state == RunState.SUCCEEDED:
            await hooks.emit("on_task_success", run, run.result)
            if task is not None and task.on_success is not None:
                await call_safely(task.on_success, run, label=f"{task.name}.on_success")
        elif run.state == RunState.FAILED:
            await hooks.emit("on_task_failure", run, run.error)
            if task is not None and task.on_failure is not None:
                await call_safely(task.on_failure, run, label=f"{task.name}.on_failure")
        elif run.state == RunState.CANCELLED:
            await hooks.emit("on_task_cancel", run)
        elif run.state == RunState.RETRYING:
            await hooks.emit("on_task_retry", run, run.error)

    # ------------------------------------------------------------------ heartbeat and flush

    async def tick(self) -> None:
        """Heartbeat plus cancel watch (one batched store call), the local lease deadline
        check, then due progress flushes. Store errors are logged, never raised."""
        held = [a for a in self._inflight.values() if not a.fenced_out and not a.written]
        if self.heartbeats and held:
            sent_at = self._monotonic()
            try:
                status: dict[str, bool] | None = await self.store.renew_leases(
                    self.worker_id,
                    [HeldLease(run_id=a.run_id, attempt=a.run.attempt) for a in held],
                    lease_ttl=self.lease_ttl,
                )
            except Exception:
                logger.warning("jobs lease renewal failed; will retry", exc_info=True)
                status = None
            if status is not None:
                self._apply_renewals(held, status, sent_at)
        self.check_deadlines()
        if not self.heartbeats:
            return
        for attempt in held:
            if attempt.finished or attempt.fenced_out or not attempt.emitter.dirty:
                continue
            try:
                await self._flush(attempt)
            except Exception:
                logger.warning("progress flush of run %s failed", attempt.run_id, exc_info=True)

    def _apply_renewals(
        self, held: list[_Attempt], status: dict[str, bool], sent_at: float
    ) -> None:
        for attempt in held:
            if attempt.fenced_out or attempt.written:
                continue
            if attempt.run_id not in status:
                if not attempt.finished:
                    self._lose(attempt, "lease renewal matched nothing")
                # A finishing attempt's outcome write may have landed: its write loop decides
                # (CAS plus read-back), and its body is not running any more.
                continue
            attempt.lease_deadline = sent_at + self.lease_ttl - self.lease_margin
            if status[attempt.run_id] and attempt.stop_reason is None and not attempt.finished:
                self._stop(attempt, "cancel")

    def check_deadlines(self) -> None:
        """Stop every attempt whose lease was not renewed for ``lease_ttl - lease_margin``
        (the store may already give it to another worker). Sync and store-free."""
        now = self._monotonic()
        for attempt in list(self._inflight.values()):
            if not attempt.fenced_out and not attempt.written and now >= attempt.lease_deadline:
                self._lose(attempt, "no lease renewal within lease_ttl")

    async def _flush(self, attempt: _Attempt, *, force: bool = False) -> bool:
        """Write buffered progress (fenced). False when the lease is gone."""
        async with attempt.flush_lock:
            if attempt.fenced_out:
                return False
            now = self._monotonic()
            if now >= attempt.lease_deadline:
                self._lose(attempt, "lease deadline passed before a progress flush")
                return False
            if not force and now - attempt.last_flush < self.flush_interval:
                return True
            taken = attempt.emitter.take()
            if taken is None:
                return True
            events, snapshot = taken
            attempt.last_flush = now
            ok = await self.store.append_events(
                attempt.run_id,
                attempt=attempt.run.attempt,
                worker_id=self.worker_id,
                events=events,
                progress=snapshot,
            )
            if not ok:
                self._lose(attempt, "progress flush matched nothing")
                return False
        for event in events:
            self._dispatch_progress(
                attempt.run,
                {"type": event.type, "at": event.at, "attempt": attempt.run.attempt, **event.data},
            )
        return True

    # ------------------------------------------------------------------ progress hooks

    def _dispatch_progress(self, run: TaskRun, event: dict[str, Any]) -> None:
        """Queue an ``on_task_progress`` call; one task runs them, off the tick path."""
        if not self.runtime.hook_runner.hooks:
            return
        if self._hook_queue is None:
            self._hook_queue = asyncio.Queue(maxsize=HOOK_QUEUE_SIZE)
        if self._hook_task is None or self._hook_task.done():
            self._hook_task = asyncio.create_task(self._hook_loop(self._hook_queue))
        try:
            self._hook_queue.put_nowait((run, event))
        except asyncio.QueueFull:
            self.hooks_dropped += 1
            if self.hooks_dropped == 1 or self.hooks_dropped % 1000 == 0:
                logger.warning(
                    "on_task_progress hooks are too slow: %s events dropped", self.hooks_dropped
                )

    async def _hook_loop(self, queue: asyncio.Queue) -> None:
        while True:
            run, event = await queue.get()
            try:
                await self.runtime.hook_runner.emit("on_task_progress", run, event)
            finally:
                queue.task_done()

    async def hooks_idle(self, timeout: float | None = None) -> None:
        """Wait until every queued progress hook has run."""
        if self._hook_queue is not None and self._hook_task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._hook_queue.join(), timeout=timeout)

    async def _close_hooks(self, timeout: float = 5.0) -> None:
        await self.hooks_idle(timeout)
        if self._hook_task is not None:
            self._hook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hook_task
            self._hook_task = None

    # ------------------------------------------------------------------ stop and lose

    def _stop(self, attempt: _Attempt, reason: str) -> None:
        if attempt.stop_reason is None:
            attempt.stop_reason = reason
        attempt.ctx._set_cancelled()
        if attempt.inner is not None and not attempt.inner.done():
            attempt.inner.cancel()

    def _lose(self, attempt: _Attempt, why: str) -> None:
        if attempt.fenced_out:
            return
        attempt.fenced_out = True
        attempt.stop_reason = "lost"
        if self._inflight.get(attempt.run_id) is attempt:
            del self._inflight[attempt.run_id]
            if attempt.job is not None and not attempt.job.done():
                self._stale.add(attempt)
        logger.warning(
            "task run lease lost run_id=%s attempt=%s worker=%s: %s; stopping it",
            attempt.run_id,
            attempt.run.attempt,
            self.worker_id,
            why,
        )
        self._stop(attempt, "lost")

    # ------------------------------------------------------------------ loop and shutdown

    async def run(self, *, block: float = 1.0, tick_interval: float | None = None) -> None:
        """Consume until ``aclose()``. Heartbeat runs every ``min(1, lease_ttl / 3)`` s.

        After ``aclose()`` it keeps the heartbeat, cancel watch and flushes going until every
        attempt has written its outcome, then stops the ticker last.
        """
        interval = tick_interval or min(1.0, self.lease_ttl / 3)
        ticker = asyncio.create_task(self._tick_loop(interval))
        backoff = POLL_ERROR_BASE
        try:
            while not self._stopping:
                if self.free_slots == 0:
                    self._slot_freed.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._slot_freed.wait(), timeout=block)
                    continue
                errors = self.poll_errors
                try:
                    await self.poll_once(block=block)
                except Exception:
                    self.poll_errors += 1
                    logger.exception("jobs worker poll failed; backing off %.1f s", backoff)
                if self.poll_errors != errors:
                    await asyncio.sleep(backoff)
                    backoff = min(POLL_ERROR_MAX, backoff * 2)
                else:
                    backoff = POLL_ERROR_BASE
            while jobs := self.jobs():
                await asyncio.wait(jobs)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            await self._close_hooks()

    async def _tick_loop(self, interval: float) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("jobs worker tick failed; retrying")
            await asyncio.sleep(interval)

    async def aclose(self, *, grace: float = 30.0) -> None:
        """Stop reading, wait ``grace`` s for running attempts, then stop the rest and put
        them back to ``queued`` without using a retry.

        Leases are renewed meanwhile by ``run()``'s ticker (still running until every attempt
        has written); a worker driven by hand must keep calling ``tick()``.
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
        await self.hooks_idle(timeout=5.0)

    async def abort(self) -> None:
        """Stop at once and write nothing (what a crash looks like to the store). For tests
        and for the harness teardown."""
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
        if self._hook_task is not None:
            self._hook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hook_task
            self._hook_task = None
            self._hook_queue = None

    def resume(self) -> None:
        """Accept work again after ``abort`` (the same worker id, like a restarted pod)."""
        self._stopping = False
