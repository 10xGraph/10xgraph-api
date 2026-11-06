"""Records shared by the API, the worker, stores and transports.

All times are float epoch seconds (UTC) taken from the run store's clock.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED})
#: States a claim may start from.
CLAIMABLE_STATES = frozenset({RunState.QUEUED, RunState.RETRYING})
#: States in which a worker holds the lease (and, with a cap, a concurrency slot).
LEASED_STATES = frozenset({RunState.RUNNING, RunState.CANCELLING})

Priority = Literal["high", "normal", "low"]
PRIORITIES: tuple[str, ...] = ("high", "normal", "low")

#: Most events kept per run; older ones are dropped.
EVENT_LOG_CAP = 500
#: Most ``RetryAfter`` deferrals per run; the next one fails it.
MAX_DEFERRALS = 50
#: Wait after a claim refused by a concurrency cap: 1 s doubling up to 30 s.
CAP_WAIT_BASE = 1.0
CAP_WAIT_MAX = 30.0
#: Default wait before a published, still unclaimed run is published again.
REPUBLISH_AFTER = 300.0


def _tenant_text(value: Any) -> str:
    """Tenant is never NULL in a store: None becomes "" so a SQL unique key on
    ``(task, owner_user_id, tenant, idempotency_key)`` treats it as one value."""
    return "" if value is None else str(value)


class _Frozen(BaseModel):
    """Immutable value record."""

    model_config = ConfigDict(frozen=True)


class TaskRun(BaseModel):  # type: ignore[no-redef]  # pydantic.mypy vs a field named kwargs
    """One run, as stored. Returned by ``run.status()`` and ``runs.list()``."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    task: str
    queue: str = "default"
    state: RunState = RunState.QUEUED
    priority: Priority = "normal"

    # Attempts. ``attempt`` goes up by one on every claim and is the fencing token.
    attempt: int = 0
    max_retries: int = 0
    failure_count: int = 0
    lost_count: int = 0
    max_lost: int = 2
    defer_count: int = 0
    cap_waits: int = 0

    kwargs: dict[str, Any] = Field(default_factory=dict)
    identity: dict[str, Any] = Field(default_factory=dict)
    owner_user_id: str | None = None
    #: Tenant of the owner; "" when there is none (never None, see ``_tenant_text``).
    tenant: str = ""
    idempotency_key: str | None = None
    concurrency_key: str | None = None
    #: Counter the run holds a slot in while leased (``"<task>\x1f<key>"``), else None.
    slot: str | None = None
    trigger: dict[str, Any] = Field(default_factory=lambda: {"type": "code"})

    worker_id: str | None = None
    lease_expires_at: float | None = None
    cancel_requested: bool = False

    progress: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: dict[str, Any] | None = None

    parent_run_id: str | None = None
    batch_id: str | None = None
    code_version: str | None = None
    #: Seconds a finished run is kept; None keeps it until removed by hand.
    retention: float | None = None

    available_at: float = 0.0
    enqueued_at: float = 0.0
    published_at: float | None = None
    #: When the sweeper should (re)publish this claimable run; None while leased or
    #: terminal. Indexed with ``state`` so the sweeper reads only what is due.
    next_publish_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = 0.0
    expires_at: float | None = None

    _tenant = field_validator("tenant", mode="before")(_tenant_text)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class NewRun(BaseModel):  # type: ignore[no-redef]  # pydantic.mypy vs a field named kwargs
    """What ``create_run`` writes. The store fills times and ``available_at``.

    ``available_at`` is ``at`` when set, else store-now plus ``delay`` (0 when unset).
    """

    run_id: str
    task: str
    queue: str = "default"
    priority: Priority = "normal"
    kwargs: dict[str, Any] = Field(default_factory=dict)
    identity: dict[str, Any] = Field(default_factory=dict)
    owner_user_id: str | None = None
    tenant: str = ""
    idempotency_key: str | None = None
    concurrency_key: str | None = None
    max_retries: int = 0
    max_lost: int = 2
    retention: float | None = None
    trigger: dict[str, Any] = Field(default_factory=lambda: {"type": "code"})
    parent_run_id: str | None = None
    code_version: str | None = None
    delay: float | None = None
    at: float | None = None

    _tenant = field_validator("tenant", mode="before")(_tenant_text)


KeyMode = Literal["keep", "replace"]


class CreateResult(_Frozen):
    """``create_run`` outcome.

    ``created``: a new row was written. ``replaced``: an existing ``queued`` run had its kwargs
    swapped (``key_mode="replace"``). Neither: an existing run was returned unchanged.
    ``due``: the run may be claimed now (publish it); False for a delayed run.
    """

    run: TaskRun
    created: bool
    replaced: bool = False
    due: bool = False


class ClaimStatus(StrEnum):
    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    #: Wrong state (already running, terminal) or ``available_at`` in the future.
    NOT_READY = "not_ready"
    #: A concurrency cap is full; ``available_at`` was pushed back.
    CAPPED = "capped"


class ClaimResult(_Frozen):
    status: ClaimStatus
    run: TaskRun | None = None


OutcomeKind = Literal["succeeded", "failed", "retry", "defer", "cancelled", "requeue"]


class AttemptOutcome(_Frozen):
    """How an attempt ended, written by ``transition``.

    - ``succeeded``: terminal, ``result`` stored.
    - ``failed``: terminal, ``error`` stored, ``failure_count + 1``.
    - ``retry``: ``retrying`` with ``available_at = now + delay``, ``failure_count + 1``.
    - ``defer``: ``queued`` with ``available_at = now + delay``, ``defer_count + 1``
      (``RetryAfter``; no retry used).
    - ``cancelled``: terminal.
    - ``requeue``: ``queued`` now, nothing counted (worker shutdown).

    A run in ``cancelling`` always ends ``cancelled`` whatever the outcome (the cancel landed
    first; the result is discarded).
    """

    kind: OutcomeKind
    result: Any = None
    error: dict[str, Any] | None = None
    delay: float = 0.0


class CancelStatus(StrEnum):
    #: Was ``queued`` or ``retrying``: now ``cancelled``.
    CANCELLED = "cancelled"
    #: Was ``running``: now ``cancelling`` with ``cancel_requested``.
    REQUESTED = "requested"
    #: Already ``cancelling``: unchanged.
    ALREADY_REQUESTED = "already_requested"
    #: Already terminal: unchanged.
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"


class CancelResult(_Frozen):
    status: CancelStatus
    run: TaskRun | None = None


class LostResult(_Frozen):
    """``mark_lost`` outcome: the run after recovery (queued, failed or cancelled)."""

    run: TaskRun


class TaskEvent(BaseModel):
    """One entry of a run's event log."""

    seq: int
    attempt: int
    type: str
    at: float
    data: dict[str, Any] = Field(default_factory=dict)


class EventIn(_Frozen):
    """An event to append; the store assigns ``seq``."""

    type: str
    at: float
    data: dict[str, Any] = Field(default_factory=dict)


class RunFilter(_Frozen):
    """Filters for ``list_runs``. Every field set must match; a tuple means "any of"."""

    task: str | None = None
    state: RunState | str | tuple[RunState | str, ...] | None = None
    queue: str | None = None
    owner_user_id: str | None = None


class RunPage(_Frozen):
    """A page of runs, newest first. Pass ``next_cursor`` back for the next page."""

    items: list[TaskRun]
    next_cursor: str | None = None


class HeldLease(_Frozen):
    """A lease the worker believes it holds, for ``renew_leases``."""

    run_id: str
    attempt: int
