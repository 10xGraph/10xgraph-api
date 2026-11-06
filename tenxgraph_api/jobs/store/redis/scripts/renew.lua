-- Extend the leases a worker still holds; returns run_id, cancel_requested pairs.
-- KEYS[1] leases. ARGV: P, worker_id, ttl_us, run_id, attempt, run_id, attempt, ...
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
