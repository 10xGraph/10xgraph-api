"""The run state machine as pure functions, shared by every run store.

Each function takes a ``TaskRun`` (and the store clock's ``now``) and returns a new record; the
input is never changed. A store locks or reads the row, calls one of these, and writes back the
fields ``changed`` lists, all in one atomic call. The rules are in ``store/base.py``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence

from .models import (
    CAP_WAIT_BASE,
    CAP_WAIT_MAX,
    CLAIMABLE_STATES,
    EVENT_LOG_CAP,
    LEASED_STATES,
    TERMINAL_STATES,
    AttemptOutcome,
    CancelStatus,
    EventIn,
    NewRun,
    RunState,
    TaskEvent,
    TaskRun,
)


#: Most items ``list_runs`` and ``read_events`` return.
MAX_PAGE = 1000


def changed(before: TaskRun, after: TaskRun) -> list[str]:
    """Names of the fields that differ, in model order (the change set to write)."""
    return [name for name in TaskRun.model_fields if getattr(before, name) != getattr(after, name)]


# ---------------------------------------------------------------------- create


def created(new: NewRun, now: float) -> TaskRun:
    """A fresh ``queued`` run; ``available_at`` is ``at``, else ``now + delay``."""
    available_at = new.at if new.at is not None else now + (new.delay or 0.0)
    return TaskRun(
        **new.model_dump(exclude={"delay", "at"}),
        state=RunState.QUEUED,
        available_at=available_at,
        next_publish_at=available_at,
        enqueued_at=now,
        updated_at=now,
    )


def replaced(existing: TaskRun, new: NewRun, now: float) -> TaskRun | None:
    """``key_mode="replace"``: new kwargs while still ``queued``, else None (keep it)."""
    if existing.state != RunState.QUEUED:
        return None
    return existing.model_copy(
        update={
            "kwargs": dict(new.kwargs),
            "concurrency_key": new.concurrency_key,
            "updated_at": now,
        },
        deep=True,
    )


# ---------------------------------------------------------------------- claim and lease


def claimable(run: TaskRun, now: float) -> bool:
    return run.state in CLAIMABLE_STATES and run.available_at <= now


def claimed(
    run: TaskRun, now: float, *, worker_id: str, lease_ttl: float, slot: str | None
) -> TaskRun:
    """``running`` with ``attempt + 1``, a fresh lease and progress, holding ``slot``."""
    return run.model_copy(
        update={
            "state": RunState.RUNNING,
            "attempt": run.attempt + 1,
            "worker_id": worker_id,
            "lease_expires_at": now + lease_ttl,
            "started_at": now,
            "cancel_requested": False,
            "error": None,
            "progress": {},
            "cap_waits": 0,
            "slot": slot,
            "next_publish_at": None,
            "updated_at": now,
        },
        deep=True,
    )


def capped(run: TaskRun, now: float) -> TaskRun:
    """A claim refused by a full cap: wait 1 s doubling up to 30 s, then publish again."""
    available_at = now + min(CAP_WAIT_MAX, CAP_WAIT_BASE * 2**run.cap_waits)
    return run.model_copy(
        update={
            "available_at": available_at,
            "cap_waits": run.cap_waits + 1,
            "next_publish_at": available_at,
            "updated_at": now,
        },
        deep=True,
    )


def fenced(run: TaskRun | None, attempt: int, worker_id: str) -> bool:
    """True when a worker write from ``(attempt, worker_id)`` may change ``run``."""
    return (
        run is not None
        and run.state in LEASED_STATES
        and run.attempt == attempt
        and run.worker_id == worker_id
    )


def lease_expired(run: TaskRun | None, attempt: int, now: float) -> bool:
    """True when ``mark_lost`` may recover ``run`` (leased, same attempt, expiry <= now)."""
    return (
        run is not None
        and run.state in LEASED_STATES
        and run.attempt == attempt
        and run.lease_expires_at is not None
        and run.lease_expires_at <= now
    )


# ---------------------------------------------------------------------- end of a lease


def _finish(update: dict, run: TaskRun, state: RunState, now: float) -> None:
    update.update(
        state=state,
        next_publish_at=None,
        finished_at=now,
        expires_at=None if run.retention is None else now + run.retention,
    )


def _end_lease(now: float) -> dict:
    return {"slot": None, "worker_id": None, "lease_expires_at": None, "updated_at": now}


def ended(run: TaskRun, outcome: AttemptOutcome, now: float) -> TaskRun:
    """``transition``: apply ``outcome``; a ``cancelling`` run always ends ``cancelled``."""
    kind = "cancelled" if run.state == RunState.CANCELLING else outcome.kind
    update = _end_lease(now)
    if kind == "succeeded":
        update.update(result=outcome.result, error=None)
        _finish(update, run, RunState.SUCCEEDED, now)
    elif kind == "failed":
        update.update(error=outcome.error, failure_count=run.failure_count + 1)
        _finish(update, run, RunState.FAILED, now)
    elif kind == "cancelled":
        update["error"] = outcome.error if outcome.kind == "cancelled" else None
        _finish(update, run, RunState.CANCELLED, now)
    elif kind == "retry":
        at = now + max(0.0, outcome.delay)
        update.update(
            error=outcome.error,
            failure_count=run.failure_count + 1,
            state=RunState.RETRYING,
            available_at=at,
            next_publish_at=at,
        )
    elif kind == "defer":
        at = now + max(0.0, outcome.delay)
        update.update(
            defer_count=run.defer_count + 1,
            state=RunState.QUEUED,
            available_at=at,
            next_publish_at=at,
        )
    else:  # requeue
        update.update(
            error=outcome.error, state=RunState.QUEUED, available_at=now, next_publish_at=now
        )
    return run.model_copy(update=update, deep=True)


def lost(run: TaskRun, now: float) -> TaskRun:
    """``mark_lost``: ``cancelled`` if cancelling, ``failed`` past ``max_lost``, else queued now."""
    update = _end_lease(now)
    count = run.lost_count + 1
    update["lost_count"] = count
    if run.state == RunState.CANCELLING:
        _finish(update, run, RunState.CANCELLED, now)
    elif count > run.max_lost:
        update["error"] = {
            "code": "lost",
            "type": "LeaseLost",
            "message": f"worker lost the run {count} times (max_lost={run.max_lost})",
        }
        _finish(update, run, RunState.FAILED, now)
    else:
        update.update(state=RunState.QUEUED, available_at=now, next_publish_at=now)
    return run.model_copy(update=update, deep=True)


# ---------------------------------------------------------------------- callers and sweeper


def cancel(run: TaskRun, now: float) -> tuple[CancelStatus, TaskRun | None]:
    """``request_cancel``: the status and the new record (None when nothing changes)."""
    if run.state in TERMINAL_STATES:
        return CancelStatus.TERMINAL, None
    if run.state == RunState.CANCELLING:
        return CancelStatus.ALREADY_REQUESTED, None
    update: dict = {"updated_at": now}
    if run.state in CLAIMABLE_STATES:
        _finish(update, run, RunState.CANCELLED, now)
        status = CancelStatus.CANCELLED
    else:
        update.update(state=RunState.CANCELLING, cancel_requested=True)
        status = CancelStatus.REQUESTED
    return status, run.model_copy(update=update, deep=True)


def publish_matches(run: TaskRun | None, attempt: int, next_publish_at: float) -> bool:
    """``mark_published`` CAS: still claimable with the ``attempt`` and time the caller read."""
    return (
        run is not None
        and run.state in CLAIMABLE_STATES
        and run.attempt == attempt
        and run.next_publish_at == next_publish_at
    )


def published(run: TaskRun, now: float, republish_after: float) -> TaskRun:
    return run.model_copy(
        update={"published_at": now, "next_publish_at": now + max(0.0, republish_after)}
    )


def log_events(
    last_seq: int, attempt: int, events: Sequence[EventIn]
) -> tuple[list[TaskEvent], int]:
    """Number ``events`` after ``last_seq``. Returns the events to store and ``keep_from``:
    the log keeps only ``seq >= keep_from`` (the newest ``EVENT_LOG_CAP``)."""
    keep_from = max(1, last_seq + len(events) - EVENT_LOG_CAP + 1)
    kept = [
        TaskEvent(seq=seq, attempt=attempt, type=e.type, at=e.at, data=dict(e.data))
        for seq, e in enumerate(events, start=last_seq + 1)
        if seq >= keep_from
    ]
    return kept, keep_from


# ---------------------------------------------------------------------- paging (list_runs)


def page_limit(limit: int) -> int:
    return max(1, min(MAX_PAGE, limit))


def encode_cursor(enqueued_at: float, run_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([enqueued_at, run_id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        enqueued_at, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(enqueued_at), str(run_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


def state_values(state: object) -> list[str]:
    """``RunFilter.state`` (one value or a tuple) as sorted state strings."""
    raw = state if isinstance(state, tuple) else (state,)
    return sorted({str(s) for s in raw})
