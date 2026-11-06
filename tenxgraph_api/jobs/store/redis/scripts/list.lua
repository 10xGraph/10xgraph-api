-- One page of runs from an index zset, newest first, filtered in place.
-- KEYS[1] index zset. ARGV: P, filter_json, limit, cursor_score, cursor_id, budget.
local function lt(a, b)
  local n = math.min(#a, #b)
  for i = 1, n do
    local x, y = string.byte(a, i), string.byte(b, i)
    if x ~= y then return x < y end
  end
  return #a < #b
end
local f = cjson.decode(ARGV[2])
local limit = tonumber(ARGV[3])
local budget = tonumber(ARGV[6])
local max, cs, cid = '+inf', nil, nil
if ARGV[4] ~= '' then max = ARGV[4]; cs = tonumber(ARGV[4]); cid = ARGV[5] end
local states = nil
if f['states'] then
  states = {}
  for _, s in ipairs(f['states']) do states[s] = true end
end
local out, scanned, offset = {}, 0, 0
while true do
  local batch = redis.call('ZREVRANGEBYSCORE', KEYS[1], max, '-inf', 'WITHSCORES',
    'LIMIT', offset, 200)
  if #batch == 0 then return {'end', out} end
  for i = 1, #batch, 2 do
    local id, score = batch[i], tonumber(batch[i + 1])
    if not (cs and score == cs and not lt(id, cid)) then
      scanned = scanned + 1
      local rk = key('run', id)
      local h = redis.call('HMGET', rk, 'state', 'task', 'queue', 'owner_user_id')
      if h[1] and (not states or states[h[1]])
        and (not f['task'] or h[2] == f['task'])
        and (not f['queue'] or h[3] == f['queue'])
        and (not f['owner'] or h[4] == f['owner']) then
        out[#out + 1] = redis.call('HGETALL', rk)
        if #out > limit then return {'more', out} end
      end
      if scanned >= budget then return {'budget', out, batch[i + 1], id} end
    end
  end
  offset = offset + 200
end
