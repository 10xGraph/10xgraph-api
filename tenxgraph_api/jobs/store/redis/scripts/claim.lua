-- Lease a claimable run to a worker, honouring the concurrency cap.
-- KEYS[1] run key. ARGV: P, run_id, worker_id, ttl_us, limit ('' = none).
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
  slot = redis.call('HGET', rk, 'task') .. '\31' .. ck
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
