-- Create a run, or return the run its idempotency key names.
-- KEYS[1] run key. ARGV: P, run_id, idem_field, key_mode, at_us, delay_us, kwargs_json,
-- has_ck, ck, task, owner, has_owner, field/value pairs...
local rk, id, idem = KEYS[1], ARGV[2], ARGV[3]
local now = now_us()
if idem ~= '' then
  local existing = redis.call('HGET', key('idem'), idem)
  if existing then
    local ek = key('run', existing)
    local st = redis.call('HGET', ek, 'state')
    if not st then
      redis.call('HDEL', key('idem'), idem)
    else
      if ARGV[4] == 'replace' and st == 'queued' then
        redis.call('HSET', ek, 'kwargs', ARGV[7], 'updated_at', n2s(now))
        if ARGV[8] == '1' then
          redis.call('HSET', ek, 'concurrency_key', ARGV[9])
        else
          redis.call('HDEL', ek, 'concurrency_key')
        end
        local due = '0'
        if tonumber(redis.call('HGET', ek, 'available_at')) <= now then due = '1' end
        return {'replaced', due, redis.call('HGETALL', ek)}
      end
      return {'existing', '0', redis.call('HGETALL', ek)}
    end
  end
end
if redis.call('EXISTS', rk) == 1 then return {'duplicate'} end
local avail
if ARGV[5] ~= '' then avail = tonumber(ARGV[5]) else avail = now + tonumber(ARGV[6]) end
local fields = {}
for i = 13, #ARGV do fields[#fields + 1] = ARGV[i] end
redis.call('HSET', rk, unpack(fields))
local s_now, s_av = n2s(now), n2s(avail)
redis.call('HSET', rk, 'state', 'queued', 'available_at', s_av, 'next_publish_at', s_av,
  'enqueued_at', s_now, 'updated_at', s_now, 'seq', '0')
redis.call('ZADD', key('due'), s_av, id)
redis.call('ZADD', key('idx', 'all'), s_now, id)
redis.call('ZADD', key('idx', 'state', 'queued'), s_now, id)
redis.call('ZADD', key('idx', 'task', ARGV[10]), s_now, id)
if ARGV[12] == '1' then redis.call('ZADD', key('idx', 'owner', ARGV[11]), s_now, id) end
if idem ~= '' then
  redis.call('HSET', key('idem'), idem, id)
  redis.call('HSET', rk, 'idem', idem)
end
local due = '0'
if avail <= now then due = '1' end
return {'created', due, redis.call('HGETALL', rk)}
