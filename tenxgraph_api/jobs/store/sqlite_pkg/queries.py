# SQL here is built from module constants (table names, fixed IN lists, "?" marks);
# every value is a bound parameter.
# ruff: noqa: S608
"""SQLite jobs SQL and the row codec (JSON fields as text, ``cancel_requested`` as 0/1)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from tenxgraph_api.jobs.core import serialization
from tenxgraph_api.jobs.core.models import RunState, TaskRun

from .schema import COUNTERS, EVENTS, RUNS


LEASED = "('running', 'cancelling')"
CLAIMABLE = "('queued', 'retrying')"
TERMINAL = "('succeeded', 'failed', 'cancelled')"

RUN_FIELDS: tuple[str, ...] = tuple(TaskRun.model_fields)
JSON_FIELDS = frozenset({"kwargs", "identity", "trigger", "progress", "result", "error"})
COLS = ", ".join(RUN_FIELDS)

GET = f"SELECT {COLS} FROM {RUNS} WHERE run_id = ?"
EXISTS = f"SELECT 1 FROM {RUNS} WHERE run_id = ?"
BY_KEY = (
    f"SELECT {COLS} FROM {RUNS} WHERE task = ? AND owner_user_id IS ? "
    "AND tenant = ? AND idempotency_key = ?"
)
INSERT = f"INSERT INTO {RUNS} ({COLS}) VALUES ({', '.join('?' for _ in RUN_FIELDS)})"
COUNTER = f"SELECT running FROM {COUNTERS} WHERE name = ?"
ALL_COUNTERS = f"SELECT name, running FROM {COUNTERS}"
TAKE_SLOT = (
    f"INSERT INTO {COUNTERS} (name, running) VALUES (?, 1) "
    "ON CONFLICT(name) DO UPDATE SET running = running + 1"
)
RELEASE_SLOT = f"UPDATE {COUNTERS} SET running = max(0, running - 1) WHERE name = ?"
RENEW = (
    f"UPDATE {RUNS} SET lease_expires_at = ? WHERE run_id = ? AND attempt = ? "
    f"AND worker_id = ? AND state IN {LEASED} RETURNING cancel_requested"
)
EVENT_HEAD = (
    f"SELECT event_seq FROM {RUNS} WHERE run_id = ? AND attempt = ? "
    f"AND worker_id = ? AND state IN {LEASED}"
)
EVENT_INSERT = (
    f"INSERT INTO {EVENTS} (run_id, seq, attempt, type, at, data) VALUES (?, ?, ?, ?, ?, ?)"
)
EVENT_TRIM = f"DELETE FROM {EVENTS} WHERE run_id = ? AND seq < ?"
EVENT_SEQ = (
    f"UPDATE {RUNS} SET event_seq = ?, progress = coalesce(?, progress), updated_at = ? "
    "WHERE run_id = ?"
)
EVENTS_READ = (
    f"SELECT seq, attempt, type, at, data FROM {EVENTS} WHERE run_id = ? AND seq > ? "
    "ORDER BY seq LIMIT ?"
)
PUBLISHED = (
    f"UPDATE {RUNS} SET published_at = ?, next_publish_at = ? "
    f"WHERE run_id = ? AND attempt = ? AND next_publish_at = ? AND state IN {CLAIMABLE}"
)
RECOUNT = (
    f"DELETE FROM {COUNTERS}",
    f"INSERT INTO {COUNTERS} (name, running) SELECT slot, count(*) FROM {RUNS} "
    f"WHERE state IN {LEASED} AND slot IS NOT NULL GROUP BY slot",
)
PURGE_PICK = (
    f"SELECT run_id FROM {RUNS} WHERE state IN {TERMINAL} "
    "AND expires_at IS NOT NULL AND expires_at <= ? LIMIT ?"
)


def expired_leases(clock: str) -> str:
    return (
        f"SELECT run_id, attempt FROM {RUNS} WHERE state IN {LEASED} "
        f"AND lease_expires_at IS NOT NULL AND lease_expires_at <= {clock} "
        "ORDER BY lease_expires_at LIMIT ?"
    )


def due(clock: str) -> str:
    return (
        f"SELECT {COLS} FROM {RUNS} WHERE state IN {CLAIMABLE} "
        f"AND next_publish_at IS NOT NULL AND next_publish_at <= {clock} "
        "ORDER BY next_publish_at, run_id LIMIT ?"
    )


def pick(queues: int, limits: int) -> str:
    """The best due run in ``queues`` (``?`` each), skipping full caps (``limits`` pairs of
    task, cap ``?``), then ``now``. Order: priority, ``available_at``, ``run_id``."""
    if limits:
        limits_cte = " UNION ALL ".join("SELECT ? AS task, ? AS cap" for _ in range(limits))
    else:
        limits_cte = "SELECT NULL AS task, NULL AS cap WHERE 0"
    return f"""
        WITH limits(task, cap) AS ({limits_cte})
        SELECT {COLS} FROM {RUNS} r
        WHERE r.queue IN ({', '.join('?' for _ in range(queues))})
          AND r.state IN {CLAIMABLE}
          AND r.available_at <= ?
          AND NOT EXISTS (
            SELECT 1 FROM limits l JOIN {COUNTERS} c
              ON c.name = l.task || char(31) || ifnull(r.concurrency_key, '')
            WHERE l.task = r.task AND c.running >= l.cap
          )
        ORDER BY CASE r.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                 r.available_at, r.run_id
        LIMIT 1
    """


def update(fields: Sequence[str]) -> str:
    """Write ``fields`` of one run (``run_id`` is the last parameter)."""
    return f"UPDATE {RUNS} SET {', '.join(f'{n} = ?' for n in fields)} WHERE run_id = ?"


def to_param(name: str, value: Any) -> Any:
    if name in JSON_FIELDS:
        if value is None and name in ("result", "error"):
            return None
        return serialization.dumps(value, field=name)
    if name == "cancel_requested":
        return 1 if value else 0
    if name == "state":
        return RunState(value).value
    return value


def to_run(row: Any) -> TaskRun:
    data: dict[str, Any] = {}
    for name in RUN_FIELDS:
        value = row[name]
        if name in JSON_FIELDS:
            value = None if value is None else json.loads(value)
        elif name == "cancel_requested":
            value = bool(value)
        data[name] = value
    return TaskRun(**data)
