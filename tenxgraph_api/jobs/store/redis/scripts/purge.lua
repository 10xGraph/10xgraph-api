-- Delete expired finished runs. Returns {deleted, scanned}. UNLINK frees memory off the
-- main thread (an event stream can hold EVENT_LOG_CAP entries).
-- KEYS[1] expires. ARGV: P, limit.
local ids = due_ids(KEYS[1], ARGV[2])
local n = 0
for _, id in ipairs(ids) do
  local rk = key('run', id)
  local h = redis.call('HMGET', rk, 'state', 'task', 'owner_user_id', 'idem')
  redis.call('ZREM', KEYS[1], id)
  if h[1] and TERMINAL[h[1]] then
    redis.call('UNLINK', rk, key('events', id))
    redis.call('ZREM', key('idx', 'all'), id)
    redis.call('ZREM', key('idx', 'state', h[1]), id)
    redis.call('ZREM', key('idx', 'task', h[2]), id)
    if h[3] then redis.call('ZREM', key('idx', 'owner', h[3]), id) end
    if h[4] and redis.call('HGET', key('idem'), h[4]) == id then
      redis.call('HDEL', key('idem'), h[4])
    end
    n = n + 1
  end
end
return {n, #ids}
