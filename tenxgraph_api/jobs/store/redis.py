"""Redis run store: every state change is one Lua script.

Layout (every key is ``<prefix>{tenxgraph-jobs}:<part>...``, built with ``redis_key``, so every
script touches one Cluster slot):

=================================  =========================================================
``run:<run_id>``                   hash, one field per ``TaskRun`` field (absent = None).
                                   Times are integer microseconds of the store clock (Redis
                                   ``TIME``); user data (kwargs, result, ...) is JSON text the
                                   scripts never decode. Internal fields: ``seq``, ``idem``.
``events:<run_id>``                stream, entry id ``<seq>-0``, field ``e`` = event JSON;
                                   trimmed to exactly ``EVENT_LOG_CAP`` entries.
``idem``                           hash ``json([task, owner, tenant, key])`` -> run_id.
``counters``                       hash ``slot_name(task, key)`` -> slots in use.
``due``                            zset run_id -> ``next_publish_at`` (claimable runs).
``leases``                         zset run_id -> ``lease_expires_at`` (leased runs).
``expires``                        zset run_id -> ``expires_at`` (finished runs to purge).
``idx:all`` / ``idx:state:<s>`` /  zsets run_id -> ``enqueued_at`` for ``list_runs`` (newest
``idx:task:<t>`` / ``idx:owner:<u>``  first, ties by run_id, an opaque cursor).
=================================  =========================================================

Own storage: the store connects to database ``db`` (``worker.redis_db``, default 1). When the
server refuses ``SELECT`` (``ResponseError``: ACL, managed services) or the client is a
``RedisCluster`` (database 0 only), it uses database 0 and puts every key under
``10xgraph:jobs:``, with one WARNING. The prefix never contains braces, so the hash tag stays
the first ``{...}`` of every key.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict
from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.connection import parse_url
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ResponseError

from tenxgraph_api.jobs import serialization
from tenxgraph_api.jobs.models import (
    CAP_WAIT_BASE,
    CAP_WAIT_MAX,
    EVENT_LOG_CAP,
    REPUBLISH_AFTER,
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

from .base import REDIS_HASH_TAG, BaseRunStore, published_marks, redis_key, slot_name


logger = logging.getLogger("tenxgraph_api.jobs")

#: Key prefix used when jobs share database 0 with other data.
FALLBACK_PREFIX = "10xgraph:jobs:"
#: Default database for jobs data (``worker.redis_db``).
DEFAULT_JOBS_DB = 1
MAX_PAGE = 1000
#: Expired runs one purge script deletes (short scripts keep Redis responsive).
PURGE_BATCH = 100
#: Most index entries one ``list_runs`` call scans before returning a short page.
LIST_SCAN_BUDGET = 10_000

_US = 1_000_000

#: Options for short commands (store calls, XACK, XADD): every call fails after 5 s.
SHORT_CLIENT_OPTIONS: dict[str, Any] = {
    "socket_timeout": 5.0,
    "socket_connect_timeout": 5.0,
    "socket_keepalive": True,
    "health_check_interval": 30,
    "decode_responses": True,
}


# ---------------------------------------------------------------------- connection


class JobsRedis(BaseModel):
    """A resolved jobs connection: the client, the key prefix and who owns the client."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    client: Any
    prefix: str
    owned: bool
    db: int


def check_prefix(prefix: str) -> str:
    if "{" in prefix or "}" in prefix:
        raise ValueError(
            "a jobs key prefix must not contain '{' or '}' (it would move the hash tag)"
        )
    return prefix


def no_retry() -> Retry:
    """A retry policy that never retries: jobs scripts are not idempotent (a claim or a
    transition replayed after a timeout could run twice)."""
    return Retry(NoBackoff(), 0)


def client_retries(client: Any) -> int:
    """How many times ``client`` retries a failed command (0 when it cannot tell).

    redis-py keeps the policy on each connection, built from the pool's connection kwargs
    (``retry``, ``retry_on_error``, ``retry_on_timeout``); a ``RedisCluster`` has its own
    ``retry``.
    """
    if isinstance(client, RedisCluster):
        retry = getattr(client, "retry", None)
        return int(retry.get_retries()) if retry is not None else 0
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None) or {}
    retry = kwargs.get("retry")
    if retry is not None:
        return int(retry.get_retries())
    return 1 if kwargs.get("retry_on_error") or kwargs.get("retry_on_timeout") else 0


def jobs_client_for(url: str, db: int, options: dict[str, Any]) -> Redis:
    """A client for ``url`` on database ``db`` (``db`` wins over the URL path), with command
    retries off whatever the URL or ``options`` say (see ``no_retry``)."""
    kwargs: dict[str, Any] = dict(parse_url(url))
    kwargs.update(options)
    kwargs["db"] = db
    kwargs["retry"] = no_retry()
    kwargs["retry_on_error"] = []
    kwargs.pop("retry_on_timeout", None)
    return Redis.from_pool(ConnectionPool(**kwargs))


