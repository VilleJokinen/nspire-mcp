-- nspire_bridge: lets an outside program evaluate expressions through the
-- Windows clipboard.
--
--   request   NSPIRE_IN:<id>:<expr>
--   reply     NSPIRE_OUT:<id>:OK:<result>   or   NSPIRE_OUT:<id>:ERR:<message>
--   ping      NSPIRE_IN:<id>:__PING__
--             ->  NSPIRE_OUT:<id>:OK:PONG:<apiLevel>:<cas|nocas>:<protocol>
--   batch     NSPIRE_IN:<id>:]BATCH:<e1>;<e2>;...
--             ->  NSPIRE_OUT:<id>:OK:BATCH:<n>:<s1><r1>;<s2><r2>;...
--             The expressions run in order. s is O (result) or E (error
--             message). In e and r, '%', ';', CR and LF are written as %25,
--             %3B, %0D and %0A. The leading ']' makes an older script reject
--             a batch as a syntax error instead of running any of it.
--
-- <id> is any run of characters except ':'. Each id is evaluated once. A
-- reply is written only while the clipboard still holds its request: if the
-- requester gave up and put the user's clipboard back, or sent a newer
-- request, a late reply overwrites nothing. If the same request is posted
-- again, its kept reply is written then (nothing is evaluated twice).
-- Anything without the NSPIRE_IN: prefix is ignored.
--
-- Protocol 1: requests and ping. Protocol 2: batch, held replies, resend.

platform.apiLevel = '2.7'

local PREFIX_IN  = "NSPIRE_IN:"
local PREFIX_OUT = "NSPIRE_OUT:"
local PING       = "__PING__"
local BATCH      = "]BATCH:"
local PROTOCOL   = 2
local PERIOD     = 0.1
local MAX_SEEN   = 64

-- Error codes from math.evalStr, as listed in the TI-Nspire Reference Guide
-- ("Error Codes and Messages"). Unknown ones are reported by number.
local ERRORS = {
  [10] = "a function did not return a value",
  [20] = "a test did not resolve to TRUE or FALSE",
  [30] = "argument cannot be a folder name",
  [40] = "argument error",
  [50] = "argument mismatch",
  [60] = "argument must be a Boolean expression or integer",
  [70] = "argument must be a decimal number",
  [90] = "argument must be a list",
  [100] = "argument must be a matrix",
  [130] = "argument must be a string",
  [140] = "argument must be a variable name",
  [160] = "argument must be an expression",
  [165] = "batteries too low for sending or receiving",
  [170] = "bound: the lower bound must be less than the upper bound",
  [180] = "break",
  [190] = "circular definition",
  [200] = "constraint expression invalid",
  [210] = "invalid data type",
  [220] = "dependent limit",
  [230] = "dimension: a list or matrix index is not valid",
  [235] = "dimension error: not enough elements in the lists",
  [240] = "dimension mismatch",
  [250] = "divide by zero",
  [260] = "domain error",
  [270] = "duplicate variable name",
  [280] = "Else and ElseIf invalid outside of If...EndIf block",
  [290] = "EndTry is missing the matching Else statement",
  [295] = "excessive iteration",
  [300] = "expected 2 or 3-element list or matrix",
  [310] = "the first argument of nSolve must be an equation in a single variable",
  [320] = "first argument of solve or cSolve must be an equation or inequality",
  [345] = "inconsistent units",
  [350] = "index out of range",
  [360] = "indirection string is not a valid variable name",
  [380] = "undefined Ans",
  [390] = "invalid assignment",
  [400] = "invalid assignment value",
  [410] = "invalid command",
  [430] = "invalid for the current mode settings",
  [435] = "invalid guess",
  [440] = "invalid implied multiply",
  [450] = "invalid in a function or current expression",
  [490] = "invalid in Try..EndTry block",
  [510] = "invalid list or matrix",
  [550] = "invalid outside function or program",
  [560] = "invalid outside Loop..EndLoop, For..EndFor, or While..EndWhile blocks",
  [565] = "invalid outside program",
  [570] = "invalid pathname",
  [575] = "invalid polar complex",
  [580] = "invalid program reference",
  [600] = "invalid table",
  [605] = "invalid use of units",
  [610] = "invalid variable name in a Local statement",
  [620] = "invalid variable or function name",
  [630] = "invalid variable reference",
  [640] = "invalid vector syntax",
  [650] = "link transmission",
  [665] = "matrix not diagonalizable",
  [670] = "low memory",
  [672] = "resource exhaustion",
  [673] = "resource exhaustion",
  [680] = "missing (",
  [690] = "missing )",
  [700] = "missing \"",
  [710] = "missing ]",
  [720] = "missing }",
  [730] = "missing start or end of block syntax",
  [740] = "missing Then in the If..EndIf block",
  [750] = "name is not a function or program",
  [765] = "no functions selected",
  [780] = "no solution found",
  [800] = "non-real result",
  [830] = "overflow",
  [850] = "program not found",
  [855] = "rand type functions not allowed in graphing",
  [860] = "recursion too deep",
  [870] = "reserved name or system variable",
  [900] = "argument error",
  [910] = "syntax error",
  [920] = "text not found",
  [930] = "too few arguments",
  [940] = "too many arguments",
  [950] = "too many subscripts",
  [955] = "too many undefined variables",
  [960] = "variable is not defined",
  [965] = "unlicensed OS",
  [970] = "variable in use so references or changes are not allowed",
  [980] = "variable is protected",
  [990] = "invalid variable name",
  [1000] = "window variables domain",
  [1010] = "zoom",
  [1020] = "internal error",
  [1030] = "protected memory violation",
  [1040] = "unsupported function (requires the CAS)",
  [1045] = "unsupported operator (requires the CAS)",
  [1050] = "unsupported feature (requires the CAS)",
  [1060] = "input argument must be numeric",
  [1070] = "trig function argument too big for accurate reduction",
  [1080] = "unsupported use of Ans",
  [1090] = "function is not defined",
  [1100] = "non-real calculation",
  [1110] = "invalid bounds",
  [1120] = "no sign change",
  [1130] = "argument cannot be a list or matrix",
  [1140] = "argument error",
  [1150] = "argument error",
  [1160] = "invalid library pathname",
  [1170] = "invalid use of library pathname",
  [1180] = "invalid library variable name",
  [1190] = "library document not found",
  [1200] = "library variable not found",
  [1210] = "invalid library shortcut name",
  [1220] = "domain error",
  [1230] = "domain error",
  [1250] = "argument error",
  [1260] = "argument error",
  [1270] = "argument error",
  [1280] = "argument error",
  [1290] = "argument error",
  [1300] = "argument error",
  [1310] = "argument error",
  [1380] = "argument error",
}

