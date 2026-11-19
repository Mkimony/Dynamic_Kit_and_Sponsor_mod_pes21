-- KitGenerator.lua  (v0.7)
-- SP Football Life 2027 sponsor / kit module.
--
--   * Sponsor Office (executive card UI) lists this season's offers for your
--     club (pre-season only) and shows the active deal as a dashboard card
--     with a financial summary and a contract progress bar.
--   * Signing a deal:  rebuilds the home/away/third kits (new design + the
--     sponsor logo) through the background watcher, and credits the deal's
--     yearly value (EUR, 50M-150M) into the in-game budgets, split between the
--     transfer budget and the salary budget (see TRANSFER_SHARE below).  Any
--     signing bonus on the offer is paid once, at signing.
--   * A multi-season deal blocks any new offers until it ends, but a fresh kit
--     is generated for every season it runs.  When it ends the contract is
--     released (offers reopen) and the logo comes off the kit.  You can also
--     terminate early: an early-exit clause pays TERMINATION_PCT of the value
--     still to run as a buyout and takes the logo off at once.
--   * Two independent halves (CFG.ENABLE_KITS / CFG.ENABLE_SPONSORSHIP): use the
--     kit half alone, the sponsorship half alone, or both together.
--   * v0.7: the club you manage is AUTO-DETECTED.  Start a Master League with
--     any team and the mod follows it -- the club that turns up in every career
--     match wins, its kit colours are read live from the game
--     (ctx.kits.get) and written to content/KitGenerator/club.txt for the
--     Python generator, and offers are synthesised for that club when
--     deals.txt has none.  Set CFG.MANAGED_CLUB to a team id to override, or
--     type one in the Sponsor Office.
--
-- Written for Sider's LuaJIT sandbox: no pcall, require, select, next or print.
-- Uses the globals `ffi`, `memory` and `log`.
local m = { version = '0.7' }

