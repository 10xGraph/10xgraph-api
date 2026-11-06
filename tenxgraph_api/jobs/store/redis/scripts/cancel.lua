-- Cancel a claimable run, or ask the worker of a leased one to stop.
-- KEYS[1] run key. ARGV: P, run_id.
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
