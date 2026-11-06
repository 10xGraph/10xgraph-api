"""In-memory run store for tests and single-process use.

Every method does its whole read-check-write without an ``await`` in between, so each call is
atomic for every coroutine on the one event loop that uses the store. Not shared between
processes and not thread-safe.
"""

from __future__ import annotations

import base64
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence

from tenxgraph_api.jobs.clock import SystemClock
from tenxgraph_api.jobs.models import (
    CAP_WAIT_BASE,
    CAP_WAIT_MAX,
    CLAIMABLE_STATES,
    EVENT_LOG_CAP,
    LEASED_STATES,
    PRIORITIES,
    REPUBLISH_AFTER,
    TERMINAL_STATES,
    AttemptOutcome,
    CancelResult,
    CancelStatus,
    ClaimResult,
    ClaimStatus,
    CreateResult,
    EventIn,
    HeldLease,
    KeyMode,
    LostResult,
    NewRun,
    RunFilter,
    RunPage,
    RunState,
    TaskEvent,
    TaskRun,
)

from .base import BaseRunStore, slot_name


MAX_PAGE = 1000

KeyTuple = tuple[str, str | None, str | None, str]


class MemoryRunStore(BaseRunStore):
    """Run store held in dicts. ``clock`` is any zero-argument callable returning epoch seconds
    (``FakeClock`` in the harness)."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or SystemClock()
        self._runs: dict[str, TaskRun] = {}
        self._keys: dict[KeyTuple, str] = {}
        self._events: dict[str, deque[TaskEvent]] = {}
        self._seq: dict[str, int] = {}
        self._counters: dict[str, int] = {}
        self._closed = False

    # ------------------------------------------------------------------ helpers

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _key_of(run: TaskRun | NewRun) -> KeyTuple | None:
        if run.idempotency_key is None:
            return None
        return (run.task, run.owner_user_id, run.tenant, run.idempotency_key)

    def _fenced(self, run_id: str, attempt: int, worker_id: str) -> TaskRun | None:
        run = self._runs.get(run_id)
        if (
            run is None
            or run.state not in LEASED_STATES
            or run.attempt != attempt
            or run.worker_id != worker_id
        ):
            return None
        return run

    def _release_slot(self, run: TaskRun) -> None:
        if run.slot is not None:
            self._counters[run.slot] = max(0, self._counters.get(run.slot, 0) - 1)
            run.slot = None

    def _finish(self, run: TaskRun, state: RunState, now: float) -> None:
        run.state = state
        run.next_publish_at = None
        run.finished_at = now
        run.expires_at = None if run.retention is None else now + run.retention

    @staticmethod
    def _clear_lease(run: TaskRun) -> None:
        run.worker_id = None
        run.lease_expires_at = None

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        self._closed = True

    async def now(self) -> float:
        return self._now()

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        now = self._now()
        key = self._key_of(new)
        if key is not None and key in self._keys:
            existing = self._runs[self._keys[key]]
            if key_mode == "replace" and existing.state == RunState.QUEUED:
                existing.kwargs = dict(new.kwargs)
                existing.concurrency_key = new.concurrency_key
                existing.updated_at = now
                return CreateResult(
                    run=existing.model_copy(deep=True),
                    created=False,
                    replaced=True,
                    due=existing.available_at <= now,
                )
            return CreateResult(run=existing.model_copy(deep=True), created=False)
        if new.run_id in self._runs:
            raise ValueError(f"run id {new.run_id} already exists")
        available_at = new.at if new.at is not None else now + (new.delay or 0.0)
        data = new.model_dump(exclude={"delay", "at"})
        run = TaskRun(
            **data,
            state=RunState.QUEUED,
            available_at=available_at,
            next_publish_at=available_at,
            enqueued_at=now,
            updated_at=now,
        )
        self._runs[run.run_id] = run
        if key is not None:
            self._keys[key] = run.run_id
        return CreateResult(run=run.model_copy(deep=True), created=True, due=available_at <= now)

    async def get_run(self, run_id: str) -> TaskRun | None:
        run = self._runs.get(run_id)
        return run.model_copy(deep=True) if run else None

    async def list_runs(
        self,
        filters: RunFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        limit = max(1, min(MAX_PAGE, limit))
        f = filters or RunFilter()
        states: set[str] | None = None
        if f.state is not None:
            raw = f.state if isinstance(f.state, tuple) else (f.state,)
            states = {str(s) for s in raw}
        rows = [
            r
            for r in self._runs.values()
            if (f.task is None or r.task == f.task)
            and (states is None or r.state.value in states)
            and (f.queue is None or r.queue == f.queue)
            and (f.owner_user_id is None or r.owner_user_id == f.owner_user_id)
        ]
        rows.sort(key=lambda r: (r.enqueued_at, r.run_id), reverse=True)
        if cursor:
            after = _decode_cursor(cursor)
            rows = [r for r in rows if (r.enqueued_at, r.run_id) < after]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = _encode_cursor(last.enqueued_at, last.run_id)
        return RunPage(items=[r.model_copy(deep=True) for r in page], next_cursor=next_cursor)

    # ------------------------------------------------------------------ worker writes

    async def claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limit: int | None = None,
    ) -> ClaimResult:
        now = self._now()
        run = self._runs.get(run_id)
        if run is None:
            return ClaimResult(status=ClaimStatus.NOT_FOUND)
        if run.state not in CLAIMABLE_STATES or run.available_at > now:
            return ClaimResult(status=ClaimStatus.NOT_READY, run=run.model_copy(deep=True))
        slot = None
        if concurrency_limit is not None:
            slot = slot_name(run.task, run.concurrency_key)
            if self._counters.get(slot, 0) >= concurrency_limit:
                run.available_at = now + min(CAP_WAIT_MAX, CAP_WAIT_BASE * 2**run.cap_waits)
                run.cap_waits += 1
                run.next_publish_at = run.available_at
                run.updated_at = now
                return ClaimResult(status=ClaimStatus.CAPPED, run=run.model_copy(deep=True))
        self._take(run, now, worker_id=worker_id, lease_ttl=lease_ttl, slot=slot)
        return ClaimResult(status=ClaimStatus.CLAIMED, run=run.model_copy(deep=True))

    def _take(
        self, run: TaskRun, now: float, *, worker_id: str, lease_ttl: float, slot: str | None
    ) -> None:
        if slot is not None:
            self._counters[slot] = self._counters.get(slot, 0) + 1
        run.state = RunState.RUNNING
        run.attempt += 1
        run.worker_id = worker_id
        run.lease_expires_at = now + lease_ttl
        run.started_at = now
        run.cancel_requested = False
        run.error = None
        run.progress = {}
        run.cap_waits = 0
        run.slot = slot
        run.next_publish_at = None
        run.updated_at = now

    async def claim_next(
        self,
        queues: Sequence[str],
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limits: Mapping[str, int] | None = None,
    ) -> TaskRun | None:
        now = self._now()
        wanted = set(queues)
        limits = concurrency_limits or {}
        rank = {p: i for i, p in enumerate(PRIORITIES)}
        candidates = sorted(
            (
                r
                for r in self._runs.values()
                if r.queue in wanted and r.state in CLAIMABLE_STATES and r.available_at <= now
            ),
            key=lambda r: (rank.get(r.priority, 1), r.available_at, r.run_id),
        )
        for run in candidates:
            slot = None
            if run.task in limits:
                slot = slot_name(run.task, run.concurrency_key)
                if self._counters.get(slot, 0) >= limits[run.task]:
                    continue
            self._take(run, now, worker_id=worker_id, lease_ttl=lease_ttl, slot=slot)
            return run.model_copy(deep=True)
        return None

    async def renew_leases(
        self,
        worker_id: str,
        held: Sequence[HeldLease],
        *,
        lease_ttl: float,
    ) -> dict[str, bool]:
        now = self._now()
        out: dict[str, bool] = {}
        for lease in held:
            run = self._fenced(lease.run_id, lease.attempt, worker_id)
            if run is None:
                continue
            run.lease_expires_at = now + lease_ttl
            out[run.run_id] = run.cancel_requested
        return out

    async def append_events(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        events: Sequence[EventIn],
        progress: dict | None = None,
    ) -> bool:
        run = self._fenced(run_id, attempt, worker_id)
        if run is None:
            return False
        log = self._events.setdefault(run_id, deque(maxlen=EVENT_LOG_CAP))
        seq = self._seq.get(run_id, 0)
        for event in events:
            seq += 1
            log.append(
                TaskEvent(
                    seq=seq, attempt=attempt, type=event.type, at=event.at, data=dict(event.data)
                )
            )
        self._seq[run_id] = seq
        if progress is not None:
            run.progress = json.loads(json.dumps(progress))
        run.updated_at = self._now()
        return True

    async def transition(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        outcome: AttemptOutcome,
    ) -> TaskRun | None:
        run = self._fenced(run_id, attempt, worker_id)
        if run is None:
            return None
        now = self._now()
        self._release_slot(run)
        self._clear_lease(run)
        run.updated_at = now
        kind = "cancelled" if run.state == RunState.CANCELLING else outcome.kind
        if kind == "succeeded":
            run.result = outcome.result
            run.error = None
            self._finish(run, RunState.SUCCEEDED, now)
        elif kind == "failed":
            run.error = outcome.error
            run.failure_count += 1
            self._finish(run, RunState.FAILED, now)
        elif kind == "cancelled":
            run.error = outcome.error if outcome.kind == "cancelled" else None
            self._finish(run, RunState.CANCELLED, now)
        elif kind == "retry":
            run.error = outcome.error
            run.failure_count += 1
            run.state = RunState.RETRYING
            run.available_at = now + max(0.0, outcome.delay)
            run.next_publish_at = run.available_at
        elif kind == "defer":
            run.defer_count += 1
            run.state = RunState.QUEUED
            run.available_at = now + max(0.0, outcome.delay)
            run.next_publish_at = run.available_at
        else:  # requeue
            run.error = outcome.error
            run.state = RunState.QUEUED
            run.available_at = now
            run.next_publish_at = now
        return run.model_copy(deep=True)

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        run = self._runs.get(run_id)
        if run is None:
            return CancelResult(status=CancelStatus.NOT_FOUND)
        now = self._now()
        if run.state in TERMINAL_STATES:
            return CancelResult(status=CancelStatus.TERMINAL, run=run.model_copy(deep=True))
        if run.state == RunState.CANCELLING:
            return CancelResult(
                status=CancelStatus.ALREADY_REQUESTED, run=run.model_copy(deep=True)
            )
        if run.state in CLAIMABLE_STATES:
            run.updated_at = now
            self._finish(run, RunState.CANCELLED, now)
            return CancelResult(status=CancelStatus.CANCELLED, run=run.model_copy(deep=True))
        run.state = RunState.CANCELLING
        run.cancel_requested = True
        run.updated_at = now
        return CancelResult(status=CancelStatus.REQUESTED, run=run.model_copy(deep=True))

    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        limit = max(1, min(MAX_PAGE, limit))
        log = self._events.get(run_id, ())
        return [e.model_copy() for e in log if e.seq > after][:limit]

    # ------------------------------------------------------------------ sweeper

    async def find_expired_leases(self, *, limit: int = 100) -> list[HeldLease]:
        now = self._now()
        rows = [
            r
            for r in self._runs.values()
            if r.state in LEASED_STATES
            and r.lease_expires_at is not None
            and r.lease_expires_at <= now
        ]
        rows.sort(key=lambda r: r.lease_expires_at or 0.0)
        return [HeldLease(run_id=r.run_id, attempt=r.attempt) for r in rows[: max(1, limit)]]

    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        now = self._now()
        run = self._runs.get(run_id)
        if (
            run is None
            or run.state not in LEASED_STATES
            or run.attempt != attempt
            or run.lease_expires_at is None
            or run.lease_expires_at > now
        ):
            return None
        self._release_slot(run)
        self._clear_lease(run)
        run.lost_count += 1
        run.updated_at = now
        if run.state == RunState.CANCELLING:
            self._finish(run, RunState.CANCELLED, now)
        elif run.lost_count > run.max_lost:
            run.error = {
                "code": "lost",
                "type": "LeaseLost",
                "message": f"worker lost the run {run.lost_count} times (max_lost={run.max_lost})",
            }
            self._finish(run, RunState.FAILED, now)
        else:
            run.state = RunState.QUEUED
            run.available_at = now
            run.next_publish_at = now
        return LostResult(run=run.model_copy(deep=True))

    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        now = self._now()
        rows = [
            r
            for r in self._runs.values()
            if r.state in CLAIMABLE_STATES
            and r.next_publish_at is not None
            and r.next_publish_at <= now
        ]
        rows.sort(key=lambda r: (r.next_publish_at or 0.0, r.run_id))
        return [r.model_copy(deep=True) for r in rows[: max(1, limit)]]

    async def mark_published(
        self, run_ids: Sequence[str], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        now = self._now()
        for run_id in run_ids:
            run = self._runs.get(run_id)
            if run is not None and run.state in CLAIMABLE_STATES:
                run.published_at = now
                run.next_publish_at = now + max(0.0, republish_after)

    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        return self._counters.get(slot_name(task, key), 0)

    async def recount_concurrency(self) -> None:
        counts: dict[str, int] = {}
        for run in self._runs.values():
            if run.state in LEASED_STATES and run.slot is not None:
                counts[run.slot] = counts.get(run.slot, 0) + 1
        self._counters = counts

    async def purge_expired(self, *, limit: int = 1000) -> int:
        now = self._now()
        gone = [
            r
            for r in self._runs.values()
            if r.state in TERMINAL_STATES and r.expires_at is not None and r.expires_at <= now
        ][: max(1, limit)]
        for run in gone:
            del self._runs[run.run_id]
            self._events.pop(run.run_id, None)
            self._seq.pop(run.run_id, None)
            key = self._key_of(run)
            if key is not None and self._keys.get(key) == run.run_id:
                del self._keys[key]
        return len(gone)

    # ------------------------------------------------------------------ test introspection

    def snapshot(self) -> dict[str, TaskRun]:
        """Copies of every run (tests check invariants with it)."""
        return {k: v.model_copy(deep=True) for k, v in self._runs.items()}

    def counters(self) -> dict[str, int]:
        return dict(self._counters)


def _encode_cursor(enqueued_at: float, run_id: str) -> str:
    raw = json.dumps([enqueued_at, run_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        enqueued_at, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(enqueued_at), str(run_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc
