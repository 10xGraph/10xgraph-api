"""In-memory run store for tests and single-process use.

Every method reads, checks and writes without an ``await`` in between, so each call is atomic
for every coroutine on the one event loop that uses the store. Not shared between processes
and not thread-safe.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence

from tenxgraph_api.jobs.core import serialization
from tenxgraph_api.jobs.core import transitions as tr
from tenxgraph_api.jobs.core.clock import SystemClock
from tenxgraph_api.jobs.core.models import (
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
    TaskEvent,
    TaskRun,
)

from .base import BaseRunStore, published_marks, slot_name


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

    def _save(self, before: TaskRun, after: TaskRun) -> TaskRun:
        """Store ``after``; give back the slot ``before`` held if ``after`` dropped it."""
        if before.slot is not None and after.slot is None:
            self._counters[before.slot] = max(0, self._counters.get(before.slot, 0) - 1)
        if after.slot is not None and before.slot is None:
            self._counters[after.slot] = self._counters.get(after.slot, 0) + 1
        self._runs[after.run_id] = after
        return after.model_copy(deep=True)

    def _full(self, slot: str | None, cap: int | None) -> bool:
        return slot is not None and cap is not None and self._counters.get(slot, 0) >= cap

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        self._closed = True

    async def now(self) -> float:
        return self._now()

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        serialization.check_new_run(new)
        now = self._now()
        key = self._key_of(new)
        if key is not None and key in self._keys:
            existing = self._runs[self._keys[key]]
            after = tr.replaced(existing, new, now) if key_mode == "replace" else None
            if after is None:
                return CreateResult(run=existing.model_copy(deep=True), created=False)
            run = self._save(existing, after)
            return CreateResult(
                run=run, created=False, replaced=True, due=run.available_at <= now
            )
        if new.run_id in self._runs:
            raise ValueError(f"run id {new.run_id} already exists")
        run = tr.created(new, now)
        self._runs[run.run_id] = run
        if key is not None:
            self._keys[key] = run.run_id
        return CreateResult(
            run=run.model_copy(deep=True), created=True, due=run.available_at <= now
        )

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
        limit = tr.page_limit(limit)
        f = filters or RunFilter()
        states = None if f.state is None else set(tr.state_values(f.state))
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
            after = tr.decode_cursor(cursor)
            rows = [r for r in rows if (r.enqueued_at, r.run_id) < after]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = tr.encode_cursor(page[-1].enqueued_at, page[-1].run_id)
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
        if not tr.claimable(run, now):
            return ClaimResult(status=ClaimStatus.NOT_READY, run=run.model_copy(deep=True))
        slot = None if concurrency_limit is None else slot_name(run.task, run.concurrency_key)
        if self._full(slot, concurrency_limit):
            return ClaimResult(status=ClaimStatus.CAPPED, run=self._save(run, tr.capped(run, now)))
        after = tr.claimed(run, now, worker_id=worker_id, lease_ttl=lease_ttl, slot=slot)
        return ClaimResult(status=ClaimStatus.CLAIMED, run=self._save(run, after))

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
            (r for r in self._runs.values() if r.queue in wanted and tr.claimable(r, now)),
            key=lambda r: (rank.get(r.priority, 1), r.available_at, r.run_id),
        )
        for run in candidates:
            slot = slot_name(run.task, run.concurrency_key) if run.task in limits else None
            if self._full(slot, limits.get(run.task)):
                continue
            after = tr.claimed(run, now, worker_id=worker_id, lease_ttl=lease_ttl, slot=slot)
            return self._save(run, after)
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
            run = self._runs.get(lease.run_id)
            if run is not None and tr.fenced(run, lease.attempt, worker_id):
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
        serialization.check_events(events, progress)
        run = self._runs.get(run_id)
        if run is None or not tr.fenced(run, attempt, worker_id):
            return False
        last = self._seq.get(run_id, 0)
        kept, _ = tr.log_events(last, attempt, events)
        self._events.setdefault(run_id, deque(maxlen=EVENT_LOG_CAP)).extend(kept)
        self._seq[run_id] = last + len(events)
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
        serialization.check_outcome(outcome)
        run = self._runs.get(run_id)
        if run is None or not tr.fenced(run, attempt, worker_id):
            return None
        return self._save(run, tr.ended(run, outcome, self._now()))

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        run = self._runs.get(run_id)
        if run is None:
            return CancelResult(status=CancelStatus.NOT_FOUND)
        status, after = tr.cancel(run, self._now())
        saved = run.model_copy(deep=True) if after is None else self._save(run, after)
        return CancelResult(status=status, run=saved)

    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        log = self._events.get(run_id, ())
        return [e.model_copy() for e in log if e.seq > after][: tr.page_limit(limit)]

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
        if run is None or not tr.lease_expired(run, attempt, now):
            return None
        return LostResult(run=self._save(run, tr.lost(run, now)))

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
        self, seen: Sequence[TaskRun], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        now = self._now()
        for run_id, attempt, expected in published_marks(seen):
            run = self._runs.get(run_id)
            if run is not None and tr.publish_matches(run, attempt, expected):
                self._runs[run_id] = tr.published(run, now, republish_after)

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