local state = {
  ticks = 0, served = 0, held = 0, resent = 0,
  id = "-", req = "-", res = "-", err = "-",
  cas = nil,
}

-- id -> its reply once evaluated (true while evaluating). Oldest dropped first.
local seen, seenOrder = {}, {}
local lastClip = nil

local function remember(id, reply)
  if seen[id] == nil then
    table.insert(seenOrder, id)
    if #seenOrder > MAX_SEEN then
      seen[table.remove(seenOrder, 1)] = nil
    end
  end
  seen[id] = reply
end

local function short(s, n)
  s = tostring(s)
  if #s > n then return s:sub(1, n) .. "..." end
  return s
end

local function esc(s)
  return (s:gsub("[%%;\r\n]", function(c) return string.format("%%%02X", c:byte()) end))
end

local function unesc(s)
  return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
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

-- Returns the reply body and the number of expressions run.
local function runBatch(body)
  local out = {}
  if body ~= "" then
    for item in (body .. ";"):gmatch("([^;]*);") do
      local ok, text = evaluate(unesc(item))
      out[#out + 1] = (ok and "O" or "E") .. esc(text)
    end
  end
  return "BATCH:" .. #out .. ":" .. table.concat(out, ";"), #out
end

-- Write reply only if the clipboard still holds request; otherwise keep it
-- for a resend. If the clipboard can't be read, write anyway (old behaviour).
local function deliver(reply, request)
  local okc, now = pcall(clipboard.getText)
  if okc and now ~= request then
    state.held = state.held + 1
    lastClip = nil  -- look at the clipboard again on the next tick
    return false
  end
  local okw, werr = pcall(clipboard.addText, reply)
  if not okw then
    state.err = "addText failed: " .. tostring(werr)
    lastClip = nil
    return false
  end
  lastClip = reply  -- so posting the same request again counts as a change
  return true
end

local function handle(id, expr, request)
  local ok, text
  if expr == PING then
    ok = true
    text = "PONG:" .. tostring(platform.apiLevel) .. ":" .. (detectCas() and "cas" or "nocas")
           .. ":" .. PROTOCOL
  elseif expr:sub(1, #BATCH) == BATCH then
    local n
    ok = true
    text, n = runBatch(expr:sub(#BATCH + 1))
    expr = "batch of " .. n
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
  remember(id, reply)
  deliver(reply, request)
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
  if id == nil then return end
  local done = seen[id]
  if done == nil then
    remember(id, true)
    local okh, herr = pcall(handle, id, expr, clip)
    if not okh then
      state.err = "lua: " .. tostring(herr)
      local reply = PREFIX_OUT .. id .. ":ERR:lua: " .. tostring(herr)
      remember(id, reply)
      deliver(reply, clip)
    end
  elseif done ~= true then
    -- Posted again: the requester's copy of the reply was overwritten.
    if deliver(done, clip) then state.resent = state.resent + 1 end
  end
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
  line("api " .. tostring(platform.apiLevel) .. "   " .. cas .. "   protocol " .. PROTOCOL
       .. "   held " .. state.held .. "   resent " .. state.resent)
  line("id:  " .. short(state.id, 30))
  line("req: " .. short(state.req, 40))
  line("res: " .. short(state.res, 40))
  line("err: " .. short(state.err, 40))
  line("Keep this page open. Don't copy while a request runs.")
end
