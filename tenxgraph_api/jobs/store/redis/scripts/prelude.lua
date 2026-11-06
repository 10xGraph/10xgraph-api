-- Shared by every script. ARGV[1] is the key base ``<prefix>{tenxgraph-jobs}:``; KEYS[1] is
-- a real key of the slot (Cluster routing). Other keys are derived from it (same hash tag).
-- CAP_BASE, CAP_MAX and EVENT_CAP are defined in front of this file by scripts/__init__.py.
local P = ARGV[1]
local function key(...) return P .. table.concat({...}, ':') end
local function now_us()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000000 + tonumber(t[2])
end
local function n2s(n) return string.format('%.0f', n) end

-- Ids in zset z scored at or before now, at most limit.
local function due_ids(z, limit)
  return redis.call('ZRANGEBYSCORE', z, '-inf', n2s(now_us()), 'LIMIT', 0, limit)
end
local CLAIMABLE = {queued = true, retrying = true}
local LEASED = {running = true, cancelling = true}
local TERMINAL = {succeeded = true, failed = true, cancelled = true}

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
