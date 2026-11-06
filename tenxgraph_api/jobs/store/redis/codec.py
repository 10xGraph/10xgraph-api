"""Run hash encoding: times as integer microseconds, user data as JSON text, opaque cursors."""

from __future__ import annotations

import base64
import json
from typing import Any

from tenxgraph_api.jobs.core import serialization
from tenxgraph_api.jobs.core.models import NewRun, TaskRun


US = 1_000_000

_STR_FIELDS = frozenset(
    "run_id task queue state priority owner_user_id tenant idempotency_key concurrency_key "
    "slot worker_id parent_run_id batch_id code_version".split()
)
_INT_FIELDS = frozenset(
    "attempt max_retries failure_count lost_count max_lost defer_count cap_waits".split()
)
#: Stored as integer microseconds.
_TIME_FIELDS = frozenset(
    "retention available_at enqueued_at published_at next_publish_at started_at finished_at "
    "updated_at expires_at lease_expires_at".split()
)
_JSON_FIELDS = frozenset("kwargs identity trigger progress result error".split())


def us(seconds: float) -> int:
    """Seconds to integer microseconds."""
    return int(round(seconds * US))


def store_json(value: Any, field: str) -> str:
    """User data through the shared encoder (``ValueNotStorable`` before any script runs)."""
    return serialization.dumps(value, field=field)


def new_run_fields(new: NewRun) -> dict[str, str]:
    """The hash fields a new run starts with (the create script adds state and times)."""
    fields: dict[str, str] = {
        "run_id": new.run_id,
        "task": new.task,
        "queue": new.queue,
        "priority": new.priority,
        "tenant": new.tenant,
        "max_retries": str(new.max_retries),
        "max_lost": str(new.max_lost),
        "kwargs": store_json(new.kwargs, "kwargs"),
        "identity": store_json(new.identity, "identity"),
        "trigger": store_json(new.trigger, "trigger"),
        "progress": "{}",
        "attempt": "0",
        "failure_count": "0",
        "lost_count": "0",
        "defer_count": "0",
        "cap_waits": "0",
        "cancel_requested": "0",
    }
    optional = {
        "owner_user_id": new.owner_user_id,
        "idempotency_key": new.idempotency_key,
        "concurrency_key": new.concurrency_key,
        "parent_run_id": new.parent_run_id,
        "code_version": new.code_version,
    }
    fields.update({k: v for k, v in optional.items() if v is not None})
    if new.retention is not None:
        fields["retention"] = str(us(new.retention))
    return fields


def _pairs(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return raw
    items = list(raw or [])
    return dict(zip(items[::2], items[1::2], strict=True))


def decode_run(raw: Any) -> TaskRun | None:
    """A run from an ``HGETALL`` reply (dict or flat list); None when empty."""
    fields = _pairs(raw)
    if not fields:
        return None
    data: dict[str, Any] = {}
    for name, value in fields.items():
        if name in _STR_FIELDS:
            data[name] = value
        elif name in _INT_FIELDS:
            data[name] = int(value)
        elif name in _TIME_FIELDS:
            data[name] = int(float(value)) / US
        elif name in _JSON_FIELDS:
            data[name] = json.loads(value)
        elif name == "cancel_requested":
            data[name] = value == "1"
    return TaskRun(**data)


def decode_runs(raws: Any) -> list[TaskRun]:
    """Every non-empty run of a list of ``HGETALL`` replies."""
    return [r for r in (decode_run(raw) for raw in raws) if r is not None]


def encode_cursor(enqueued_us: int, run_id: str) -> str:
    """An opaque ``list_runs`` cursor for the last run of a page."""
    text = json.dumps([enqueued_us, run_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode()).decode()


def decode_cursor(cursor: str) -> tuple[int, str]:
    """``(enqueued_us, run_id)`` of a cursor; ValueError when it is not one."""
    try:
        enqueued_us, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(run_id, str):
            raise TypeError("run id")
        return int(enqueued_us), run_id
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc
