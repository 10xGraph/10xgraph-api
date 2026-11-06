-- Rebuild the concurrency counters from the leased runs.
-- KEYS[1] counters. ARGV: P.
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