def warn_if_retrying(client: Any, what: str) -> None:
    """One WARNING when a caller's client retries commands (it may replay a jobs script)."""
    retries = client_retries(client)
    if retries > 0:
        logger.warning(
            "jobs %s: the Redis client passed in retries failed commands (%d retries); a "
            "retried Lua script can apply a claim or transition twice. Build it with "
            "retry=Retry(NoBackoff(), 0) (redis.asyncio.retry, redis.backoff).",
            what,
            retries,
        )


async def open_jobs_redis(
    *,
    url: str | None,
    client: Any,
    db: int | None,
    key_prefix: str,
    options: dict[str, Any],
    what: str,
) -> JobsRedis:
    """Connect for jobs data, applying the own-storage rule (see the module docstring).

    A passed ``client`` is used as it is and never closed here; only its database decides the
    prefix. It must not retry commands (a WARNING says so when it does). A client built here
    from ``url`` never retries. ``db=None`` keeps the database named in ``url`` (0 when none).
    """
    check_prefix(key_prefix)
    if client is not None:
        warn_if_retrying(client, what)
        if isinstance(client, RedisCluster):
            logger.warning(
                "jobs %s: Redis Cluster has only database 0; using it with keys under %r",
                what,
                FALLBACK_PREFIX,
            )
            return JobsRedis(client=client, prefix=key_prefix + FALLBACK_PREFIX, owned=False, db=0)
        current = int(client.connection_pool.connection_kwargs.get("db") or 0)
        prefix = key_prefix + (FALLBACK_PREFIX if current == 0 else "")
        return JobsRedis(client=client, prefix=prefix, owned=False, db=current)
    if not url:
        raise ValueError(f"jobs {what}: pass a Redis url or client")
    wanted = int(parse_url(url).get("db") or 0) if db is None else int(db)
    made = jobs_client_for(url, wanted, options)
    if wanted != 0:
        try:
            await made.ping()
        except ResponseError as exc:
            await made.aclose()
            logger.warning(
                "jobs %s: Redis refused database %d (%s); using database 0 with keys under %r. "
                "Give each app and environment its own Redis database or server.",
                what,
                wanted,
                exc,
                FALLBACK_PREFIX,
            )
            made = jobs_client_for(url, 0, options)
            return JobsRedis(client=made, prefix=key_prefix + FALLBACK_PREFIX, owned=True, db=0)
        return JobsRedis(client=made, prefix=key_prefix, owned=True, db=wanted)
    return JobsRedis(client=made, prefix=key_prefix + FALLBACK_PREFIX, owned=True, db=0)


# ---------------------------------------------------------------------- record encoding

_STR_FIELDS = frozenset(
    {
        "run_id",
        "task",
        "queue",
        "state",
        "priority",
        "owner_user_id",
        "tenant",
        "idempotency_key",
        "concurrency_key",
        "slot",
        "worker_id",
        "parent_run_id",
        "batch_id",
        "code_version",
    }
)
_INT_FIELDS = frozenset(
    {
        "attempt",
        "max_retries",
        "failure_count",
        "lost_count",
        "max_lost",
        "defer_count",
        "cap_waits",
    }
)
#: Stored as integer microseconds.
_TIME_FIELDS = frozenset(
    {
        "retention",
        "available_at",
        "enqueued_at",
        "published_at",
        "next_publish_at",
        "started_at",
        "finished_at",
        "updated_at",
        "expires_at",
        "lease_expires_at",
    }
)
_JSON_FIELDS = frozenset({"kwargs", "identity", "trigger", "progress", "result", "error"})


def _us(seconds: float) -> int:
    return int(round(seconds * _US))


def _dumps(value: Any) -> str:
    """Internal values (keys, cursors, list filters)."""
    return json.dumps(value, separators=(",", ":"))


def _store_json(value: Any, field: str) -> str:
    """User data: the shared encoder (``ValueNotStorable`` before any script runs)."""
    return serialization.dumps(value, field=field)


