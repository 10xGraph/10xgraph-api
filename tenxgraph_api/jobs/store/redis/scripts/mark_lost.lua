-- Recover a run whose lease expired: requeue, fail or finish cancelling.
-- KEYS[1] run key. ARGV: P, run_id, attempt.
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