-- ===== config =============================================================
-- Two independent halves -- set either off to use the other on its own:
--   ENABLE_KITS        the kit half: rebuilds the club's home/away/third kits
--                      each season through the background Python watcher.
--   ENABLE_SPONSORSHIP the deal half: the Sponsor Office (offers, signing) and
--                      crediting the deal's money into the in-game budgets.
-- Both on = the full mod.  Kits only = fresh kits each season, no office and no
-- money.  Sponsorship only = the office and the money, but the kits are left
-- exactly as they are (no watcher, no Python needed).
local CFG = {
  MANAGED_CLUB     = nil,        -- manual override: your club's team id, or nil to auto-detect
  ENABLE_KITS        = true,     -- rebuild the club's kits (needs the Python watcher)
  ENABLE_SPONSORSHIP = true,     -- Sponsor Office: offers, signing and budget money
  START_PROCESS    = true,       -- launch the kit watcher at game load (run setup_watcher.py once)
  OFFICE_KEY       = 0x76,       -- F7: classic text office (fallback)
  MODE_KEY         = 0x77,       -- F8: toggle 'Master League mode' by hand
  WINDOW_KEY       = 0x79,       -- F10: toggle 'transfer window open' by hand
  SIGNING_MONTHS   = {6, 7, 8},  -- PRE-SEASON only (Jun-Aug); Jan is deliberately excluded
  ALLOW_OFF_SEASON = false,      -- true bypasses the window check (testing only)
  CREDIT_BUDGET    = true,       -- credit signed deals into the in-game budgets
  TERMINATION_PCT  = 0.50,       -- early-exit clause: pay this share of the value still to run
  MONEY_UNIT       = 100,        -- budgets are stored as a u32 in units of EUR 100
  MAX_BUDGET       = 4294967295, -- u32 ceiling (~EUR 429bn; the format's real limit)
  SALARY_DELTA     = 0x14,       -- salary budget sits this far past the transfer budget
  TRANSFER_SHARE   = 0.70,       -- share of each payment added to the transfer budget
  SALARY_SHARE     = 0.30,       -- ...and to the salary budget (must sum to 1.0)
}
m.CFG = CFG

local STATE = { manual_ml = false, manual_window = false, month = nil, year = nil,
                date_ok = false,
                -- managed-club auto-detection (see the club section below)
                club = nil, club_source = nil, club_name = nil,
                colors = {},        -- team id -> kit colours read from the game
                votes = {},         -- team id -> times seen in a career match
                matches = 0 }       -- career matches seen since the game loaded
local OFFICE = { open = false, sel = 1 }
local SEL = { counter = nil, ask = 1, reply = nil, credit_msg = nil, rounds = {}, term_confirm = false }
local OFFERS, SIGNED, BRANDS = {}, {}, {}
local DEAL = nil                      -- active multi-season deal (or nil)
local REQ_SEASONS, PAID, WARNED, CLEAN = {}, {}, {}, {}

local DIR, CAREER, SEASON = '', 'unknown', 2027
local MONTH_NAMES = { 'Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec' }

local function L(msg) log('[KitGenerator] ' .. tostring(msg)) end

-- ===== ffi (WinExec / process id) =========================================
local ffi = ffi
if ffi and not ffi.kg_declared then
  ffi.cdef([[
    void* KG_Self(void) __asm__("GetCurrentProcess");
    unsigned int KG_WinExec(const char* cmd, unsigned int show) __asm__("WinExec");
    unsigned int KG_GetPid(void) __asm__("GetCurrentProcessId");
  ]])
  ffi.kg_declared = true
end

-- Master League can't be detected reliably yet, so the F8 toggle gates the
-- office (and signing).  Exhibition is always excluded.
local function mode_active(ctx)
  if ctx and ctx.tournament_id == 65535 then return false end
  return STATE.manual_ml
end

-- ===== small helpers ======================================================
-- All file paths go through here.  Sider hands us a Windows path, so we keep
-- the whole thing in backslashes: both Lua's io.open and the menu DLL's image
-- loader then see one unambiguous native path.
local function path(rel)
  return (DIR .. 'content/KitGenerator/' .. rel):gsub('/', '\\')
end

-- a path relative to SiderAddons itself (for files outside content/KitGenerator)
local function sider_path(rel)
  return (DIR .. rel):gsub('/', '\\')
end

-- ===== low-level memory helpers ===========================================
-- Sider can hand addresses back as cdata/userdata; normalise to a Lua number.
local function num_addr(x)
  if type(x) == 'number' then return x end
  if type(x) == 'cdata' or type(x) == 'userdata' then
    local n = tonumber(x)
    if n then return n end
    if memory and memory.hex then return tonumber(memory.hex(x)) end
  end
  return nil
end

local function mread_u32(addr)
  local s = memory.read(addr, 4)
  if type(s) ~= 'string' or #s ~= 4 then return nil end
  return tonumber(memory.unpack('u32', s))
end

-- read a validated 64-bit pointer stored at `addr`
local function read_ptr_at(addr)
  local s = memory.read(addr, 8)
  if type(s) ~= 'string' or #s ~= 8 then return nil end
  local v = tonumber(memory.unpack('u64', s))
  if not v or v < 0x10000 or v >= 0x800000000000 then return nil end
  return v
end

local function split(line)
  local t = {}
  for f in (line .. '|'):gmatch('([^|]*)|') do t[#t + 1] = f end
  return t
end

-- djb2: the deterministic roll shared with kg_core.py (plain arithmetic, no bit ops)
local function hash(s)
  local h = 5381
  for i = 1, #s do h = (h * 33 + s:byte(i)) % 4294967296 end
  return h
end

-- hash -> [0,1), for the offer synthesis (same roll every reload and season)
local function hash01(s) return (hash(s) % 10000) / 10000 end

local function fmt_money(v)
  v = tonumber(v) or 0
  if v >= 1e9 then return string.format('EUR %.2fB', v / 1e9)
  elseif v >= 1e6 then return string.format('EUR %.1fM', v / 1e6)
  elseif v >= 1e3 then return string.format('EUR %.0fk', v / 1e3) end
  return 'EUR ' .. tostring(math.floor(v))
end

-- ---- executive office presentation helpers -------------------------------
-- sponsor tier -> the label shown on the deal card
local TIER_LABEL = { small = 'Tier 3  -  KIT SPONSOR',
                     mid   = 'Tier 2  -  MAIN KIT SPONSOR',
                     big   = 'Tier 1  -  MAIN KIT SPONSOR' }
local function tier_label(t)
  return TIER_LABEL[t] or 'KIT SPONSOR'
end

-- ASCII progress bar, e.g. [==========              ]  (font-safe: no unicode)
local function progress_bar(cur, total, width)
  width = width or 24
  cur = tonumber(cur) or 0
  total = tonumber(total) or 1
  if total < 1 then total = 1 end
  if cur < 0 then cur = 0 end
  if cur > total then cur = total end
  local filled = math.floor(width * cur / total + 0.5)
  if filled > width then filled = width end
  return '[' .. string.rep('=', filled) .. string.rep(' ', width - filled) .. ']'
end

local function plural(n, word)
  n = tonumber(n) or 0
  return tostring(n) .. ' ' .. word .. (n == 1 and '' or 's')
end

local function append_line(rel, text)
  local f = io.open(path(rel), 'a')
  if not f then return false end
  f:write(text .. '\n'); f:flush(); f:close()
  return true
end

local function write_file(rel, text)
  local f = io.open(path(rel), 'w')
  if not f then return false end
  f:write(text); f:flush(); f:close()
  return true
end

local function read_lines(rel)
  local out, f = {}, io.open(path(rel), 'r')
  if not f then return out end
  for line in f:lines() do out[#out + 1] = line end
  f:close()
  return out
end

local function in_signing_window()
  if CFG.ALLOW_OFF_SEASON then return true end
  if STATE.manual_window then return true end
  if not STATE.month then return false end
  for _, mm in ipairs(CFG.SIGNING_MONTHS) do
    if STATE.month == mm then return true end
  end
  return false
end

-- ===== game date (the Master League calendar) =============================
-- Sider's set_teams / make_key context carries NO date (only tournament_id:
-- confirmed with a probe), so the date is read straight out of the game.  The
-- ML calendar is a YYYYMMDD u32 at [mode + 0x1642A24]; `mode` is the current
-- game-mode object, reached with a getter the game itself exposes:
--     48 8B 05 A9 F3 24 02 C3    ; mov rax,[rip+disp] ; ret
--   global_ptr = site + 7 + disp ; ctx = *global_ptr ; mode = *(ctx + 0x48)
-- (0x1642A24 is the same calendar offset fl26caps.lua patches, so this is the
-- real in-game date, not the wall clock.)
local DATE = { ready = false, global_ptr = nil, off = 0x1642A24, mode = nil }
local AOB_MODE = '\x48\x8B\x05\xA9\xF3\x24\x02\xC3'

local function install_date_reader()
  if DATE.ready then return true end
  if not (memory and memory.search_process and memory.read and memory.unpack) then return false end
  local site = num_addr(memory.search_process(AOB_MODE))
  if not site or site < 0x10000 then L('date: getter pattern not found'); return false end
  if memory.read(site, 8) ~= AOB_MODE then L('date: unexpected getter bytes'); return false end
  local disp = memory.unpack('i32', memory.read(site + 3, 4))
  if not disp then L('date: bad getter displacement'); return false end
  DATE.global_ptr, DATE.ready = site + 7 + disp, true
  L(string.format('date: reader ready (global ptr at 0x%X)', DATE.global_ptr))
  return true
end

local function read_mode()
  if not DATE.ready and not install_date_reader() then return nil end
  local ctx = read_ptr_at(DATE.global_ptr)
  if not ctx then return nil end
  return read_ptr_at(ctx + 0x48)
end

-- returns year, month, day, mode -- all nil when no career is loaded
local function game_date()
  local mode = read_mode()
  if not mode then return nil end
  DATE.mode = mode
  local v = mread_u32(mode + DATE.off)
  if not v or v < 19000101 or v > 22001231 then return nil end
  local y, mo, d = math.floor(v / 10000), math.floor(v / 100) % 100, v % 100
  if mo < 1 or mo > 12 or d < 1 or d > 31 then return nil end
  return y, mo, d, mode
end

-- ===== managed club: auto-detection =======================================
-- The mod used to hard-code the club you manage (CFG.MANAGED_CLUB = 102, i.e.
-- Chelsea).  It now works the club out on its own, so a career can be started
-- with ANY team.  The layers, most authoritative first:
--
--   1. CFG.MANAGED_CLUB       a manual override (a number) always wins
--   2. club.txt               the club detected in an earlier session
--   3. the career learner     the team that turns up in EVERY career match
--                             (your club plays all of them; no other club does)
--   4. edit mode              the team being edited, only when nothing else has
--                             been found yet
--
-- Detection needs the live context, so it runs from set_teams (career matches),
-- set_home_team_for_kits (edit mode) and whenever the Sponsor Office is drawn.
-- The club's own kit colours are read from the game with ctx.kits.get(team, 0)
-- and written to club.txt, where the Python generator picks them up and paints
-- the kits in the club's real colours.
local CLUB_FILE = 'club.txt'

local function club_id()
  if type(CFG.MANAGED_CLUB) == 'number' and CFG.MANAGED_CLUB > 0 then
    return math.floor(CFG.MANAGED_CLUB)
  end
  return STATE.club
end

local function club_source()
  if type(CFG.MANAGED_CLUB) == 'number' and CFG.MANAGED_CLUB > 0 then return 'manual' end
  return STATE.club_source or 'unknown'
end

local CLUB_SOURCE_LABEL = {
  manual = 'manual override', saved = 'saved from last session',
  career = 'auto-detected in career', edit = 'from edit mode',
  unknown = 'not detected yet',
}
local function club_source_label()
  return CLUB_SOURCE_LABEL[club_source()] or tostring(club_source())
end

-- The game's own team -> kit-folder map, used only to put a readable name on the
-- office card (the ids are what matter to the generator).
local MAP_NAMES = nil
local function club_display_name(cid)
  cid = tonumber(cid) or club_id()
  if not cid then return 'not detected' end
  if not MAP_NAMES then
    MAP_NAMES = {}
    local f = io.open(sider_path('content/kits/map.txt'), 'r')
    if f then
      for line in f:lines() do
        local clean = line:gsub('#.*', '')
        local tid, folder = clean:match('^%s*(%d+)%s*,%s*"([^"]*)"')
        if not tid then tid, folder = clean:match('^%s*(%d+)%s*,%s*(%S+)') end
        tid = tonumber(tid)
        if tid and folder and #folder > 0 then
          local name = folder:gsub('[\\/]+$', '')
          name = name:match('([^\\/]+)$') or name
          if #name > 0 then MAP_NAMES[tid] = name end
        end
      end
      f:close()
    end
  end
  return MAP_NAMES[cid] or ('Club #' .. tostring(cid))
end

-- read the club's kit colours straight out of the game.  ctx.kits.get answers
-- nil for teams the game has no licensed kit for (created / edited clubs), in
-- which case the Python resolver falls back to the kit map or a palette.
local function fetch_colors(ctx, team)
  team = tonumber(team)
  if not team or team <= 0 then return nil end
  local cached = STATE.colors[team]
  if cached then return cached end
  if not (ctx and ctx.kits and ctx.kits.get) then return nil end
  local k = ctx.kits.get(team, 0)
  if type(k) ~= 'table' then return nil end
  local c = { shirt1 = k.ShirtColor1, shirt2 = k.ShirtColor2, shorts = k.ShortsColor,
              socks = k.SocksColor, uni1 = k.UniColor_Color1, uni2 = k.UniColor_Color2 }
  if not (c.shirt1 or c.shirt2 or c.shorts or c.socks) then return nil end
  STATE.colors[team] = c
  return c
end

-- persist the detected club + its colours for the Python generator
local function save_club()
  local cid = club_id()
  if not cid then return false end
  local c = STATE.colors[cid] or {}
  return write_file(CLUB_FILE, string.format('%d|%s|%s|%s|%s|%s|%s|%s\n',
    cid, club_display_name(cid), c.shirt1 or '', c.shirt2 or '', c.shorts or '',
    c.socks or '', c.uni1 or '', c.uni2 or ''))
end

local function load_club_file()
  for _, line in ipairs(read_lines(CLUB_FILE)) do
    if #line > 0 and line:sub(1, 1) ~= '#' then
      local t = split(line)
      local cid = tonumber(t[1])
      if cid and cid > 0 then
        STATE.club, STATE.club_source = cid, 'saved'
        STATE.club_name = (t[2] and #t[2] > 0) and t[2] or nil
        if t[3] and #t[3] > 0 then
          STATE.colors[cid] = { shirt1 = t[3], shirt2 = t[4] or '', shorts = t[5] or '',
                                socks = t[6] or '', uni1 = t[7] or '', uni2 = t[8] or '' }
        end
        L('club.txt: managed club #' .. tostring(cid) ..
          (STATE.club_name and (' (' .. STATE.club_name .. ')') or ''))
      end
    end
  end
end

local function set_club(cid, source, persist)
  cid = tonumber(cid)
  if not cid or cid <= 0 then return false end
  cid = math.floor(cid)
  if STATE.club ~= cid or STATE.club_source ~= source then
    STATE.club, STATE.club_source = cid, source or 'manual'
    L(string.format('managed club -> #%d (%s)', cid, tostring(STATE.club_source)))
  end
  if persist then save_club() end
  return true
end

-- Sponsor Office: type a club id by hand; this becomes the manual override
local function set_manual_club(cid)
  cid = tonumber(cid)
  if not cid or cid <= 0 then return false end
  CFG.MANAGED_CLUB = math.floor(cid)
  STATE.club, STATE.club_source = CFG.MANAGED_CLUB, 'manual'
  save_club()
  L('manual club override: #' .. tostring(CFG.MANAGED_CLUB))
  return true
end

local function clear_manual_club()
  CFG.MANAGED_CLUB = nil
  STATE.club_source = STATE.club and 'saved' or nil
  L('manual club override cleared - auto-detecting again')
end

-- ---- the career learner ---------------------------------------------------
-- Your club plays every one of your career matches; no other club does.  So
-- whichever team appears in (nearly) every career match is the one you manage.
local function record_match(home, away)
  home, away = tonumber(home), tonumber(away)
  if not home or not away or home == away then return end
  STATE.votes[home] = (STATE.votes[home] or 0) + 1
  STATE.votes[away] = (STATE.votes[away] or 0) + 1
  STATE.matches = STATE.matches + 1
end

local function leader_team()
  local best, best_n, second_n = nil, 0, 0
  for t, n in pairs(STATE.votes) do
    if n > best_n then second_n, best, best_n = best_n, t, n
    elseif n > second_n then second_n = n end
  end
  return best, best_n, second_n
end

local function learn_club()
  if STATE.matches < 2 then return end
  local best, best_n, second_n = leader_team()
  if not best or best_n < 2 or best_n <= second_n then return end
  if STATE.club == best then return end
  if not STATE.club then
    set_club(best, 'career', true)
    return
  end
  local cur_n = STATE.votes[STATE.club] or 0
  -- a stale club.txt (the old club never plays) is replaced after a couple of
  -- matches, and a clear leader can also win out over a merely-possible one
  if (cur_n == 0 and STATE.matches >= 2) or (best_n >= 3 and best_n >= cur_n + 2) then
    set_club(best, 'career', true)
  end
end

local function detect_club(ctx)
  if type(CFG.MANAGED_CLUB) == 'number' and CFG.MANAGED_CLUB > 0 then
    if not STATE.colors[CFG.MANAGED_CLUB] and fetch_colors(ctx, CFG.MANAGED_CLUB) then
      save_club()
    end
    return club_id()
  end
  learn_club()
  local cid = club_id()
  if cid then
    local had = STATE.colors[cid]
    if fetch_colors(ctx, cid) and not had then save_club() end
  end
  return cid
end

-- ===== offers =============================================================
local function load_offers()
  OFFERS = {}
  local lines = read_lines('deals.txt')
  if #lines == 0 then L('deals.txt missing: Sponsor Office will be empty') end
  for _, line in ipairs(lines) do
    if line:sub(1, 1) ~= '#' and #line > 0 then
      local t = split(line)
      OFFERS[#OFFERS + 1] = { id = t[1], season = tonumber(t[2]), club = tonumber(t[3]), brand = t[4],
        logo = path(t[5]), value = tonumber(t[6]), years = tonumber(t[7]), bonus = tonumber(t[8]), tier = t[9] }
    end
  end
  for _, line in ipairs(read_lines('career.txt')) do
    if #line > 0 then CAREER = line end
  end
  L('loaded ' .. #OFFERS .. ' offers, career ' .. CAREER)
end

-- the brand pool, written by setup_watcher.py from the logos folder.  It is what
-- lets the mod offer deals to a club that deals.txt does not cover.
local function load_brands()
  BRANDS = {}
  for _, line in ipairs(read_lines('brands.txt')) do
    local b = line:gsub('^%s+', ''):gsub('%s+$', '')
    if #b > 0 and b:sub(1, 1) ~= '#' then BRANDS[#BRANDS + 1] = b end
  end
  if #BRANDS == 0 then
    L('brands.txt missing: clubs outside deals.txt cannot be offered a deal')
  end
end

-- Offers for a club deals.txt does not cover are synthesised here with the same
-- djb2 roll the rest of the mod uses, so they are identical on every reload and
-- every season.  Each season's five brands are a fresh window of the pool, five
-- on from the season before, so a club never sees the same brand two years
-- running (the rule deals.py enforces for the curated clubs).
local TIER_ORDER = { 'small', 'mid', 'big' }
local BASE_EUR = { small = 60000000, mid = 88000000, big = 135000000 }
local YEARS_PICK = { 1, 2, 2, 3 }
local BONUS_PICK = { 0, 5, 10 }

local function synth_offers(club, season, count)
  local out = {}
  local n = #BRANDS
  if n == 0 then return out end
  count = math.min(count or 5, n)
  local tier = TIER_ORDER[(hash('tier:' .. tostring(club)) % 3) + 1]
  local base = BASE_EUR[tier]
  local start = (hash('offers:' .. tostring(club)) + season * count) % n
  for i = 1, count do
    local key = tostring(club) .. ':' .. tostring(season) .. ':' .. tostring(i)
    local v = base * (0.85 + 0.30 * hash01('value:' .. key))
    out[#out + 1] = {
      id = string.format('%d-%d-%d', club, season, i),
      season = season, club = club,
      brand = BRANDS[((start + i - 1) % n) + 1],
      logo = path('logos/' .. BRANDS[((start + i - 1) % n) + 1] .. '.png'),
      value = math.floor(v / 100000 + 0.5) * 100000,
      years = YEARS_PICK[(hash('years:' .. key) % 4) + 1],
      bonus = BONUS_PICK[(hash('bonus:' .. key) % 3) + 1],
      tier = tier, synth = true,
    }
  end
  return out
end

local function visible()
  local out = {}
  local cid = club_id()
  if not cid then return out end
  if DEAL then return out end                 -- under contract: no new offers
  for _, o in ipairs(OFFERS) do
    if o.club == cid and o.season == SEASON then out[#out + 1] = o end
  end
  if #out == 0 then                           -- no curated set: make one
    out = synth_offers(cid, SEASON)
  end
  return out
end

-- ===== active deal (multi-season) =========================================
local function deal_end() return DEAL and (DEAL.start + DEAL.years - 1) or nil end

local function deal_active(season)
  if DEAL == nil then return false end
  local cid = club_id()
  if cid and DEAL.club ~= cid then return false end   -- a deal for another club
  return season >= DEAL.start and season <= deal_end()
end

local function save_deal()
  if not DEAL then write_file('active_deal.txt', ''); return end
  write_file('active_deal.txt', string.format('%s|%d|%s|%d|%d|%d|%s|%s|%d',
    DEAL.id, DEAL.club, DEAL.brand, DEAL.start, DEAL.years, DEAL.value,
    DEAL.tier or '', DEAL.signed_at or '', DEAL.bonus or 0))
end

local function load_deal()
  DEAL = nil
  local lines = read_lines('active_deal.txt')
  if #lines == 0 then return end
  local t = split(lines[1])
  local start, years, value = tonumber(t[4]), tonumber(t[5]), tonumber(t[6])
  if not (t[1] and t[3] and start and years) then return end
  DEAL = { id = t[1], club = tonumber(t[2]) or 0, brand = t[3], start = start,
           years = years, value = value or 0, tier = t[7] or '', signed_at = t[8] or '',
           bonus = tonumber(t[9]) or 0 }
  L('active deal: ' .. DEAL.brand .. ' (' .. tostring(start) .. '..' .. tostring(deal_end()) .. ')')
end

local function load_paid()
  PAID = {}
  for _, line in ipairs(read_lines('paid.txt')) do
    if #line > 0 then PAID[line] = true end
  end
end

-- ===== budget: capture the club-budget pointer, then credit ===============
-- The game reads the manager's transfer budget here:
--     mov eax,[rdi+016ECBF4]      ; 8B 87 F4 CB 6E 01
--     mov [rbp-3C],eax            ; 89 45 C4
-- `rdi` is the club budget block and the two budgets live inside it:
--     transfer = [rdi + 016ECBF4]   (this instruction)
--     salary   = [rdi + 016ECC08]   (= transfer + 0x14)
-- Both offsets come from xAranaktu's PES2021 / Football Life cheat table, which
-- uses this exact AOB and these exact offsets.
--
-- We detour the instruction into a code cave, stash `rdi`, run the stolen
-- bytes, then jump back with an ABSOLUTE (FF 25) jump.  A Sider code cave is
-- not guaranteed to sit within the +/-2 GB reach of a rel32 jump, so a rel32
-- detour can silently corrupt the flow (crash).  The detour steals 17 bytes
-- (a clean instruction boundary) so the 14-byte absolute jump fits.
local BUDGET = { ready = false, slot = nil, disp = nil, salary_disp = nil }
local AOB_BUDGET = '\x8B\x87\xF4\xCB\x6E\x01\x89\x45\xC4'
-- the 8 bytes that follow the AOB (two `lea` instructions), completing the
-- 17-byte window we steal and then replay inside the cave
local AOB_BUDGET_TAIL = '\x48\x8D\x55\xC4\x48\x8D\x4D\xD0'
local BUDGET_STEAL = 17

local function install_budget_hook()
  if BUDGET.ready then return true end
  if not (memory and memory.search_process and memory.allocate_codecave and memory.read) then
    L('budget: Sider memory API unavailable'); return false
  end
  local site = num_addr(memory.search_process(AOB_BUDGET))
  if not site or site < 0x10000 then
    L('budget: hook pattern not found - crediting disabled'); return false
  end
  local original = AOB_BUDGET .. AOB_BUDGET_TAIL
  if memory.read(site, BUDGET_STEAL) ~= original then
    L('budget: unexpected bytes at hook site - crediting disabled'); return false
  end
  -- disp32 sits at offset 2 (no REX prefix): 8B 87 <disp32> 89 45 C4
  local disp = memory.unpack('i32', memory.read(site + 2, 4))
  if not disp or disp <= 0 then L('budget: bad displacement'); return false end
  if memory.read(site, 6) ~= '\x8B\x87' .. memory.pack('i32', disp) then
    L('budget: displacement does not re-encode - crediting disabled'); return false
  end
  local cave = num_addr(memory.allocate_codecave(96))
  if not cave or cave < 0x10000 then L('budget: code cave allocation failed'); return false end
  local slot = 0x40
  memory.write(cave + slot, memory.pack('u64', 0))
  -- mov [rip+disp32], rdi            (stash rdi at cave+slot)
  local code = '\x48\x89\x3D' .. memory.pack('i32', slot - 7)
  -- replay the 17 stolen bytes
  code = code .. original
  -- absolute jump back to the instruction after the stolen window
  code = code .. '\xFF\x25\x00\x00\x00\x00' .. memory.pack('u64', site + BUDGET_STEAL)
  memory.write(cave, code)
  if memory.read(cave, #code) ~= code then
    L('budget: cave verification failed - crediting disabled'); return false
  end
  -- detour: jmp qword ptr [rip+0] ; dq cave ; nop*n
  local patch = '\xFF\x25\x00\x00\x00\x00' .. memory.pack('u64', cave) ..
    string.rep('\x90', BUDGET_STEAL - 14)
  memory.write(site, patch)
  if memory.read(site, BUDGET_STEAL) ~= patch then
    memory.write(site, original)
    L('budget: patch verification failed - original restored'); return false
  end
  BUDGET.ready, BUDGET.slot = true, cave + slot
  BUDGET.disp, BUDGET.salary_disp = disp, disp + CFG.SALARY_DELTA
  L(string.format('budget hook installed (transfer +0x%X, salary +0x%X)',
    BUDGET.disp, BUDGET.salary_disp))
  return true
end

-- the exact club-budget pointer the game used, once the hook has fired
local function budget_ptr()
  if not BUDGET.ready then return nil end
  return read_ptr_at(BUDGET.slot)
end

-- add `units` (EUR100) to one u32 budget field; roll back if the write does
-- not read back.  Returns ok, err, before, after.
local function credit_field(addr, units, label)
  local before = mread_u32(addr)
  if not before or before == 0xFFFFFFFF or before == 0xCCCCCCCC then
    return false, label .. ' budget unreadable/implausible', before
  end
  local after = before + units
  if after > CFG.MAX_BUDGET then after = CFG.MAX_BUDGET end
  if after < 0 then after = 0 end
  memory.write(addr, memory.pack('u32', after))
  if mread_u32(addr) ~= after then
    memory.write(addr, memory.pack('u32', before))
    return false, label .. ' budget read-back mismatch', before
  end
  return true, nil, before, after
end

local function credit_budget(euros)
  if not CFG.CREDIT_BUDGET then return false, 'crediting disabled' end
  if not install_budget_hook() then return false, 'budget hook unavailable' end
  local career = budget_ptr()
  if not career then
    local m = DATE.mode
    if m then
      return false, string.format('pointer not captured yet (mode=0x%X; open the budget/Negotiations screen once)', m)
    end
    return false, 'pointer not captured yet (open the budget/Negotiations screen once)'
  end
  euros = tonumber(euros) or 0
  if euros == 0 then return false, 'nothing to credit' end
  if DATE.mode and not BUDGET.checked then
    BUDGET.checked = true
    L(string.format('budget: rdi=0x%X  mode=0x%X  same=%s  transfer@rdi=%s  salary@rdi=%s',
      career, DATE.mode, tostring(career == DATE.mode),
      tostring(mread_u32(career + BUDGET.disp)),
      tostring(mread_u32(career + BUDGET.salary_disp))))
  end
  -- split the payment: TRANSFER_SHARE to the transfer budget, the rest to the
  -- salary budget (default 70% / 30%).  Each share is rounded to EUR100.
  local t_euros = math.floor(euros * CFG.TRANSFER_SHARE / CFG.MONEY_UNIT + 0.5) * CFG.MONEY_UNIT
  local s_euros = math.floor((euros - t_euros) / CFG.MONEY_UNIT + 0.5) * CFG.MONEY_UNIT
  local t_units = math.floor(t_euros / CFG.MONEY_UNIT)
  local s_units = math.floor(s_euros / CFG.MONEY_UNIT)
  if t_units == 0 and s_units == 0 then return false, 'nothing to move' end
  local parts = {}
  if t_units ~= 0 then
    local ok, why, before, after = credit_field(career + BUDGET.disp, t_units, 'transfer')
    if not ok then return false, why end
    parts[#parts + 1] = string.format('transfer %s -> %s', fmt_money(before * CFG.MONEY_UNIT), fmt_money(after * CFG.MONEY_UNIT))
  end
  if s_units ~= 0 then
    local ok, why, before, after = credit_field(career + BUDGET.salary_disp, s_units, 'salary')
    if not ok then return false, why end
    parts[#parts + 1] = string.format('salary %s -> %s', fmt_money(before * CFG.MONEY_UNIT), fmt_money(after * CFG.MONEY_UNIT))
  end
  return true, string.format('%s %s (%d%% transfer / %d%% salary): %s',
    euros > 0 and 'credited' or 'debited', fmt_money(math.abs(euros)),
    math.floor(CFG.TRANSFER_SHARE * 100 + 0.5), math.floor(CFG.SALARY_SHARE * 100 + 0.5),
    table.concat(parts, ', '))
end

-- ===== per-season deal upkeep =============================================
-- Called whenever the season is known/changes.  While a deal runs it asks the
-- watcher for a fresh kit (unique id per season, so it always rebuilds) and
-- credits that season's money exactly once.
-- the paid.txt ledger key for one season of the active deal (one payment each)
local function paid_key(season)
  return string.format('%s:%d:%d:%s', CAREER, DEAL.club, season, DEAL.brand)
end

local function ensure_season_deal(season)
  -- Never act on a guessed season: at game load the calendar is not readable
  -- yet and SEASON would be the default, which would wrongly revert an active
  -- deal's kit to the clean one.  Wait until a real in-game date has been read.
  if not STATE.date_ok then return end
  local cid = club_id()

  -- A deal that belongs to a DIFFERENT club (you started a new career) must not
  -- carry over: release it and strip its logo from the old club's kit.
  if DEAL and cid and DEAL.club ~= cid then
    local old, brand = DEAL.club, DEAL.brand
    DEAL = nil
    REQ_SEASONS = {}
    save_deal()
    L('deal with ' .. tostring(brand) .. ' belongs to club #' .. tostring(old) ..
      ' - released (now following #' .. tostring(cid) .. ')')
    if CFG.ENABLE_KITS then
      append_line('kit_request.txt', string.format('clean-%d-%d-t%s|%d|%d||%s',
        old, season, os.date('%Y%m%d%H%M%S'), old, season, os.date('%Y-%m-%d %H:%M:%S')))
    end
  end

  -- A contract that has run out must be released.  Otherwise `visible()` would
  -- keep hiding every offer (it returns nothing while DEAL is set) and the club
  -- could never sign a new sponsor again.  Persisted so it stays released.
  if DEAL and season > deal_end() then
    local brand, ended = DEAL.brand, deal_end()
    DEAL = nil
    REQ_SEASONS = {}
    save_deal()                       -- blanks active_deal.txt
    L('deal expired: ' .. tostring(brand) .. ' ended after season ' .. tostring(ended) ..
      ' - offers are open again')
  end

  if not cid then return end          -- club not detected yet: nothing to build

  if not deal_active(season) then
    -- No deal for this season: make sure the club is back in its clean,
    -- sponsor-less kit.  Without this, a deal that has just expired would leave
    -- its logo on the shirts until the store is rebuilt.
    -- One request per club and season; the watcher dedupes by request id.
    -- Kits-only mode lands here every season, so the club gets a fresh kit
    -- each year.
    local ckey = cid .. ':' .. season
    if CFG.ENABLE_KITS and not CLEAN[ckey] then
      CLEAN[ckey] = true
      append_line('kit_request.txt', string.format('clean-%d-%d|%d|%d||%s',
        cid, season, cid, season, os.date('%Y-%m-%d %H:%M:%S')))
      L('no deal for season ' .. tostring(season) ..
        ': requested the clean kit for club #' .. tostring(cid))
    end
    return
  end
  local rkey = DEAL.club .. ':' .. season
  if CFG.ENABLE_KITS and not REQ_SEASONS[rkey] then
    REQ_SEASONS[rkey] = true
    append_line('kit_request.txt', string.format('rebuild-%d-%s-%d|%d|%d|%s|%s|%s',
      DEAL.club, DEAL.brand, season, DEAL.club, season, DEAL.brand, DEAL.tier or '',
      os.date('%Y-%m-%d %H:%M:%S')))
    L('deal active: requested a fresh kit for season ' .. tostring(season))
  end
  if not CFG.ENABLE_SPONSORSHIP then return end
  local key = paid_key(season)
  if not PAID[key] then
    local ok, why = credit_budget(DEAL.value)
    if ok then
      PAID[key] = true
      append_line('paid.txt', key)
      L('budget: ' .. tostring(why))
    elseif not WARNED[season] then
      WARNED[season] = true
      L('budget credit deferred (' .. key .. '): ' .. tostring(why))
    end
  end
end

-- Refresh the in-game date/season from the ML calendar.  The season is named
-- for the year it ENDS in (Football Life 2027 == the 2026/27 season) and rolls
-- over in June, when the pre-season (Jun-Aug) opens.
local function refresh_date(ctx)
  local y, mo, d = game_date()
  if y then
    STATE.year, STATE.month, STATE.day = y, mo, d
    STATE.date_ok = true
    local prev = SEASON
    SEASON = (mo >= 6) and (y + 1) or y
    if prev ~= SEASON then
      L(string.format('season %d  (game date %04d-%02d-%02d)', SEASON, y, mo, d))
    end
  end
  if ctx then detect_club(ctx) end
  ensure_season_deal(SEASON)
end

-- ===== counter-offer rule (must equal kg_core.py) ========================
local function roll(career, season, offer_id, step)
  return (hash(career .. ':' .. season .. ':' .. offer_id .. ':' .. step) % 10000) / 10000
end
local TIER = { small = 0.8, mid = 1.0, big = 1.2 }
local ASKS = { 'Higher yearly value', 'Longer deal', 'Bigger bonus', 'Budget split' }
local ASK_PCT = { 0.15, 0.08, 0.20, 0.0 }
function m.counter_reply(career, season, offer_id, tier, ask_pct, step)
  local p = (0.70 - 2.0 * ask_pct - 0.15 * (step - 1)) * (TIER[tier] or 1.0)
  p = math.max(0.05, math.min(0.90, p))
  local r = roll(career, season, offer_id, step)
  if r < p then return 'accept' elseif r < p + 0.30 then return 'halfway' else return 'withdraw' end
end

-- ===== actions =============================================================
local function sign_offer(o)
  if SIGNED[o.id] then return end
  if DEAL then L('signing blocked: a deal with ' .. tostring(DEAL.brand) .. ' is already active'); return end
  if not in_signing_window() then
    local mn = STATE.month and MONTH_NAMES[STATE.month] or 'unknown'
    L('signing blocked: outside transfer window (current month: ' .. mn .. ')')
    return
  end
  SIGNED[o.id] = true
  append_line('signed.txt', string.format('%s|%s|%s', o.id, CAREER, os.date('%Y-%m-%d %H:%M:%S')))
  DEAL = { id = o.id, club = o.club, brand = o.brand, start = o.season, years = o.years or 1,
           value = o.value or 0, tier = o.tier, signed_at = os.date('%Y-%m-%d %H:%M:%S'),
           bonus = o.bonus or 0 }
  REQ_SEASONS = {}
  save_deal()
  L('signed ' .. o.id .. ' (' .. tostring(o.brand) .. ') for ' .. tostring(DEAL.years) ..
    ' season(s), ' .. fmt_money(DEAL.value) .. '/yr' ..
    ((DEAL.bonus > 0) and (', ' .. DEAL.bonus .. '% signing bonus') or ''))
  ensure_season_deal(SEASON)
  -- the signing bonus is a one-off, separate from the yearly value; ledger it
  -- under its own key so it can never be paid twice
  if CFG.ENABLE_SPONSORSHIP and CFG.CREDIT_BUDGET and (DEAL.bonus or 0) > 0 then
    local bonus = math.floor(DEAL.value * DEAL.bonus / 100 / CFG.MONEY_UNIT + 0.5) * CFG.MONEY_UNIT
    local bkey = paid_key(DEAL.start) .. ':bonus'
    if bonus > 0 and not PAID[bkey] then
      local ok, why = credit_budget(bonus)
      if ok then
        PAID[bkey] = true
        append_line('paid.txt', bkey)
        L('signing bonus (' .. DEAL.bonus .. '%): ' .. tostring(why))
      else
        L('signing bonus deferred: ' .. tostring(why))
      end
    end
  end
end

local function send_counter(o)
  local n = (SEL.rounds[o.id] or 0)
  if n >= 2 then SEL.reply = 'no more counters on this offer'; return end
  n = n + 1; SEL.rounds[o.id] = n
  SEL.reply = m.counter_reply(CAREER, o.season, o.id, o.tier, ASK_PCT[SEL.ask], n)
end

local function credit_now()
  if not DEAL then SEL.credit_msg = 'no active deal'; return end
  -- the office button is a retry for when the automatic credit had to defer;
  -- it must never pay the same season twice
  local key = paid_key(SEASON)
  if PAID[key] then
    SEL.credit_msg = 'already credited for season ' .. tostring(SEASON)
    L('manual credit skipped: ' .. SEL.credit_msg)
    return
  end
  local ok, why = credit_budget(DEAL.value)
  if ok then
    PAID[key] = true
    append_line('paid.txt', key)
  end
  SEL.credit_msg = (ok and 'OK: ' or 'FAILED: ') .. tostring(why)
  L('manual credit: ' .. SEL.credit_msg)
end

-- early-exit clause: end the deal now, pay a share of the value still to run
-- (CFG.TERMINATION_PCT) as a buyout, and take the sponsor off the kit at once
local function terminate_deal()
  if not DEAL then SEL.credit_msg = 'no active deal'; return end
  local season = SEASON
  local last = deal_end()
  local remaining = (last and last >= season) and (last - season + 1) or 0
  local buyout = math.floor(DEAL.value * CFG.TERMINATION_PCT * remaining / CFG.MONEY_UNIT + 0.5) * CFG.MONEY_UNIT
  local note = ''
  if CFG.ENABLE_SPONSORSHIP and CFG.CREDIT_BUDGET and buyout > 0 then
    local ok, why = credit_budget(-buyout)
    note = (ok and 'buyout paid (' or 'buyout failed (') .. tostring(why) .. ')'
    L('terminate: ' .. note)
  end
  local brand = DEAL.brand
  local club = DEAL.club
  DEAL = nil
  REQ_SEASONS = {}
  save_deal()                       -- blanks active_deal.txt
  if CFG.ENABLE_KITS then
    CLEAN[club .. ':' .. season] = true   -- one clean rebuild now, logo off the shirts
    -- a UNIQUE id: the season's earlier clean request (made before signing) may
    -- already be in the watcher's done-list, which dedupes by id
    append_line('kit_request.txt', string.format('clean-%d-%d-t%s|%d|%d||%s',
      club, season, os.date('%Y%m%d%H%M%S'), club, season,
      os.date('%Y-%m-%d %H:%M:%S')))
  end
  SEL.credit_msg = 'terminated ' .. tostring(brand) .. (note ~= '' and ('; ' .. note) or '')
  L('terminated deal with ' .. tostring(brand) .. (note ~= '' and (' (' .. note .. ')') or ''))
end

-- ===== Sider Mod Menu page (native) =======================================
local function build_office(ui, ctx)
  refresh_date(ctx)

  -- ---- Mode card: the two switches side by side --------------------------
  ui:section('Mode', 'settings', 'Master League & transfer window')
  ui:cols(0.5)
  ui:toggle('ml_mode', 'Master League mode', STATE.manual_ml,
    function(on) STATE.manual_ml = on; L('manual Master League mode: ' .. tostring(on)) end,
    'Enable this when you are in Master League')
  ui:nextcol()
  ui:toggle('window', 'Transfer window open', STATE.manual_window,
    function(on) STATE.manual_window = on; L('manual transfer window: ' .. tostring(on)) end,
    'Enable only during the pre-season (Jun-Aug)')
  ui:endcols()

  if not (STATE.manual_ml or (ctx and ctx.tournament_id ~= 65535)) then
    ui:message('Master League mode is off. Toggle it above to see offers.', 'warn'); return
  end

  -- ---- Club card: which club the mod is following ------------------------
  local cid = detect_club(ctx)
  ui:section('Club', 'tag', 'Followed automatically from your career')
  ui:cols(0.5)
  ui:kv('Club', cid and club_display_name(cid) or 'not detected yet', cid and 'accent' or 'warn')
  ui:kv('Team id', cid and ('#' .. tostring(cid)) or '-')
  ui:nextcol()
  ui:kv('Detected by', club_source_label())
  ui:kv('Career matches seen', tostring(STATE.matches))
  ui:endcols()
  ui:input('clubid', 'Set the club id by hand (optional)', 'team id, e.g. 102',
    function(text, submitted)
      if not submitted then return end
      local n = tonumber((tostring(text or ''):gsub('%s', '')))
      if n and n > 0 then set_manual_club(n) end
    end)
  if club_source() == 'manual' then
    ui:button('auto', 'Back to auto-detect', function() clear_manual_club() end, 'ghost', 'edit')
  end
  if not cid then
    ui:message('Club not detected yet. Play a career match, or type your team id above.', 'warn')
    return
  end

  -- ---- Executive sponsorship card ----------------------------------------
  if DEAL then
    local seasons = DEAL.years or 1
    local last = deal_end() or DEAL.start
    local elapsed = math.max(1, math.min(seasons, SEASON - DEAL.start + 1))
    local bonus = math.floor(DEAL.value * (DEAL.bonus or 0) / 100 / CFG.MONEY_UNIT + 0.5) * CFG.MONEY_UNIT
    local pct = math.floor(100 * elapsed / seasons + 0.5)

    ui:section('Active Sponsorship', 'tag', 'Season ' .. tostring(SEASON))

    ui:cols(0.32)
    ui:image(DEAL.logo or path('logos/' .. DEAL.brand .. '.png'), nil, 64)
    ui:nextcol()
    ui:text(string.upper(tostring(DEAL.brand)), 'bold')
    ui:text(tier_label(DEAL.tier), 'small')
    ui:kv('Yearly value', fmt_money(DEAL.value), 'accent')
    ui:endcols()

    ui:space(4)
    ui:cols(0.34)
    ui:kv('Contract', tostring(DEAL.start) .. ' - ' .. tostring(last))
    ui:kv('Length', plural(seasons, 'season'))
    ui:nextcol()
    ui:kv('Signing bonus', (DEAL.bonus or 0) > 0 and (tostring(DEAL.bonus) .. '%  (' .. fmt_money(bonus) .. ')') or 'none')
    ui:kv('Kit', 'rebuilt each season')
    ui:nextcol()
    ui:kv('Status', 'Active', 'ok')
    ui:kv('Season', 'year ' .. tostring(elapsed) .. ' of ' .. tostring(seasons))
    ui:endcols()

    ui:space(4)
    ui:kv('Contract progress', progress_bar(elapsed, seasons) .. '  ' .. tostring(pct) .. '%')

    ui:message('Under contract. No new offers until the deal ends; a new kit is built each season.', 'info')
    ui:row()
    ui:button('credit', 'Credit this season\'s money now', function() credit_now() end, 'primary', 'check')
    ui:button('term', SEL.term_confirm and 'Confirm termination' or 'Terminate contract',
      function()
        if SEL.term_confirm then SEL.term_confirm = false; terminate_deal()
        else SEL.term_confirm = true end
      end, 'ghost', 'edit')
    ui:endrow()
    if SEL.term_confirm then
      ui:message('Press again to confirm. The deal ends now, a buyout (' ..
        math.floor(CFG.TERMINATION_PCT * 100 + 0.5) .. '% of the value still to run) is taken ' ..
        'from your budget, and the logo comes off the kit.', 'warn')
    end
    if SEL.credit_msg then ui:message(SEL.credit_msg, 'info') end
    return
  end

  -- ---- Offers ------------------------------------------------------------
  local list = visible()
  local mn = STATE.month and MONTH_NAMES[STATE.month] or 'unknown'
  local window_open = in_signing_window()

  ui:section('Sponsor Offers', 'trophy', 'Season ' .. tostring(SEASON) .. '  -  ' .. plural(#list, 'offer'))
  if window_open then
    ui:message('Transfer window OPEN (' .. mn .. '). You may sign deals now.', 'ok')
  else
    local mn_list = {}
    for _, mm in ipairs(CFG.SIGNING_MONTHS) do mn_list[#mn_list + 1] = MONTH_NAMES[mm] end
    ui:message('Transfer window CLOSED (' .. mn .. '). Signing is allowed only in ' ..
      table.concat(mn_list, ', ') .. '.', 'warn')
  end
  if #list == 0 then
    ui:text('No offers available for your club this season.', 'muted')
  end

  for i, o in ipairs(list) do
    ui:cols(0.28)
    ui:image(o.logo, nil, 48)
    ui:nextcol()
    ui:text(string.upper(tostring(o.brand)), 'bold')
    ui:text(tier_label(o.tier), 'small')
    ui:kv('Per year', fmt_money(o.value), 'accent')
    ui:endcols()
    ui:kv('Length', plural(o.years, 'season'))
    ui:kv('Signing bonus', (o.bonus or 0) > 0 and (tostring(o.bonus) .. '%') or 'none')
    ui:row()
    ui:button('sign' .. i, SIGNED[o.id] and 'Signed' or 'Sign deal', function() sign_offer(o) end,
      'primary', 'check', not SIGNED[o.id])
    ui:button('ctr' .. i, 'Counter offer', function() SEL.counter = i; SEL.reply = nil end, 'ghost', 'edit')
    ui:endrow()
    if i < #list then ui:separator() end
  end

  local o = list[SEL.counter]
  if o then
    ui:section('Counter: ' .. string.upper(tostring(o.brand)), 'edit', 'Ask the sponsor to improve the terms')
    ui:choice('ask', 'Ask for', ASKS, SEL.ask, function(i) SEL.ask = i end)
    ui:button('send', 'Send counter offer', function() send_counter(o) end, 'primary', 'play')
    if SEL.reply then ui:message('Sponsor answer: ' .. SEL.reply, 'info') end
  end
end

-- ===== classic overlay fallback (text + keys) ==============================
local function pad(s, n)
  s = tostring(s)
  if #s >= n then return s:sub(1, n) end
  return s .. string.rep(' ', n - #s)
end

function m.overlay_on(ctx)
  if not OFFICE.open then return '' end
  refresh_date(ctx)
  local mn = STATE.month and MONTH_NAMES[STATE.month] or 'unknown'
  local win_open = in_signing_window()
  local club_name = club_display_name(club_id())
  local bar = string.rep('=', 78)
  local sep = string.rep('-', 78)

  local lines = {}
  lines[#lines + 1] = bar
  lines[#lines + 1] = '  SPONSOR OFFICE  -  ' .. club_name .. '  -  Season ' .. tostring(SEASON)
  lines[#lines + 1] = '  Window: [' .. (win_open and 'OPEN' or 'CLOSED') .. ' - ' .. mn .. ']'
  lines[#lines + 1] = bar

  if DEAL then
    lines[#lines + 1] = '  ACTIVE DEAL: ' .. DEAL.brand .. '   ' .. fmt_money(DEAL.value) .. '/yr'
    lines[#lines + 1] = '  Runs: season ' .. tostring(DEAL.start) .. ' - ' .. tostring(deal_end())
    if (DEAL.bonus or 0) > 0 then
      lines[#lines + 1] = '  Signing bonus: ' .. tostring(DEAL.bonus) .. '% (' ..
        fmt_money(math.floor(DEAL.value * DEAL.bonus / 100 / CFG.MONEY_UNIT + 0.5) * CFG.MONEY_UNIT) .. ')'
    end
    lines[#lines + 1] = '  No new offers while the deal runs; a new kit is built each season.'
    lines[#lines + 1] = sep
    lines[#lines + 1] = '  [C] credit this season\'s money  [T] terminate contract  [Esc] close'
    lines[#lines + 1] = bar
    return table.concat(lines, '\n')
  end

  lines[#lines + 1] = '  ' .. pad('BRAND', 16) .. pad('VALUE/YR', 16) .. pad('LENGTH', 10) .. pad('BONUS', 10) .. 'STATUS'
  lines[#lines + 1] = sep
  local list = visible()
  if #list == 0 then
    lines[#lines + 1] = '  (no offers available for this club)'
  else
    for i, o in ipairs(list) do
      local marker = (i == OFFICE.sel) and '>>' or '  '
      local status = SIGNED[o.id] and 'SIGNED' or 'available'
      lines[#lines + 1] = marker .. ' ' .. pad(o.brand, 14) .. pad(fmt_money(o.value), 16) ..
        pad(tostring(o.years) .. 'y', 10) .. pad(tostring(o.bonus) .. '%', 10) .. status
    end
  end
  lines[#lines + 1] = sep
  if not win_open then
    local mn_list = {}
    for _, mm in ipairs(CFG.SIGNING_MONTHS) do mn_list[#mn_list + 1] = MONTH_NAMES[mm] end
    lines[#lines + 1] = '  Signing allowed only in the pre-season: ' .. table.concat(mn_list, ', ')
    lines[#lines + 1] = '  Press F10 to manually confirm transfer window'
  else
    lines[#lines + 1] = '  Press Enter to sign the highlighted offer'
  end
  lines[#lines + 1] = '  [Up/Down] select  [Enter] sign  [F10] window toggle  [Esc] close'
  lines[#lines + 1] = bar
  return table.concat(lines, '\n')
end

function m.key_down(ctx, vk)
  local handled = false
  if vk == CFG.MODE_KEY then
    STATE.manual_ml = not STATE.manual_ml
    L('manual Master League mode: ' .. tostring(STATE.manual_ml))
    handled = true
  elseif vk == CFG.WINDOW_KEY then
    STATE.manual_window = not STATE.manual_window
    L('manual transfer window: ' .. tostring(STATE.manual_window))
    handled = true
  elseif vk == CFG.OFFICE_KEY then
    OFFICE.open = not OFFICE.open
    handled = true
  elseif OFFICE.open then
    if vk == 0x26 then OFFICE.sel = math.max(1, OFFICE.sel - 1); handled = true
    elseif vk == 0x28 then OFFICE.sel = math.min(math.max(1, #visible()), OFFICE.sel + 1); handled = true
    elseif vk == 0x0D and visible()[OFFICE.sel] then sign_offer(visible()[OFFICE.sel]); handled = true
    elseif vk == 0x43 and DEAL then credit_now(); handled = true
    elseif vk == 0x54 and DEAL then           -- [T] terminate (press twice)
      if SEL.term_confirm then SEL.term_confirm = false; terminate_deal()
      else SEL.term_confirm = true; L('press T again to confirm termination') end
      handled = true
    elseif vk == 0x1B then OFFICE.open = false; SEL.term_confirm = false; handled = true end
  end
  if handled and ctx then ctx.consumed = true end
end

-- Every match the game sets up tells us which two clubs are playing.  A career
-- fixture is the strong signal (your club plays them all); a friendly
-- (tournament_id 65535) is ignored so exhibition opponents cannot muddy the
-- vote.  We also grab both sides' kit colours while the game is holding them.
function m.set_teams(ctx, home, away)
  local career = not (ctx and ctx.tournament_id == 65535)
  if career then
    record_match(home, away)
    fetch_colors(ctx, home)
    fetch_colors(ctx, away)
  end
  refresh_date(ctx)
end

-- Edit mode is the last-resort signal: only when no club has been found any
-- other way do we assume the team being edited is the one you manage.
function m.set_home_team_for_kits(ctx, team_id, edit_mode)
  if edit_mode == 1 and not club_id() then set_club(team_id, 'edit', true) end
  fetch_colors(ctx, team_id)
  refresh_date(ctx)
end

-- ===== generated kits: served ONLY while a career (Master League) is loaded ==
-- The generated .ftex set lives in a private store,
--     <sider>\content\kits\_KitGenerator\u<team>p<slot>.ftex
-- and is handed to Sider through the livecpk hooks below.  That means the game's
-- own kits load untouched in exhibition / edit / every non-career mode, and the
-- generated kit appears only in the Master League.
local KIT_STORE = 'content/kits/_KitGenerator/'

-- the generated file name for a team + slot, or nil when it has not been built
local function generated_kit(cid, slot)
  local name = string.format('u%04dp%d.ftex', cid, slot)
  local f = io.open(sider_path(KIT_STORE .. name), 'rb')
  if f then f:close(); return name end
  return nil
end

-- True only while the Master League career is loaded: the ML calendar is
-- readable AND the game is not in an exhibition (tournament_id 65535).
local function career_loaded(ctx)
  if ctx and ctx.tournament_id == 65535 then return false end
  local y = game_date()
  return y ~= nil
end

function m.make_key(ctx, filename)
  -- hot path: called for every file Sider loads, so no side effects here -- the
  -- date/club refresh already runs from set_teams / overlay_on / init.
  if not CFG.ENABLE_KITS then return nil end
  if not career_loaded(ctx) then return nil end
  local cid = club_id()
  if not cid then return nil end
  local fn = tostring(filename)
  local team, slot = string.match(fn, '[uU](%d+)[pP]([123])%.ftex')
  if not team or tonumber(team) ~= cid then return nil end
  local name = generated_kit(cid, tonumber(slot))
  if not name then return nil end
  return '_KitGenerator\\' .. name
end

-- Resolve our keys to the private store.  (common/kits.lua's handler resolves
-- them the same way -- kroot .. key -- so the result is identical whichever
-- handler Sider picks first.)
function m.get_filepath(ctx, filename, key)
  if type(key) == 'string' and key:sub(1, 13) == '_KitGenerator' then
    return sider_path('content/kits/' .. key)
  end
  return nil
end

-- ===== background kit watcher =============================================
local WATCHER_STARTED = false
local function start_watcher()
  if WATCHER_STARTED or not CFG.START_PROCESS then return end
  if not (ffi and ffi.C and ffi.C.KG_WinExec) then L('watcher: ffi/WinExec unavailable'); return end
  local cmd = (DIR .. 'content/KitGenerator/watcher.cmd'):gsub('/', '\\')
  local f = io.open(cmd, 'r')
  if not f then
    L('watcher not installed: run generator/setup_watcher.py to create ' .. cmd)
    return
  end
  f:close()
  WATCHER_STARTED = true
  if ffi.C.KG_GetPid then write_file('game.pid', tostring(ffi.C.KG_GetPid())) end
  ffi.C.KG_WinExec('cmd.exe /c "' .. cmd .. '"', 0)
  L('kit watcher launched')
end

-- ===== module init ========================================================
function m.init(ctx)
  DIR = tostring(ctx.sider_dir or '')
  if DIR ~= '' and DIR:sub(-1) ~= '\\' and DIR:sub(-1) ~= '/' then DIR = DIR .. '/' end
  load_offers()
  load_brands()
  load_club_file()
  load_deal()
  load_paid()
  install_date_reader()
  if CFG.ENABLE_SPONSORSHIP and CFG.CREDIT_BUDGET then install_budget_hook() end
  if CFG.ENABLE_KITS then start_watcher() end
  refresh_date(ctx)
  local y, mo, d = game_date()
  if y then
    L(string.format('game date %04d-%02d-%02d -> season %d', y, mo, d, SEASON))
  else
    L('game date not readable yet (no career loaded?) - season default ' .. tostring(SEASON))
  end
  ensure_season_deal(SEASON)
  L(string.format('loaded v%s (kits %s, sponsorship %s, budget crediting %s)', m.version,
    CFG.ENABLE_KITS and 'ON' or 'OFF',
    CFG.ENABLE_SPONSORSHIP and 'ON' or 'OFF',
    (CFG.ENABLE_SPONSORSHIP and CFG.CREDIT_BUDGET) and 'ON' or 'OFF'))
  if CFG.ENABLE_SPONSORSHIP then
    if ctx.menu then
      ctx.menu.add({ id = 'sponsors', title = 'Sponsor Office', icon = 'tag', order = 30,
        description = 'ACTIVE SPONSORSHIP CONTRACT & MANAGEMENT  -  MODULE V2.1',
        status = function(c) return DEAL and 'Active' or 'Ready' end, build = build_office })
      ctx.register('key_down', m.key_down)
    else
      ctx.register('overlay_on', m.overlay_on)
      ctx.register('key_down', m.key_down)
    end
  end
  ctx.register('set_teams', m.set_teams)
  ctx.register('set_home_team_for_kits', m.set_home_team_for_kits)
  ctx.register('livecpk_make_key', m.make_key)
  ctx.register('livecpk_get_filepath', m.get_filepath)
end

-- small read-only accessor (diagnostics / tests)
function m.status()
  return { version = m.version, season = SEASON, year = STATE.year, month = STATE.month,
           day = STATE.day, date_ready = DATE.ready, mode = DATE.mode,
           budget_ready = BUDGET.ready, budget_disp = BUDGET.disp,
           budget_salary_disp = BUDGET.salary_disp,
           kits = CFG.ENABLE_KITS, sponsorship = CFG.ENABLE_SPONSORSHIP,
           club = club_id(), club_source = club_source(),
           club_name = club_id() and club_display_name(club_id()) or nil,
           club_matches = STATE.matches, brands = #BRANDS,
           deal = DEAL and DEAL.brand or nil,
           deal_bonus = DEAL and (DEAL.bonus or 0) or nil,
           deal_end = DEAL and deal_end() or nil }
end

-- diagnostic helper: credit an amount now (also handy as a manual fix-up).
-- NOTE: this is a raw credit and deliberately bypasses the per-season ledger.
function m.debug_credit(euros)
  return credit_budget(euros)
end

-- diagnostic helpers for the per-season payment guard (used by the tests)
function m.debug_set_deal(brand, club, season, value, years, bonus)
  DEAL = { id = 'debug', club = club, brand = brand, start = season,
           years = years or 1, value = value or 0, tier = 'mid', bonus = bonus or 0 }
  return true
end

function m.debug_clear_deal()
  DEAL = nil
  REQ_SEASONS = {}
  save_deal()
  return true
end

function m.debug_credit_now()
  credit_now()
  return SEL.credit_msg
end

function m.debug_terminate()
  SEL.term_confirm = false
  terminate_deal()
  return SEL.credit_msg
end

-- diagnostic helpers for the managed-club auto-detection (used by the tests)
function m.debug_set_date(y, mo, d)
  STATE.year, STATE.month, STATE.day = y, mo, d
  STATE.date_ok = (y ~= nil)
  if y then SEASON = (mo >= 6) and (y + 1) or y end
  return SEASON
end

function m.debug_club_id()
  return club_id()
end

function m.debug_set_club(cid, persist)
  return set_club(cid, 'manual', persist ~= false)
end

function m.debug_clear_club()
  STATE.club, STATE.club_source, STATE.club_name = nil, nil, nil
  return true
end

function m.debug_reset_votes()
  STATE.votes, STATE.matches = {}, 0
  return true
end

function m.debug_record_match(home, away)
  record_match(home, away)
  return STATE.matches
end

function m.debug_learn_club()
  learn_club()
  return club_id()
end

function m.debug_visible_count()
  return #visible()
end

function m.debug_sign(brand, club, season, value, years, bonus)
  local o = { id = 'dbg-' .. tostring(brand) .. '-' .. tostring(season), club = club,
              brand = brand, season = season, years = years or 1, value = value or 0,
              bonus = bonus or 0, tier = 'mid', logo = path('logos/' .. tostring(brand) .. '.png') }
  sign_offer(o)
  return DEAL and DEAL.brand or nil
end

function m.debug_set_flag(name, value)
  CFG[name] = value
  return CFG[name]
end

return m
