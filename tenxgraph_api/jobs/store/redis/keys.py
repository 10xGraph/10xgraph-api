"""Key layout of the Redis run store (table in JOBS_DESIGN.md, "Redis backend").

Every key is ``<prefix>{tenxgraph-jobs}:<part>...``: one hash tag, so every script touches one
Cluster slot. The prefix never contains braces, so the tag stays the first ``{...}``.
"""

from __future__ import annotations

import json

from tenxgraph_api.jobs.core.models import NewRun, RunFilter
from tenxgraph_api.jobs.store.base import REDIS_HASH_TAG, redis_key


def full_key(prefix: str, *parts: str) -> str:
    """``<prefix>{tenxgraph-jobs}:<parts joined by ':'>``."""
    return prefix + redis_key(*parts)


def key_base(prefix: str) -> str:
    """The base the scripts get as ``ARGV[1]`` and build their keys on."""
    return prefix + REDIS_HASH_TAG + ":"


def idem_field(new: NewRun) -> str:
    """The ``idem`` hash field of a run ('' when it has no idempotency key)."""
    if new.idempotency_key is None:
        return ""
    return json.dumps(
        [new.task, new.owner_user_id, new.tenant, new.idempotency_key], separators=(",", ":")
    )


def index_parts(f: RunFilter, states: list[str] | None) -> tuple[str, ...]:
    """The narrowest ``idx:*`` zset to scan for a ``list_runs`` filter."""
    if f.task is not None:
        return ("idx", "task", f.task)
    if f.owner_user_id is not None:
        return ("idx", "owner", f.owner_user_id)
    if states is not None and len(states) == 1:
        return ("idx", "state", states[0])
    return ("idx", "all")
