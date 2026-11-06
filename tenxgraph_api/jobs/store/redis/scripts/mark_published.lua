-- CAS: only a claimable run still holding the attempt and next_publish_at the caller saw.
-- KEYS[1] due. ARGV: P, republish_us, (run_id, seen attempt, seen next_publish_at us)...
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
