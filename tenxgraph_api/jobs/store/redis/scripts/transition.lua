-- Write the outcome of a fenced attempt.
-- KEYS[1] run key. ARGV: P, run_id, attempt, worker_id, kind, result_json, error_json ('' =
-- None), delay_us.
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
