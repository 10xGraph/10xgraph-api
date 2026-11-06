# SQL here is built from table names checked by ``check_schema_name`` and fixed column lists;
# every value is a bound parameter.
# ruff: noqa: S608
"""Postgres jobs SQL and the row codec.

JSON fields are ``jsonb`` sent and read as text (``$n::text::jsonb`` / ``col::text``), so a
caller's pool with its own jsonb codec cannot change them. Times come from
``clock_timestamp()`` (``now()`` is the transaction start).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from tenxgraph_api.jobs.core import serialization
from tenxgraph_api.jobs.core.models import RunState, TaskRun


NOW = "extract(epoch FROM clock_timestamp())::double precision"
CLAIMABLE = "('queued', 'retrying')"
LEASED = "('running', 'cancelling')"
TERMINAL = "('succeeded', 'failed', 'cancelled')"

#: Every ``TaskRun`` field is a column of the runs table.
RUN_FIELDS: tuple[str, ...] = tuple(TaskRun.model_fields)
JSON_FIELDS = frozenset({"kwargs", "identity", "trigger", "progress", "result", "error"})


def cols(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(
        f"{prefix}{c}::text AS {c}" if c in JSON_FIELDS else f"{prefix}{c}" for c in RUN_FIELDS
    )


def placeholder(name: str, index: int) -> str:
    return f"${index}::text::jsonb" if name in JSON_FIELDS else f"${index}"


def to_param(name: str, value: Any) -> Any:
    if name in JSON_FIELDS:
        if value is None and name in ("result", "error"):
            return None
        return serialization.dumps(value, field=name)
    if name == "state":
        return RunState(value).value
    return value


def to_run(row: Any) -> TaskRun:
    data = {c: row[c] for c in RUN_FIELDS}
    for name in JSON_FIELDS:
        if data[name] is not None:
            data[name] = json.loads(data[name])
    return TaskRun.model_validate(data)


class Queries:
    """Fixed statements for one schema (``t`` maps table name to qualified name)."""

    def __init__(self, t: Mapping[str, str]) -> None:
        runs, events, counters = t["runs"], t["events"], t["counters"]
        self.runs = runs
        c = cols()
        clock = f"WITH c AS MATERIALIZED (SELECT {NOW} AS ts)"
        key = (
            "task = $1 AND coalesce(owner_user_id, '') = coalesce($2::text, '') "
            "AND tenant = $3 AND idempotency_key = $4"
        )
        self.get = f"SELECT {c} FROM {runs} WHERE run_id = $1"
        self.get_for_update = self.get + " FOR UPDATE"
        self.by_key_for_update = f"SELECT {c} FROM {runs} WHERE {key} FOR UPDATE"
        values = ", ".join(placeholder(n, i) for i, n in enumerate(RUN_FIELDS, start=1))
        self.insert = (
            f"INSERT INTO {runs} ({', '.join(RUN_FIELDS)}) VALUES ({values}) "
            "ON CONFLICT (task, (coalesce(owner_user_id, '')), tenant, idempotency_key) "
            f"WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING {c}"
        )
        # Uncapped claim: one CAS statement (the hot path). Same fields as transitions.claimed.
        self.claim = (
            f"{clock} UPDATE {runs} SET state = 'running', attempt = attempt + 1, "
            "worker_id = $2, lease_expires_at = c.ts + $3, started_at = c.ts, "
            "cancel_requested = false, error = NULL, progress = '{}'::jsonb, cap_waits = 0, "
            "slot = NULL, next_publish_at = NULL, updated_at = c.ts FROM c "
            f"WHERE run_id = $1 AND state IN {CLAIMABLE} AND available_at <= c.ts RETURNING {c}"
        )
        # claim_next: reads counters without locking them, so a full cap is skipped lock-free.
        self.pick = (
            f"SELECT {cols('r')} FROM {runs} r "
            f"WHERE r.queue = ANY($1::text[]) AND r.state IN {CLAIMABLE} "
            "AND r.available_at <= $2 AND NOT (r.run_id = ANY($5::text[])) "
            "AND NOT EXISTS (SELECT 1 FROM unnest($3::text[], $4::int[]) AS l(task, cap) "
            f"JOIN {counters} k ON k.slot = r.task || chr(31) || "
            "coalesce(r.concurrency_key, '') WHERE l.task = r.task AND k.running >= l.cap) "
            "ORDER BY r.prio, r.available_at, r.run_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED"
        )
        self.take_slot = (
            f"INSERT INTO {counters} AS k (slot, running) VALUES ($1, 1) "
            "ON CONFLICT (slot) DO UPDATE SET running = k.running + 1 "
            "WHERE k.running < $2 RETURNING k.running"
        )
        self.release_slot = (
            f"UPDATE {counters} SET running = greatest(running - 1, 0) WHERE slot = $1"
        )
        self.renew = (
            f"{clock} UPDATE {runs} r SET lease_expires_at = c.ts + $4 "
            "FROM c, unnest($2::text[], $3::int[]) AS h(run_id, attempt) "
            "WHERE r.run_id = h.run_id AND r.attempt = h.attempt AND r.worker_id = $1 "
            f"AND r.state IN {LEASED} RETURNING r.run_id, r.cancel_requested"
        )
        self.events_head = (
            f"UPDATE {runs} SET event_seq = event_seq + $4, "
            f"progress = coalesce($5::text::jsonb, progress), updated_at = {NOW} "
            f"WHERE run_id = $1 AND attempt = $2 AND worker_id = $3 AND state IN {LEASED} "
            "RETURNING event_seq"
        )
        self.events_insert = (
            f"INSERT INTO {events} (run_id, seq, attempt, type, at, data) "
            "SELECT $1, s.seq, $2, s.type, s.at, s.data::jsonb FROM "
            "unnest($3::bigint[], $4::text[], $5::float8[], $6::text[]) AS s(seq, type, at, data)"
        )
        self.events_trim = f"DELETE FROM {events} WHERE run_id = $1 AND seq < $2"
        self.events_read = (
            f"SELECT seq, attempt, type, at, data::text AS data FROM {events} "
            "WHERE run_id = $1 AND seq > $2 ORDER BY seq LIMIT $3"
        )
        self.find_expired = (
            f"{clock} SELECT r.run_id, r.attempt FROM {runs} r, c "
            f"WHERE r.state IN {LEASED} AND r.lease_expires_at <= c.ts "
            "ORDER BY r.lease_expires_at, r.run_id LIMIT $1 FOR UPDATE OF r SKIP LOCKED"
        )
        self.due = (
            f"{clock} SELECT {cols('r')} FROM {runs} r, c "
            f"WHERE r.state IN {CLAIMABLE} AND r.next_publish_at <= c.ts "
            "ORDER BY r.next_publish_at, r.run_id LIMIT $1"
        )
        self.published = (
            f"{clock} UPDATE {runs} r SET published_at = c.ts, next_publish_at = c.ts + $4 "
            "FROM c, unnest($1::text[], $2::int[], $3::float8[]) AS s(run_id, attempt, seen) "
            "WHERE r.run_id = s.run_id AND r.attempt = s.attempt AND r.next_publish_at = s.seen "
            f"AND r.state IN {CLAIMABLE}"
        )
        self.count = f"SELECT running FROM {counters} WHERE slot = $1"
        self.lock_counters = f"SELECT slot FROM {counters} ORDER BY slot FOR UPDATE"
        held = (
            f"SELECT slot, count(*)::int AS n FROM {runs} WHERE state IN {LEASED} "
            "AND slot IS NOT NULL GROUP BY slot"
        )
        self.recount = (
            f"UPDATE {counters} k SET running = coalesce(x.n, 0) FROM ("
            f"SELECT s.slot, h.n FROM unnest($1::text[]) AS s(slot) LEFT JOIN ({held}) h "
            "USING (slot)) x WHERE k.slot = x.slot AND k.running <> coalesce(x.n, 0)"
        )
        self.recount_missing = (
            f"INSERT INTO {counters} (slot, running) {held} ON CONFLICT (slot) DO NOTHING"
        )
        self.purge = (
            f"{clock}, gone AS (DELETE FROM {runs} WHERE run_id IN (SELECT r.run_id "
            f"FROM {runs} r, c WHERE r.state IN {TERMINAL} AND r.expires_at <= c.ts "
            "ORDER BY r.expires_at LIMIT $1 FOR UPDATE OF r SKIP LOCKED) RETURNING run_id), "
            f"ev AS (DELETE FROM {events} e USING gone WHERE e.run_id = gone.run_id) "
            "SELECT count(*) FROM gone"
        )

    def update(self, fields: Sequence[str]) -> str:
        """Write ``fields`` ($3...) of the run ``run_id = $1 AND attempt = $2``."""
        sets = ", ".join(f"{n} = {placeholder(n, i)}" for i, n in enumerate(fields, start=3))
        return (
            f"UPDATE {self.runs} SET {sets} WHERE run_id = $1 AND attempt = $2 "
            f"RETURNING {cols()}"
        )

    def list(self, where: Sequence[str], limit_arg: str) -> str:
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return (
            f"SELECT {cols()} FROM {self.runs} {clause} "
            f"ORDER BY enqueued_at DESC, run_id DESC LIMIT {limit_arg}"
        )
