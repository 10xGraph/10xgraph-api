-- Claimable runs whose next_publish_at has come.
-- KEYS[1] due. ARGV: P, limit.
local ids = due_ids(KEYS[1], ARGV[2])
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
