-- Append events (and progress) for a fenced attempt.
-- KEYS[1] run key. ARGV: P, run_id, attempt, worker_id, has_progress, progress, events...
local rk, id = KEYS[1], ARGV[2]
if not fenced(rk, ARGV[3], ARGV[4]) then return 0 end
local now = now_us()
if #ARGV >= 7 then
  local ek = key('events', id)
  local seq = tonumber(redis.call('HGET', rk, 'seq') or '0')
  for i = 7, #ARGV do
    seq = seq + 1
    redis.call('XADD', ek, n2s(seq) .. '-0', 'e', ARGV[i])
  end
  redis.call('XTRIM', ek, 'MAXLEN', EVENT_CAP)
  redis.call('HSET', rk, 'seq', n2s(seq))
end
if ARGV[5] == '1' then redis.call('HSET', rk, 'progress', ARGV[6]) end
redis.call('HSET', rk, 'updated_at', n2s(now))
return 1