def _pairs(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return raw
    items = list(raw or [])
    return dict(zip(items[::2], items[1::2], strict=True))


def _decode_run(raw: Any) -> TaskRun | None:
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
            data[name] = int(float(value)) / _US
        elif name in _JSON_FIELDS:
            data[name] = json.loads(value)
        elif name == "cancel_requested":
            data[name] = value == "1"
    return TaskRun(**data)


def _encode_cursor(enqueued_us: int, run_id: str) -> str:
    return base64.urlsafe_b64encode(_dumps([enqueued_us, run_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        enqueued_us, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(run_id, str):
            raise TypeError("run id")
        return int(enqueued_us), run_id
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


# ---------------------------------------------------------------------- Lua

# ARGV[1] is always the key base ``<prefix>{tenxgraph-jobs}:``; KEYS[1] is a real key of the
# slot (for Cluster routing). Other keys are derived from it, which the store contract allows
# because they share the hash tag.
_PRELUDE = f"""
local P = ARGV[1]
local function key(...) return P .. table.concat({{...}}, ':') end
local function now_us()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000000 + tonumber(t[2])
end
local function n2s(n) return string.format('%.0f', n) end
local CLAIMABLE = {{queued = true, retrying = true}}
local LEASED = {{running = true, cancelling = true}}
local TERMINAL = {{succeeded = true, failed = true, cancelled = true}}
local CAP_BASE = {int(CAP_WAIT_BASE * _US)}
local CAP_MAX = {int(CAP_WAIT_MAX * _US)}
local EVENT_CAP = {EVENT_LOG_CAP}

local function set_state(rk, id, new)
  local old = redis.call('HGET', rk, 'state')
  if old == new then return end
  local enq = redis.call('HGET', rk, 'enqueued_at')
  if old then redis.call('ZREM', key('idx', 'state', old), id) end
  redis.call('ZADD', key('idx', 'state', new), enq, id)
  redis.call('HSET', rk, 'state', new)
end

local function release_slot(rk)
  local slot = redis.call('HGET', rk, 'slot')
  if slot then
    local used = tonumber(redis.call('HGET', key('counters'), slot) or '0')
    if used <= 1 then
      redis.call('HDEL', key('counters'), slot)
    else
      redis.call('HSET', key('counters'), slot, n2s(used - 1))
    end
    redis.call('HDEL', rk, 'slot')
  end
end

local function clear_lease(rk, id)
  redis.call('HDEL', rk, 'worker_id', 'lease_expires_at')
  redis.call('ZREM', key('leases'), id)
end

local function finish(rk, id, state, now)
  set_state(rk, id, state)
  redis.call('HDEL', rk, 'next_publish_at')
  redis.call('ZREM', key('due'), id)
  redis.call('HSET', rk, 'finished_at', n2s(now))
  local retention = redis.call('HGET', rk, 'retention')
  if retention then
    local exp = n2s(now + tonumber(retention))
    redis.call('HSET', rk, 'expires_at', exp)
    redis.call('ZADD', key('expires'), exp, id)
  else
    redis.call('HDEL', rk, 'expires_at')
  end
end

local function make_claimable(rk, id, state, at)
  set_state(rk, id, state)
  local s = n2s(at)
  redis.call('HSET', rk, 'available_at', s, 'next_publish_at', s)
  redis.call('ZADD', key('due'), s, id)
end

local function fenced(rk, attempt, worker)
  local h = redis.call('HMGET', rk, 'state', 'attempt', 'worker_id')
  if h[1] and LEASED[h[1]] and h[2] == attempt and h[3] == worker then return h[1] end
  return false
end
"""

# KEYS[1] run key. ARGV: P, run_id, idem_field, key_mode, at_us, delay_us, kwargs_json,
# has_ck, ck, task, owner, has_owner, field/value pairs...
_CREATE = """
local rk, id, idem = KEYS[1], ARGV[2], ARGV[3]
local now = now_us()
if idem ~= '' then
  local existing = redis.call('HGET', key('idem'), idem)
  if existing then
    local ek = key('run', existing)
    local st = redis.call('HGET', ek, 'state')
    if not st then
      redis.call('HDEL', key('idem'), idem)
    else
      if ARGV[4] == 'replace' and st == 'queued' then
        redis.call('HSET', ek, 'kwargs', ARGV[7], 'updated_at', n2s(now))
        if ARGV[8] == '1' then
          redis.call('HSET', ek, 'concurrency_key', ARGV[9])
        else
          redis.call('HDEL', ek, 'concurrency_key')
        end
        local due = '0'
        if tonumber(redis.call('HGET', ek, 'available_at')) <= now then due = '1' end
        return {'replaced', due, redis.call('HGETALL', ek)}
      end
      return {'existing', '0', redis.call('HGETALL', ek)}
    end
  end
end
if redis.call('EXISTS', rk) == 1 then return {'duplicate'} end
local avail
if ARGV[5] ~= '' then avail = tonumber(ARGV[5]) else avail = now + tonumber(ARGV[6]) end
local fields = {}
for i = 13, #ARGV do fields[#fields + 1] = ARGV[i] end
redis.call('HSET', rk, unpack(fields))
local s_now, s_av = n2s(now), n2s(avail)
redis.call('HSET', rk, 'state', 'queued', 'available_at', s_av, 'next_publish_at', s_av,
  'enqueued_at', s_now, 'updated_at', s_now, 'seq', '0')
redis.call('ZADD', key('due'), s_av, id)
redis.call('ZADD', key('idx', 'all'), s_now, id)
redis.call('ZADD', key('idx', 'state', 'queued'), s_now, id)
redis.call('ZADD', key('idx', 'task', ARGV[10]), s_now, id)
if ARGV[12] == '1' then redis.call('ZADD', key('idx', 'owner', ARGV[11]), s_now, id) end
if idem ~= '' then
  redis.call('HSET', key('idem'), idem, id)
  redis.call('HSET', rk, 'idem', idem)
end
local due = '0'
if avail <= now then due = '1' end
return {'created', due, redis.call('HGETALL', rk)}
"""

# KEYS[1] index zset. ARGV: P, filter_json, limit, cursor_score, cursor_id, budget.
_LIST = """
local function lt(a, b)
  local n = math.min(#a, #b)
  for i = 1, n do
    local x, y = string.byte(a, i), string.byte(b, i)
    if x ~= y then return x < y end
  end
  return #a < #b
end
local f = cjson.decode(ARGV[2])
local limit = tonumber(ARGV[3])
local budget = tonumber(ARGV[6])
local max, cs, cid = '+inf', nil, nil
if ARGV[4] ~= '' then max = ARGV[4]; cs = tonumber(ARGV[4]); cid = ARGV[5] end
local states = nil
if f['states'] then
  states = {}
  for _, s in ipairs(f['states']) do states[s] = true end
end
local out, scanned, offset = {}, 0, 0
while true do
  local batch = redis.call('ZREVRANGEBYSCORE', KEYS[1], max, '-inf', 'WITHSCORES',
    'LIMIT', offset, 200)
  if #batch == 0 then return {'end', out} end
  for i = 1, #batch, 2 do
    local id, score = batch[i], tonumber(batch[i + 1])
    if not (cs and score == cs and not lt(id, cid)) then
      scanned = scanned + 1
      local rk = key('run', id)
      local h = redis.call('HMGET', rk, 'state', 'task', 'queue', 'owner_user_id')
      if h[1] and (not states or states[h[1]])
        and (not f['task'] or h[2] == f['task'])
        and (not f['queue'] or h[3] == f['queue'])
        and (not f['owner'] or h[4] == f['owner']) then
        out[#out + 1] = redis.call('HGETALL', rk)
        if #out > limit then return {'more', out} end
      end
      if scanned >= budget then return {'budget', out, batch[i + 1], id} end
    end
  end
  offset = offset + 200
end
"""

# KEYS[1] run key. ARGV: P, run_id, worker_id, ttl_us, limit ('' = none).
_CLAIM = """
local rk, id = KEYS[1], ARGV[2]
local st = redis.call('HGET', rk, 'state')
if not st then return {'not_found'} end
local now = now_us()
if not CLAIMABLE[st] or tonumber(redis.call('HGET', rk, 'available_at')) > now then
  return {'not_ready', redis.call('HGETALL', rk)}
end
local slot = false
if ARGV[5] ~= '' then
  local ck = redis.call('HGET', rk, 'concurrency_key') or ''
  slot = redis.call('HGET', rk, 'task') .. '\\31' .. ck
  local used = tonumber(redis.call('HGET', key('counters'), slot) or '0')
  if used >= tonumber(ARGV[5]) then
    local waits = tonumber(redis.call('HGET', rk, 'cap_waits') or '0')
    local av = n2s(now + math.min(CAP_MAX, CAP_BASE * (2 ^ waits)))
    redis.call('HSET', rk, 'available_at', av, 'next_publish_at', av,
      'cap_waits', n2s(waits + 1), 'updated_at', n2s(now))
    redis.call('ZADD', key('due'), av, id)
    return {'capped', redis.call('HGETALL', rk)}
  end
  redis.call('HINCRBY', key('counters'), slot, 1)
end
set_state(rk, id, 'running')
redis.call('HINCRBY', rk, 'attempt', 1)
local exp = n2s(now + tonumber(ARGV[4]))
redis.call('HSET', rk, 'worker_id', ARGV[3], 'lease_expires_at', exp, 'started_at', n2s(now),
  'cancel_requested', '0', 'progress', '{}', 'cap_waits', '0', 'updated_at', n2s(now))
redis.call('HDEL', rk, 'error', 'next_publish_at')
if slot then redis.call('HSET', rk, 'slot', slot) else redis.call('HDEL', rk, 'slot') end
redis.call('ZREM', key('due'), id)
redis.call('ZADD', key('leases'), exp, id)
return {'claimed', redis.call('HGETALL', rk)}
"""

# KEYS[1] leases. ARGV: P, worker_id, ttl_us, run_id, attempt, run_id, attempt, ...
_RENEW = """
local now = now_us()
local exp = n2s(now + tonumber(ARGV[3]))
local out = {}
for i = 4, #ARGV, 2 do
  local id = ARGV[i]
  local rk = key('run', id)
  if fenced(rk, ARGV[i + 1], ARGV[2]) then
    redis.call('HSET', rk, 'lease_expires_at', exp)
    redis.call('ZADD', key('leases'), exp, id)
    out[#out + 1] = id
    out[#out + 1] = redis.call('HGET', rk, 'cancel_requested')
  end
end
return out
"""

# KEYS[1] run key. ARGV: P, run_id, attempt, worker_id, has_progress, progress, events...
_APPEND = """
local rk, id = KEYS[1], ARGV[2]
if not fenced(rk, ARGV[3], ARGV[4]) then return 0 end
local now = now_us()
if #ARGV >= 7 then
  local ek = key('events', id)
  local seq = tonumber(redis.call('HGET', rk, 'seq') or '0')
  for i = 7, #ARGV do
    seq = seq + 1
    redis.call('XADD', ek, n2s(seq) .. '-0', 'e', ARGV[i])
  end
  redis.call('XTRIM', ek, 'MAXLEN', EVENT_CAP)
  redis.call('HSET', rk, 'seq', n2s(seq))
end
if ARGV[5] == '1' then redis.call('HSET', rk, 'progress', ARGV[6]) end
redis.call('HSET', rk, 'updated_at', n2s(now))
return 1
"""

# KEYS[1] run key. ARGV: P, run_id, attempt, worker_id, kind, result_json, error_json ('' =
# None), delay_us.
_TRANSITION = """
local rk, id = KEYS[1], ARGV[2]
local st = fenced(rk, ARGV[3], ARGV[4])
if not st then return false end
local now = now_us()
release_slot(rk)
clear_lease(rk, id)
redis.call('HSET', rk, 'updated_at', n2s(now))
local asked = ARGV[5]
local kind = asked
if st == 'cancelling' then kind = 'cancelled' end
local function set_error(v)
  if v == '' then redis.call('HDEL', rk, 'error') else redis.call('HSET', rk, 'error', v) end
end
if kind == 'succeeded' then
  redis.call('HSET', rk, 'result', ARGV[6])
  redis.call('HDEL', rk, 'error')
  finish(rk, id, 'succeeded', now)
elseif kind == 'failed' then
  set_error(ARGV[7])
  redis.call('HINCRBY', rk, 'failure_count', 1)
  finish(rk, id, 'failed', now)
elseif kind == 'cancelled' then
  if asked == 'cancelled' then set_error(ARGV[7]) else redis.call('HDEL', rk, 'error') end
  finish(rk, id, 'cancelled', now)
elseif kind == 'retry' then
  set_error(ARGV[7])
  redis.call('HINCRBY', rk, 'failure_count', 1)
  make_claimable(rk, id, 'retrying', now + tonumber(ARGV[8]))
elseif kind == 'defer' then
  redis.call('HINCRBY', rk, 'defer_count', 1)
  make_claimable(rk, id, 'queued', now + tonumber(ARGV[8]))
else
  set_error(ARGV[7])
  make_claimable(rk, id, 'queued', now)
end
return redis.call('HGETALL', rk)
"""

# KEYS[1] run key. ARGV: P, run_id.
_CANCEL = """
local rk, id = KEYS[1], ARGV[2]
local st = redis.call('HGET', rk, 'state')
if not st then return {'not_found'} end
if TERMINAL[st] then return {'terminal', redis.call('HGETALL', rk)} end
if st == 'cancelling' then return {'already_requested', redis.call('HGETALL', rk)} end
local now = now_us()
redis.call('HSET', rk, 'updated_at', n2s(now))
if CLAIMABLE[st] then
  finish(rk, id, 'cancelled', now)
  return {'cancelled', redis.call('HGETALL', rk)}
end
set_state(rk, id, 'cancelling')
redis.call('HSET', rk, 'cancel_requested', '1')
return {'requested', redis.call('HGETALL', rk)}
"""

# KEYS[1] leases. ARGV: P, limit.
_FIND_EXPIRED = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', n2s(now_us()), 'LIMIT', 0, ARGV[2])
local out = {}
for _, id in ipairs(ids) do
  local h = redis.call('HMGET', key('run', id), 'state', 'attempt')
  if h[1] and LEASED[h[1]] then
    out[#out + 1] = id
    out[#out + 1] = h[2]
  else
    redis.call('ZREM', KEYS[1], id)
  end
end
return out
"""

# KEYS[1] run key. ARGV: P, run_id, attempt.
_MARK_LOST = """
local rk, id = KEYS[1], ARGV[2]
local h = redis.call('HMGET', rk, 'state', 'attempt', 'lease_expires_at', 'max_lost')
if not h[1] or not LEASED[h[1]] or h[2] ~= ARGV[3] or not h[3] then return false end
local now = now_us()
if tonumber(h[3]) > now then return false end
release_slot(rk)
clear_lease(rk, id)
local lost = redis.call('HINCRBY', rk, 'lost_count', 1)
redis.call('HSET', rk, 'updated_at', n2s(now))
local max_lost = tonumber(h[4] or '0')
if h[1] == 'cancelling' then
  finish(rk, id, 'cancelled', now)
elseif lost > max_lost then
  redis.call('HSET', rk, 'error', cjson.encode({code = 'lost', type = 'LeaseLost',
    message = string.format('worker lost the run %d times (max_lost=%d)', lost, max_lost)}))
  finish(rk, id, 'failed', now)
else
  make_claimable(rk, id, 'queued', now)
end
return redis.call('HGETALL', rk)
"""

# KEYS[1] due. ARGV: P, limit.
_DUE = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', n2s(now_us()), 'LIMIT', 0, ARGV[2])
local out = {}
for _, id in ipairs(ids) do
  local rk = key('run', id)
  local st = redis.call('HGET', rk, 'state')
  if st and CLAIMABLE[st] then
    out[#out + 1] = redis.call('HGETALL', rk)
  else
    redis.call('ZREM', KEYS[1], id)
  end
end
return out
"""

# KEYS[1] due. ARGV: P, republish_us, (run_id, seen attempt, seen next_publish_at us)...
# CAS: only a claimable run still holding the attempt and next_publish_at the caller saw.
_MARK_PUBLISHED = """
local now = now_us()
local nxt = n2s(now + tonumber(ARGV[2]))
for i = 3, #ARGV, 3 do
  local id = ARGV[i]
  local rk = key('run', id)
  local h = redis.call('HMGET', rk, 'state', 'attempt', 'next_publish_at')
  if h[1] and CLAIMABLE[h[1]] and h[2] == ARGV[i + 1] and h[3]
      and tonumber(h[3]) == tonumber(ARGV[i + 2]) then
    redis.call('HSET', rk, 'published_at', n2s(now), 'next_publish_at', nxt)
    redis.call('ZADD', KEYS[1], nxt, id)
  end
end
return 1
"""

# KEYS[1] counters. ARGV: P.
_RECOUNT = """
local counts = {}
for _, id in ipairs(redis.call('ZRANGE', key('leases'), 0, -1)) do
  local h = redis.call('HMGET', key('run', id), 'state', 'slot')
  if h[1] and LEASED[h[1]] then
    if h[2] then counts[h[2]] = (counts[h[2]] or 0) + 1 end
  else
    redis.call('ZREM', key('leases'), id)
  end
end
redis.call('DEL', KEYS[1])
for slot, n in pairs(counts) do redis.call('HSET', KEYS[1], slot, n2s(n)) end
return 1
"""

# KEYS[1] expires. ARGV: P, limit. Returns {deleted, scanned}. UNLINK frees memory off the
# main thread (an event stream can hold EVENT_LOG_CAP entries).
_PURGE = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', n2s(now_us()), 'LIMIT', 0, ARGV[2])
local n = 0
for _, id in ipairs(ids) do
  local rk = key('run', id)
  local h = redis.call('HMGET', rk, 'state', 'task', 'owner_user_id', 'idem')
  redis.call('ZREM', KEYS[1], id)
  if h[1] and TERMINAL[h[1]] then
    redis.call('UNLINK', rk, key('events', id))
    redis.call('ZREM', key('idx', 'all'), id)
    redis.call('ZREM', key('idx', 'state', h[1]), id)
    redis.call('ZREM', key('idx', 'task', h[2]), id)
    if h[3] then redis.call('ZREM', key('idx', 'owner', h[3]), id) end
    if h[4] and redis.call('HGET', key('idem'), h[4]) == id then
      redis.call('HDEL', key('idem'), h[4])
    end
    n = n + 1
  end
end
return {n, #ids}
"""

_SCRIPTS = {
    "create": _CREATE,
    "list": _LIST,
    "claim": _CLAIM,
    "renew": _RENEW,
    "append": _APPEND,
    "transition": _TRANSITION,
    "cancel": _CANCEL,
    "find_expired": _FIND_EXPIRED,
    "mark_lost": _MARK_LOST,
    "due": _DUE,
    "mark_published": _MARK_PUBLISHED,
    "recount": _RECOUNT,
    "purge": _PURGE,
}


# ---------------------------------------------------------------------- store


class RedisRunStore(BaseRunStore):
    """Run store on Redis 7+ (also Valkey). See the module docstring for the key layout.

    Pass ``url`` (the store opens and closes its own client) or ``client`` (used as is, never
    closed). A client built from ``url`` never retries a command; a supplied ``client`` must
    not either (``retry=Retry(NoBackoff(), 0)``, no ``retry_on_error`` / ``retry_on_timeout``):
    every state change is a Lua script that is not idempotent, and a script replayed after a
    timeout could claim or transition twice. A supplied client that retries gets one WARNING.
    ``db`` is the jobs database (``worker.redis_db``); ``key_prefix`` goes in front of
    every key (tests use it for isolation). ``client_options`` override the short-command
    client options (``socket_timeout=5`` ...).
    """

    def __init__(
        self,
        url: str | None = None,
        client: Any = None,
        db: int | None = DEFAULT_JOBS_DB,
        *,
        key_prefix: str = "",
        **client_options: Any,
    ) -> None:
        if url is None and client is None:
            raise ValueError("RedisRunStore needs a url or a client")
        self._url = url
        self._given = client
        self._db = db
        self._key_prefix = check_prefix(key_prefix)
        self._options = {**SHORT_CLIENT_OPTIONS, **client_options}
        self._conn: JobsRedis | None = None
        self._scripts: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ helpers

    @property
    def key_prefix(self) -> str:
        """The prefix in front of the hash tag (after setup; includes the fallback prefix)."""
        return self._conn.prefix if self._conn else self._key_prefix

    @property
    def db(self) -> int | None:
        """The database in use (after setup)."""
        return self._conn.db if self._conn else None

    @property
    def client(self) -> Any:
        if self._conn is None:
            raise RuntimeError("RedisRunStore.setup() has not run")
        return self._conn.client

    def key(self, *parts: str) -> str:
        """A full key of this store."""
        return self.key_prefix + redis_key(*parts)

    @property
    def _base(self) -> str:
        return self.key_prefix + REDIS_HASH_TAG + ":"

    async def _ready(self) -> Any:
        if self._closed:
            raise RuntimeError("RedisRunStore is closed")
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    conn = await open_jobs_redis(
                        url=self._url,
                        client=self._given,
                        db=self._db,
                        key_prefix=self._key_prefix,
                        options=self._options,
                        what="run store",
                    )
                    self._scripts = {
                        name: conn.client.register_script(_PRELUDE + body)
                        for name, body in _SCRIPTS.items()
                    }
                    self._conn = conn
        return self._conn.client

    async def _run(self, name: str, key: str, *args: Any) -> Any:
        await self._ready()
        return await self._scripts[name](keys=[key], args=[self._base, *args])

    # ------------------------------------------------------------------ lifecycle

    async def setup(self) -> None:
        client = await self._ready()
        await client.ping()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is not None and conn.owned:
            await conn.client.aclose()

    async def now(self) -> float:
        client = await self._ready()
        seconds, micros = await client.time()
        return (int(seconds) * _US + int(micros)) / _US

    # ------------------------------------------------------------------ create and read

    async def create_run(self, new: NewRun, *, key_mode: KeyMode = "keep") -> CreateResult:
        idem = ""
        if new.idempotency_key is not None:
            idem = _dumps([new.task, new.owner_user_id, new.tenant, new.idempotency_key])
        fields: dict[str, str] = {
            "run_id": new.run_id,
            "task": new.task,
            "queue": new.queue,
            "priority": new.priority,
            "tenant": new.tenant,
            "max_retries": str(new.max_retries),
            "max_lost": str(new.max_lost),
            "kwargs": _store_json(new.kwargs, "kwargs"),
            "identity": _store_json(new.identity, "identity"),
            "trigger": _store_json(new.trigger, "trigger"),
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
            fields["retention"] = str(_us(new.retention))
        flat = [item for pair in fields.items() for item in pair]
        reply = await self._run(
            "create",
            self.key("run", new.run_id),
            new.run_id,
            idem,
            key_mode,
            "" if new.at is None else str(_us(new.at)),
            str(_us(new.delay or 0.0)),
            fields["kwargs"],
            "0" if new.concurrency_key is None else "1",
            new.concurrency_key or "",
            new.task,
            new.owner_user_id or "",
            "0" if new.owner_user_id is None else "1",
            *flat,
        )
        status = reply[0]
        if status == "duplicate":
            raise ValueError(f"run id {new.run_id} already exists")
        run = _decode_run(reply[2])
        if run is None:  # pragma: no cover - the script always returns the hash
            raise RuntimeError(f"run {new.run_id} vanished during create")
        return CreateResult(
            run=run,
            created=status == "created",
            replaced=status == "replaced",
            due=reply[1] == "1",
        )

    async def get_run(self, run_id: str) -> TaskRun | None:
        client = await self._ready()
        return _decode_run(await client.hgetall(self.key("run", run_id)))

    async def list_runs(
        self,
        filters: RunFilter | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> RunPage:
        limit = max(1, min(MAX_PAGE, limit))
        after = _decode_cursor(cursor) if cursor else None
        f = filters or RunFilter()
        spec: dict[str, Any] = {}
        states: list[str] | None = None
        if f.state is not None:
            raw = f.state if isinstance(f.state, tuple) else (f.state,)
            states = sorted({str(s) for s in raw})
            spec["states"] = states
        if f.task is not None:
            spec["task"] = f.task
        if f.queue is not None:
            spec["queue"] = f.queue
        if f.owner_user_id is not None:
            spec["owner"] = f.owner_user_id
        if f.task is not None:
            index = self.key("idx", "task", f.task)
        elif f.owner_user_id is not None:
            index = self.key("idx", "owner", f.owner_user_id)
        elif states is not None and len(states) == 1:
            index = self.key("idx", "state", states[0])
        else:
            index = self.key("idx", "all")
        reply = await self._run(
            "list",
            index,
            _dumps(spec),
            limit,
            "" if after is None else str(after[0]),
            "" if after is None else after[1],
            LIST_SCAN_BUDGET,
        )
        runs = [r for r in (_decode_run(raw) for raw in reply[1]) if r is not None]
        next_cursor = None
        if reply[0] == "more":
            runs = runs[:limit]
            last = runs[-1]
            next_cursor = _encode_cursor(_us(last.enqueued_at), last.run_id)
        elif reply[0] == "budget":
            next_cursor = _encode_cursor(int(float(reply[2])), reply[3])
        return RunPage(items=runs, next_cursor=next_cursor)

    # ------------------------------------------------------------------ worker writes

    async def claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl: float,
        concurrency_limit: int | None = None,
    ) -> ClaimResult:
        reply = await self._run(
            "claim",
            self.key("run", run_id),
            run_id,
            worker_id,
            str(_us(lease_ttl)),
            "" if concurrency_limit is None else str(int(concurrency_limit)),
        )
        status = ClaimStatus(reply[0])
        run = _decode_run(reply[1]) if len(reply) > 1 else None
        return ClaimResult(status=status, run=run)

    async def renew_leases(
        self,
        worker_id: str,
        held: Sequence[HeldLease],
        *,
        lease_ttl: float,
    ) -> dict[str, bool]:
        if not held:
            return {}
        args: list[str] = []
        for lease in held:
            args += [lease.run_id, str(lease.attempt)]
        reply = await self._run("renew", self.key("leases"), worker_id, str(_us(lease_ttl)), *args)
        return {reply[i]: reply[i + 1] == "1" for i in range(0, len(reply), 2)}

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
        encoded = [
            _store_json({"attempt": attempt, "type": e.type, "at": e.at, "data": e.data}, "event")
            for e in events
        ]
        reply = await self._run(
            "append",
            self.key("run", run_id),
            run_id,
            str(attempt),
            worker_id,
            "0" if progress is None else "1",
            "" if progress is None else _store_json(progress, "progress"),
            *encoded,
        )
        return bool(int(reply))

    async def transition(
        self,
        run_id: str,
        *,
        attempt: int,
        worker_id: str,
        outcome: AttemptOutcome,
    ) -> TaskRun | None:
        reply = await self._run(
            "transition",
            self.key("run", run_id),
            run_id,
            str(attempt),
            worker_id,
            outcome.kind,
            _store_json(outcome.result, "result"),
            "" if outcome.error is None else _store_json(outcome.error, "error"),
            str(_us(max(0.0, outcome.delay))),
        )
        return _decode_run(reply) if reply else None

    # ------------------------------------------------------------------ callers

    async def request_cancel(self, run_id: str) -> CancelResult:
        reply = await self._run("cancel", self.key("run", run_id), run_id)
        run = _decode_run(reply[1]) if len(reply) > 1 else None
        return CancelResult(status=CancelStatus(reply[0]), run=run)

    async def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[TaskEvent]:
        limit = max(1, min(MAX_PAGE, limit))
        client = await self._ready()
        start = f"{max(0, int(after)) + 1}-0"
        entries = await client.xrange(self.key("events", run_id), start, "+", count=limit)
        out: list[TaskEvent] = []
        for entry_id, fields in entries:
            data = json.loads(fields["e"])
            out.append(TaskEvent(seq=int(entry_id.split("-", 1)[0]), **data))
        return out

    # ------------------------------------------------------------------ sweeper

    async def find_expired_leases(self, *, limit: int = 100) -> list[HeldLease]:
        reply = await self._run("find_expired", self.key("leases"), max(1, limit))
        return [
            HeldLease(run_id=reply[i], attempt=int(reply[i + 1])) for i in range(0, len(reply), 2)
        ]

    async def mark_lost(self, run_id: str, *, attempt: int) -> LostResult | None:
        reply = await self._run("mark_lost", self.key("run", run_id), run_id, str(attempt))
        run = _decode_run(reply) if reply else None
        return LostResult(run=run) if run is not None else None

    async def due_runs(self, *, limit: int = 100) -> list[TaskRun]:
        reply = await self._run("due", self.key("due"), max(1, limit))
        return [r for r in (_decode_run(raw) for raw in reply) if r is not None]

    async def mark_published(
        self, seen: Sequence[TaskRun], *, republish_after: float = REPUBLISH_AFTER
    ) -> None:
        marks = published_marks(seen)
        if not marks:
            return
        args = [
            item
            for run_id, attempt, value in marks
            for item in (run_id, str(attempt), str(_us(value)))
        ]
        await self._run(
            "mark_published",
            self.key("due"),
            str(_us(max(0.0, republish_after))),
            *args,
        )

    async def concurrency_count(self, task: str, key: str | None = None) -> int:
        client = await self._ready()
        value = await client.hget(self.key("counters"), slot_name(task, key))
        return int(value or 0)

    async def recount_concurrency(self) -> None:
        await self._run("recount", self.key("counters"))

    async def purge_expired(self, *, limit: int = 1000) -> int:
        """In scripts of at most ``PURGE_BATCH`` runs, until ``limit`` or nothing is due."""
        left, deleted = max(1, limit), 0
        while left > 0:
            batch = min(PURGE_BATCH, left)
            n, scanned = await self._run("purge", self.key("expires"), batch)
            deleted += int(n)
            left -= int(scanned)
            if int(scanned) < batch:
                break
        return deleted
