"""The run store contract.

The run store is the single source of truth for a run: state, attempt, lease, progress,
result. Every state change is ONE atomic store call (a Postgres statement or transaction, a
Redis Lua script, one SQLite ``BEGIN IMMEDIATE`` transaction, one synchronous block in the
memory store), so an implementation never has to coordinate several calls.

Rules every implementation follows (``tests/unit_tests/jobs/contract`` checks them):

Clock
    ``now()`` is the store's clock (Postgres ``clock_timestamp()``, Redis ``TIME``, SQLite
    epoch, the memory store's injected clock). Leases, ``available_at`` and every timestamp
    the store writes use it, never the worker's clock.

Fencing
    ``attempt`` goes up by one on every successful claim and is the fencing token. Every worker
    write (``renew_leases``, ``append_events``, ``transition``) matches ``run_id``, ``attempt``
    and ``worker_id`` and requires the run to be in a leased state (``running`` or
    ``cancelling``). A write that matches nothing changes nothing and reports it (``False``,
    ``None`` or a missing key), so a stale worker learns it lost the run. A lease that has
    expired by the clock but was not yet recovered by ``mark_lost`` can still be renewed.

Concurrency caps
    A run of a capped task takes a slot in the counter ``(task, concurrency_key or "")``
    inside ``claim`` (``running < cap``, else ``CAPPED``) and records it in ``TaskRun.slot``.
    Every exit from a leased state (``transition``, ``mark_lost``) gives the slot back in the
    same atomic call. Counters never go below zero.

States
    ``queued -> running`` (claim), ``retrying -> running`` (claim), ``queued|retrying ->
    cancelled`` (request_cancel), ``running -> cancelling`` (request_cancel), ``running ->
    succeeded|failed|retrying|queued|cancelled`` (transition), ``cancelling -> cancelled``
    (transition, any outcome), ``running|cancelling -> queued|failed|cancelled`` (mark_lost).
    Terminal states (``succeeded``, ``failed``, ``cancelled``) never change again.

Publishing (delayed, retrying, lost and capped runs)
    The transport has no delay. Every claimable run carries ``next_publish_at``: set to
    ``available_at`` whenever the run becomes claimable (create, retry, defer, requeue, lost,
    capped), None while leased or terminal. ``due_runs`` returns claimable runs with
    ``next_publish_at <= now`` (index ``(state, next_publish_at)``); after publishing, the
    caller calls ``mark_published(runs_it_read, republish_after=...)``, which sets
    ``published_at = now`` and ``next_publish_at = now + republish_after``. So a run whose
    message was lost is published again after ``republish_after``, and a waiting backlog is
    never republished all at once.

    ``mark_published`` is a compare-and-set on ``(attempt, next_publish_at)``: it changes a run
    only while it still holds the values the caller read (from ``create_run``, ``due_runs`` or
    ``get_run``) and is ``queued`` or ``retrying``. Every way back to claimable after a claim
    raised ``attempt``, so the CAS holds even when two times are equal (a fake clock). A publish races the worker: the run can be claimed,
    fail and be set to retry in 1 s before the publisher marks it; an unconditional mark would
    then push that retry 5 minutes out.

Storable values
    Every store encodes JSON fields with ``tenxgraph_api.jobs.serialization`` and raises
    ``ValueNotStorable(field, reason)`` before any write when a value holds NaN or Infinity, a
    string with ``\\x00``, or is not JSON (Postgres cannot store the first two; the others
    refuse them too, so every backend behaves the same).

Tenant
    ``tenant`` is never NULL: None is stored as ``""`` (``TaskRun`` normalises it), so a SQL
    unique key over it works without ``NULLS NOT DISTINCT``. ``owner_user_id`` is always set
    by ``enqueue``.

Redis stores
    A Redis store keeps every key under one hash tag, ``REDIS_HASH_TAG`` (build keys with
    ``redis_key``), so every Lua script touches one slot and works on Redis Cluster. That puts
    all jobs data on one shard by design. Counter keys may be derived inside a script from the
    run hash (``slot``), since they share the tag.

Idempotency
    ``create_run`` with an ``idempotency_key`` is unique per ``(task, owner_user_id, tenant,
    idempotency_key)``. ``keep`` returns the existing run unchanged; ``replace`` swaps the
    kwargs of an existing run that is still ``queued`` and otherwise behaves like ``keep``.
    The key is freed when the run is purged.

Return values are copies: mutating a returned ``TaskRun`` never changes the store.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from tenxgraph_api.jobs.models import (
    REPUBLISH_AFTER,
    AttemptOutcome,
    CancelResult,
    ClaimResult,
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


class BaseRunStore(ABC):
    """Async run store. See the module docstring for the rules."""

    # ------------------------------------------------------------------ lifecycle

    async def setup(self) -> None:  # noqa: B027 - optional hook
        """Create tables / scripts. Idempotent; safe from many processes at once."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release connections. Calling it twice is a no-op."""

    @abstractmethod
    async def now(self) -> float:
        """The store clock, epoch seconds."""

    # ------------------------------------------------------------------ create and read

    @abstractmethod
    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        """Insert a run in ``queued`` (or return / replace the one holding its key).

        ``available_at`` is ``new.at`` when set, else ``now() + (new.delay or 0)``.
        ``enqueued_at`` / ``updated_at`` are ``now()``; ``published_at`` is None and
        ``next_publish_at = available_at``.
        ``CreateResult.due`` is True when ``available_at <= now()`` for a created or replaced
        run (the caller publishes it), False otherwise.
        """

    @abstractmethod
    async def get_run(self, run_id: str) -> TaskRun | None:
        """The run, or None."""

    @abstractmethod
    async def list_runs(
        self,
        filters: RunFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        """Runs newest first (``enqueued_at`` desc, then ``run_id`` desc).

        ``cursor`` is the opaque ``next_cursor`` of the previous page; ``next_cursor`` is None
        on the last page. ``limit`` is clamped to 1..1000.
        """

    # ------------------------------------------------------------------ worker writes

    @abstractmethod
    async def claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limit: int | None = None,
    ) -> ClaimResult:
        """CAS ``queued|retrying`` with ``available_at <= now`` to ``running``.

        On success: ``attempt + 1``, ``worker_id``, ``lease_expires_at = now + lease_ttl``,
        ``started_at = now``, ``cancel_requested = False``, ``error = None``, a fresh
        ``progress`` snapshot, ``cap_waits = 0`` and, with ``concurrency_limit``, a slot taken.
        ``next_publish_at = None``. Over the cap: no claim; ``available_at = now + min(30,
        2 ** cap_waits)``, ``cap_waits + 1``, ``next_publish_at = available_at``; status
        ``CAPPED``.
        """

    async def claim_next(
        self,
        queues: Sequence[str],
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limits: Mapping[str, int] | None = None,
    ) -> TaskRun | None:
        """Optional: find and claim the best due run in ``queues`` in one atomic call.

        For stores that are also their own transport (SQLite ``local``: one
        ``UPDATE ... WHERE run_id = (SELECT ... LIMIT 1) RETURNING``). Order: priority
        (``high`` first), then ``available_at``, then ``run_id``. Runs whose task is in
        ``concurrency_limits`` (task name -> cap) and whose ``(task, key)`` counter is full are
        skipped, not pushed back. Same effects as ``claim`` on the run it returns; None when
        nothing is claimable. The default raises ``NotImplementedError``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support claim_next")

    @abstractmethod
    async def renew_leases(
        self,
        worker_id: str,
        held: Sequence[HeldLease],
        *,
        lease_ttl: float,
    ) -> dict[str, bool]:
        """Heartbeat and cancel watch in one batched call.

        For each held lease still owned (fenced), set ``lease_expires_at = now + lease_ttl``.
        Returns ``{run_id: cancel_requested}`` for the leases still owned; a run missing from
        the result is lost and the worker must stop writing to it.
        """

    @abstractmethod
    async def append_events(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        events: Sequence[EventIn],
        progress: dict | None = None,
    ) -> bool:
        """Fenced progress flush: append ``events`` and replace the ``progress`` snapshot.

        Sequence numbers go up by one per event across the run's life (1, 2, ...). Only the
        newest ``EVENT_LOG_CAP`` (500) events are kept. Returns False (nothing written) when
        fenced out.
        """

    @abstractmethod
    async def transition(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        outcome: AttemptOutcome,
    ) -> TaskRun | None:
        """Fenced end of an attempt; see ``AttemptOutcome`` for each kind.

        Clears the lease and gives back the concurrency slot in the same call. Terminal
        outcomes set ``finished_at`` and ``expires_at = finished_at + retention`` (None when
        ``retention`` is None). ``retry``, ``defer`` and ``requeue`` set ``next_publish_at =
        available_at``. From ``cancelling`` the run always becomes ``cancelled``.
        Returns the new record, or None when fenced out.
        """

    # ------------------------------------------------------------------ callers

    @abstractmethod
    async def request_cancel(self, run_id: str) -> CancelResult:
        """Cancel in one call: ``queued|retrying`` -> ``cancelled`` (terminal); ``running`` ->
        ``cancelling`` with ``cancel_requested``; otherwise unchanged (see ``CancelStatus``).
        """

    @abstractmethod
    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        """Events with ``seq > after``, oldest first, at most ``limit`` (clamped to 1..1000)."""

    # ------------------------------------------------------------------ sweeper (Sprint 2)

    @abstractmethod
    async def find_expired_leases(self, *, limit: int = 100) -> list[HeldLease]:
        """Leased runs with ``lease_expires_at <= now``, oldest expiry first."""

    @abstractmethod
    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        """Recover one expired lease (CAS on ``attempt``, leased state and expiry <= now).

        ``lost_count + 1``; slot given back; lease cleared. Then: ``cancelling`` ->
        ``cancelled``; ``lost_count > max_lost`` -> ``failed`` (error code ``lost``); else ->
        ``queued`` with ``available_at = next_publish_at = now``. Returns None when the CAS
        matched nothing (renewed, finished or already recovered).
        """

    @abstractmethod
    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        """Claimable runs with ``next_publish_at <= now``, oldest ``next_publish_at`` first."""

    @abstractmethod
    async def mark_published(
        self, seen: Sequence[TaskRun], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        """Compare-and-set after a publish. ``seen`` are the run records the caller read
        before publishing (from ``create_run``, ``due_runs`` or ``get_run``).

        For each run still ``queued`` or ``retrying`` with the same ``attempt`` and
        ``next_publish_at`` as its seen record: ``published_at = now`` and ``next_publish_at =
        now + republish_after``. Any other run (unknown, leased, terminal, or claimed and made
        claimable again since: a claim always raises ``attempt``) is left as it is. A seen
        record without ``next_publish_at`` never matches.
        """

    @abstractmethod
    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        """Slots in use for ``(task, key or "")``."""

    @abstractmethod
    async def recount_concurrency(self) -> None:
        """Rebuild every counter from the leased runs that hold a slot (repairs leaks).

        Must not lose a concurrent ``claim`` increment or ``transition`` decrement: count and
        write each counter while holding its row lock (Postgres: ``SELECT ... FOR UPDATE`` on
        the counter row, then count runs, in one transaction), inside the script that also
        claims (Redis Lua), or serialised with claims (SQLite's single writer, the memory
        store's synchronous call).
        """

    @abstractmethod
    async def purge_expired(self, *, limit: int = 1000) -> int:
        """Delete finished runs with ``expires_at <= now`` and their events; returns the count."""


#: Hash tag shared by every key of a Redis jobs store (one Cluster slot, see module doc).
REDIS_HASH_TAG = "{tenxgraph-jobs}"


def redis_key(*parts: str) -> str:
    """A Redis jobs key: ``{tenxgraph-jobs}:<part>:<part>...``."""
    if not parts:
        raise ValueError("redis_key needs at least one part")
    return ":".join((REDIS_HASH_TAG, *parts))


def published_marks(seen: Sequence[TaskRun]) -> list[tuple[str, int, float]]:
    """``seen`` as sorted ``(run_id, attempt, next_publish_at)``; runs without a
    ``next_publish_at`` are dropped (they cannot match).

    Raises ``TypeError`` for anything but run records (the pre-CAS signature took ids).
    """
    if isinstance(seen, str | bytes) or not all(isinstance(r, TaskRun) for r in seen):
        raise TypeError(
            "mark_published takes the TaskRun records read before publishing, not run ids"
        )
    return sorted(
        (r.run_id, r.attempt, float(r.next_publish_at))
        for r in seen
        if r.next_publish_at is not None
    )


def slot_name(task: str, key: str | None) -> str:
    """Counter name shared by every store: task and key joined by the unit separator."""
    return f"{task}\x1f{key or ''}"
