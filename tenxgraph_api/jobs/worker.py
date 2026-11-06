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

Queues and priority: the worker reads only ``queues`` (``--queue``). ``reserve`` (``--reserve
high=2``) keeps slots for a priority and the levels above it: with ``concurrency=16`` and
``{"high": 2}``, ``normal`` and ``low`` runs use at most 14 slots and the last 2 take ``high``
runs only. ``poll_once`` reads each tier for the slots it may use. When the transport's
``consume`` takes ``priorities=`` the read for reserved slots asks for those levels only;
otherwise it reads one message at a time, and a run of a lower level is given back
(``_release``: acked, published again by the sweeper after ``RELEASE_RETRY`` s) with a
``RELEASE_PAUSE`` before the next read. A run whose ``thread`` / ``process`` executor has no
free slot is given back the same way.

``10xgraph worker`` (``jobs/service.py``) wraps this with config, signals, the sweeper loop and
health probes. ``run()`` / ``aclose()`` give a minimal background loop; ``jobs_harness``
drives ``tick``, ``poll_once`` and ``sweep_once`` by hand instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import registry
from .backoff import Backoff
from .context import TaskContext
from .control import Fail, RetryAfter
from .emit import TaskEmitter
from .errors import PayloadTooLarge, TaskValidationError, ValueNotStorable
from .hooks import call_safely
from .identity import current_task_context
from .logs import log_run
from .models import (
    LEASED_STATES,
    MAX_DEFERRALS,
    PRIORITIES,
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
#: A run read without a slot for it (reserved slots, a full executor) is published again after
#: this many seconds, and the worker pauses reading this long after giving one back.
RELEASE_RETRY = 2.0
RELEASE_PAUSE = 1.0
#: Pending ``on_task_progress`` calls; more are dropped with a warning.
HOOK_QUEUE_SIZE = 1000
#: Retry delays for a failed final write, and for the poll loop after a store or transport error.
WRITE_RETRY_BASE = 0.05
WRITE_RETRY_MAX = 2.0
POLL_ERROR_BASE = 0.1
POLL_ERROR_MAX = 5.0


#: The error written when the store refuses a result or error value (``ValueNotStorable``).
NOT_SERIALIZABLE_ERROR: dict[str, Any] = {
    "code": "result_not_serializable",
    "type": "ValueNotStorable",
    "message": "the task result or error holds a value the run store cannot store "
    "(a NUL character, NaN or Infinity)",
}


def _not_storable(exc: BaseException) -> bool:
    """Whether ``exc`` is the store's ``ValueNotStorable`` (NUL, NaN or Infinity in a value)."""
    return isinstance(exc, ValueNotStorable)


def _parameters(fn: Callable[..., Any]) -> set[str]:
    try:
        return set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return set()


def check_reserve(reserve: Mapping[str, int] | None, concurrency: int) -> dict[str, int]:
    """Validate ``--reserve``: known levels above ``low``, non-negative, and fewer reserved
    slots than ``concurrency`` so the lowest level keeps at least one."""
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


class Worker:
    """Runs ``async`` tasks from the runtime's transport and store."""

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
        graph: Any = None,
        executors: Mapping[str, Any] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.reserve = check_reserve(reserve, concurrency)
        #: The loaded ``CompiledGraph`` for ``ctx.invoke_graph`` (None: not available).
        self.graph = graph
        #: Non-async executors by name (``"thread"``, ``"process"``); see ``_call``.
        self.executors: dict[str, Any] = dict(executors or {})
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
        #: Worker-monotonic time of the last finished tick (``/healthz``).
        self.last_tick: float | None = None
        #: Whether ``transport.consume`` takes ``priorities=`` (a read limited to some levels).
        #: Without it, a read for reserved slots takes one message at a time, and a message
        #: that may not use them waits here (unacked) until a slot it may use frees.
        self._filtered_reads = "priorities" in _parameters(self.transport.consume)
        #: Runs the last ``poll_once`` read but gave back (see ``_release``).
        self.released = 0

    # ------------------------------------------------------------------ introspection

    @property
    def free_slots(self) -> int:
        return max(0, self.concurrency - len(self._inflight) - len(self._stale))

    @property
    def stopping(self) -> bool:
        return self._stopping

    def slot_limit(self, priority: str) -> int:
        """Slots runs of ``priority`` may use: ``concurrency`` minus the slots reserved for
        higher levels."""
        rank = PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES) - 1
        return self.concurrency - sum(self.reserve.get(p, 0) for p in PRIORITIES[:rank])

    def capacity(self, priority: str) -> int:
        """Slots still free for a run of ``priority``."""
        used = len(self._inflight) + len(self._stale)
        return max(0, self.slot_limit(priority) - used)

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
        started = 0
        self.released = 0
        waited = False
        previous_limit: int | None = None
        # Tiers from the lowest level up: a tier reads its level and every level above it,
        # for the slots that level may use. Without reservations every limit is the same, so
        # this is one read over all priorities.
        for index in range(len(PRIORITIES) - 1, -1, -1):
            level = PRIORITIES[index]
            limit = self.slot_limit(level)
            if previous_limit is not None and limit == previous_limit:
                continue
            previous_limit = limit
            room = self.capacity(level)
            if room <= 0 or self._stopping:
                continue
            levels = PRIORITIES[: index + 1]
            options: dict[str, Any] = {}
            if len(levels) < len(PRIORITIES):
                if self._filtered_reads:
                    options["priorities"] = list(levels)
                else:
                    room = 1  # one message at a time; a lower level is released (_release)
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
        if task is None or not self._runs_here(task):
            await self._ack(delivery)
            await self._leave_for_another_pod(current)
            return False
        if self.capacity(current.priority) <= 0 or self._executor_full(task):
            # Only reserved slots are free and this run may not use them, or its executor has
            # no free slot: give it back for a worker (or a moment) with room.
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
        run = result.run
        previous = self._inflight.get(run_id)
        if previous is not None:  # our own stalled attempt: the store gave the run away
            self._lose(previous, "run was claimed again")
        self._start(run, task, lease_deadline=claimed_at + self.lease_ttl - self.lease_margin)
        return True

    async def _release(self, delivery: Delivery, run: TaskRun) -> None:
        """Give back a run this worker read but has no slot for: ack the message and have the
        sweeper publish the run again after ``RELEASE_RETRY`` seconds (CAS on the
        ``next_publish_at`` read, so a newer publish is never pushed back). ``run()`` then
        waits up to ``RELEASE_PAUSE`` before reading again, so a backlog is not cycled."""
        self.released += 1
        await self._ack(delivery)
        await self.store.mark_published(
            {run.run_id: run.next_publish_at}, republish_after=RELEASE_RETRY
        )
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
        await self.store.mark_published(
            {run.run_id: run.next_publish_at}, republish_after=UNKNOWN_TASK_RETRY
        )

    def _start(self, run: TaskRun, task: Task, *, lease_deadline: float) -> None:
        emitter = TaskEmitter(clock=self.runtime.clock)
        ctx = TaskContext(
            run_id=run.run_id,
            attempt=run.attempt,
            task=run.task,
            queue=run.queue,
            identity=dict(run.identity),
            emit=emitter,
            graph=self.graph,
        )
        attempt = _Attempt(
            run=run, task=task, ctx=ctx, emitter=emitter, lease_deadline=lease_deadline
        )
        self._inflight[run.run_id] = attempt
        log_run(logger, logging.INFO, "started", run, worker=self.worker_id, queue=run.queue)
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

    async def _run_attempt(self, attempt: _Attempt) -> AttemptOutcome | None:  # noqa: PLR0911
        task = attempt.task
        if task is None:  # pragma: no cover - unknown tasks never start
            return None
        if task.executor != "async":
            # A cancel, shutdown or lost lease may have arrived during on_task_start.
            if attempt.fenced_out or attempt.stop_reason is not None:
                return self._stopped_outcome(attempt)
            return await self._run_in_executor(attempt, task)
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

    # ------------------------------------------------------------------ thread / process

    def executor_for(self, task: Task) -> Any:
        """The executor of a ``thread`` / ``process`` task (None: not run by this worker);
        ``async`` tasks return None too and run on the loop."""
        if task.executor == "async":
            return None
        return self.executors.get(task.executor)

    def _runs_here(self, task: Task) -> bool:
        return task.executor == "async" or task.executor in self.executors

    def _executor_full(self, task: Task) -> bool:
        executor = self.executor_for(task)
        return executor is not None and executor.free_slots <= 0

    async def _run_in_executor(self, attempt: _Attempt, task: Task) -> AttemptOutcome | None:
        """Run a ``thread`` / ``process`` body in its executor and map how it ended."""
        from .executors import ExecutionSpec

        executor = self.executors[task.executor]
        spec = ExecutionSpec(
            task=task,
            stored_kwargs=attempt.run.kwargs,
            ctx=attempt.ctx,
            max_result_bytes=self.runtime.max_result_bytes,
        )
        try:
            handle = await executor.start(spec)
        except RuntimeError as exc:  # the executor is closing (shutdown)
            if attempt.stop_reason in ("lost", "abort") or attempt.fenced_out:
                return None
            return AttemptOutcome(
                kind="requeue", error={"code": "interrupted", "message": str(exc)}
            )
        attempt.handle = handle
        # A stop may have arrived while waiting for a slot.
        if attempt.stop_reason is not None:
            self._signal_handle(attempt, attempt.stop_reason)
        try:
            result = await handle.result()
        except asyncio.CancelledError:
            handle.kill("abort")
            raise
        try:
            return self._classify_execution(attempt, result)
        except Exception as exc:
            logger.exception("jobs worker could not classify run %s", attempt.run_id)
            return AttemptOutcome(kind="failed", error=error_info(exc, "worker_error"))

    @staticmethod
    def _signal_handle(attempt: _Attempt, reason: str) -> None:
        handle = attempt.handle
        if handle is None:
            return
        if reason == "cancel":
            handle.request_cancel()
        else:
            handle.kill(reason)

    def _classify_execution(  # noqa: PLR0911 - one return per outcome
        self, attempt: _Attempt, result: Any
    ) -> AttemptOutcome | None:
        task = attempt.task
        assert task is not None  # noqa: S101 - narrowed by the caller
        reason = attempt.stop_reason
        if reason in ("lost", "abort") or attempt.fenced_out:
            return None
        kind = result.kind
        if reason == "shutdown" and kind != "returned" and kind != "raised":
            return AttemptOutcome(
                kind="requeue", error={"code": "interrupted", "message": "worker shut down"}
            )
        if reason == "cancel":
            return AttemptOutcome(
                kind="cancelled", error={"code": "cancelled", "message": "cancelled"}
            )
        error = result.error or {"code": kind, "message": kind}
        if kind == "returned":
            if result.value_is_json:
                return AttemptOutcome(kind="succeeded", result=result.value)
            return self._success(attempt, result.value)
        if kind == "raised":
            exc = result.exception
            if isinstance(exc, Fail):
                return AttemptOutcome(kind="failed", error=error)
            if isinstance(exc, RetryAfter):
                return self._defer(attempt, exc)
            log_run(logger, logging.WARNING, "attempt failed", attempt.run, error=error.get("type"))
            return self._failure(attempt, error)
        if kind == "invalid":
            return AttemptOutcome(kind="failed", error=error)
        if kind in ("timeout", "crashed"):
            log_run(
                logger, logging.WARNING, f"attempt {kind}", attempt.run, error=error.get("code")
            )
            return self._failure(attempt, error)
        # "stopped" without a stop of our own: treat it as a failed attempt.
        return self._failure(attempt, {"code": "stopped", "message": "the attempt was stopped"})

    def _defer(self, attempt: _Attempt, exc: RetryAfter) -> AttemptOutcome:
        if attempt.run.defer_count >= MAX_DEFERRALS:
            error = {
                "code": "too_many_deferrals",
                "type": "RetryAfter",
                "message": f"RetryAfter used more than {MAX_DEFERRALS} times",
            }
            return AttemptOutcome(kind="failed", error=error)
        return AttemptOutcome(kind="defer", delay=exc.seconds)

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
            return self._defer(attempt, exc)
        log_run(logger, logging.WARNING, "attempt failed", attempt.run, exc_info=exc)
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
        log_run(
            logger,
            logging.INFO,
            run.state.value,
            run,
            attempt=attempt.run.attempt,
            error=(run.error or {}).get("code"),
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
            except Exception as exc:
                if _not_storable(exc) and outcome.error != NOT_SERIALIZABLE_ERROR:
                    # The result or error holds a value the store refuses (NUL, NaN,
                    # Infinity): retrying cannot help, so the run fails with a generic error
                    # that does not echo the value.
                    log_run(logger, logging.WARNING, "result not storable", attempt.run)
                    outcome = AttemptOutcome(kind="failed", error=dict(NOT_SERIALIZABLE_ERROR))
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
            if run is not None:
                attempt.written = True
                return run
            if raised:
                landed = await self._read_back(attempt)
                if landed is not None:
                    attempt.written = True
                    return landed
            attempt.fenced_out = True
            log_run(logger, logging.WARNING, "result discarded (lease lost)", attempt.run)
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
        self._signal_handle(attempt, reason)

    def _lose(self, attempt: _Attempt, why: str) -> None:
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
                started = 0
                try:
                    started = await self.poll_once(block=block)
                except Exception:
                    self.poll_errors += 1
                    logger.exception("jobs worker poll failed; backing off %.1f s", backoff)
                if self.poll_errors != errors:
                    await asyncio.sleep(backoff)
                    backoff = min(POLL_ERROR_MAX, backoff * 2)
                else:
                    backoff = POLL_ERROR_BASE
                    if self.released and not started:
                        self._slot_freed.clear()
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._slot_freed.wait(), RELEASE_PAUSE)
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
            else:
                self.last_tick = self._monotonic()
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
