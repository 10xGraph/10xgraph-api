-- Leased runs whose lease has expired; returns run_id, attempt pairs.
-- KEYS[1] leases. ARGV: P, limit.
local ids = due_ids(KEYS[1], ARGV[2])
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
