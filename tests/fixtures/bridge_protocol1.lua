-- nspire_bridge: lets an outside program evaluate expressions through the
-- Windows clipboard.
--
--   request   NSPIRE_IN:<id>:<expr>
--   reply     NSPIRE_OUT:<id>:OK:<result>   or   NSPIRE_OUT:<id>:ERR:<message>
--   ping      NSPIRE_IN:<id>:__PING__  ->  NSPIRE_OUT:<id>:OK:PONG:<apiLevel>:<cas|nocas>
--
-- <id> is any run of characters except ':'. Each id is answered once;
-- anything without the NSPIRE_IN: prefix is ignored.

platform.apiLevel = '2.7'

local PREFIX_IN  = "NSPIRE_IN:"
local PREFIX_OUT = "NSPIRE_OUT:"
local PERIOD     = 0.2
local MAX_SEEN   = 64

-- Error numbers seen from math.evalStr. Unknown ones are reported by number.
local ERRORS = {
  [910] = "syntax error",
}

local state = {
  ticks = 0, served = 0,
  id = "-", req = "-", res = "-", err = "-",
  cas = nil,
}

local seen, seenOrder = {}, {}
local lastClip = nil

local function remember(id)
  seen[id] = true
  table.insert(seenOrder, id)
  if #seenOrder > MAX_SEEN then
    seen[table.remove(seenOrder, 1)] = nil
  end
end

local function short(s, n)
  s = tostring(s)
  if #s > n then return s:sub(1, n) .. "..." end
  return s
end

-- Returns ok(bool), text. text is the result or an error message.
local function evaluate(expr)
  local ok, res, code = pcall(math.evalStr, expr)
  if not ok then
    return false, "lua: " .. tostring(res)
  end
  if res == nil then
    local n = tonumber(code)
    if n and ERRORS[n] then
      return false, "error " .. n .. " (" .. ERRORS[n] .. ")"
    end
    return false, "error " .. tostring(code)
  end
  return true, tostring(res)
end

local function detectCas()
  if state.cas == nil then
    -- Odd names so a user's a:=5 can't fake a numeric answer.
    local ok, res = evaluate("expand((brgcasp+brgcasq)^2)")
    state.cas = ok and res:find("brgcasp") ~= nil
  end
  return state.cas
end

local function handle(id, expr)
  local ok, text
  if expr == "__PING__" then
    ok = true
    text = "PONG:" .. tostring(platform.apiLevel) .. ":" .. (detectCas() and "cas" or "nocas")
  else
    ok, text = evaluate(expr)
  end

  state.id, state.req = id, expr
  if ok then
    state.res, state.err = text, "-"
  else
    state.res, state.err = "-", text
  end
  state.served = state.served + 1

  local reply = PREFIX_OUT .. id .. ":" .. (ok and "OK:" or "ERR:") .. text
  local okw, werr = pcall(clipboard.addText, reply)
  if not okw then state.err = "addText failed: " .. tostring(werr) end
end

local function poll()
  local ok, clip = pcall(clipboard.getText)
  if not ok then
    state.err = "getText failed: " .. tostring(clip)
    return
  end
  if clip == nil or clip == lastClip then return end
  lastClip = clip
  if clip:sub(1, #PREFIX_IN) ~= PREFIX_IN then return end

  local id, expr = clip:sub(#PREFIX_IN + 1):match("^([^:]+):(.*)$")
  if id == nil or seen[id] then return end
  remember(id)
  handle(id, expr)
end

local function start()
  timer.start(PERIOD)
end

on.construction = start
on.activate = start   -- older documents may skip on.construction

function on.timer()
  state.ticks = state.ticks + 1
  poll()
  platform.window:invalidate()
end

function on.paint(gc)
  gc:setFont("sansserif", "r", 10)
  local y = 4
  local function line(s)
    gc:drawString(s, 4, y, "top")
    y = y + 16
  end
  local cas = state.cas == nil and "?" or (state.cas and "cas" or "nocas")
  line("NSPIRE BRIDGE   tick " .. state.ticks .. "   served " .. state.served)
  line("api " .. tostring(platform.apiLevel) .. "   " .. cas)
  line("id:  " .. short(state.id, 30))
  line("req: " .. short(state.req, 40))
  line("res: " .. short(state.res, 40))
  line("err: " .. short(state.err, 40))
  line("Keep this page open. Don't copy while a request runs.")
end
