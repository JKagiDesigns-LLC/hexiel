-- hexiel for Neovim: a chat split on top of `hexiel --acp` (Agent Client Protocol, JSON lines over stdio).
-- Needs Neovim 0.9+. Nothing but the Neovim API is used.
local M = {}

M.config = { command = { "hexiel", "--acp" }, width = 70 }

local state = { job = nil, buf = nil, win = nil, next_id = 1, pending = {}, session = nil, busy = false, partial = "", line_open = false, diffs = {} }

local function chat_buf()
  if state.buf and vim.api.nvim_buf_is_valid(state.buf) then return state.buf end
  state.buf = vim.api.nvim_create_buf(false, true)
  vim.bo[state.buf].buftype = "nofile"
  vim.bo[state.buf].filetype = "markdown"
  vim.bo[state.buf].swapfile = false
  pcall(vim.api.nvim_buf_set_name, state.buf, "hexiel://chat")
  vim.keymap.set("n", "q", function() M.close() end, { buffer = state.buf, silent = true })
  vim.keymap.set("n", "<C-c>", function() M.stop() end, { buffer = state.buf, silent = true })
  vim.keymap.set("n", "gd", function() M.show_diff() end, { buffer = state.buf, silent = true, desc = "hexiel: view last diff" })
  vim.keymap.set("n", "i", function() M.ask() end, { buffer = state.buf, silent = true, desc = "hexiel: ask" })
  return state.buf
end

local function append(text)
  local buf = chat_buf()
  vim.bo[buf].modifiable = true
  local pieces = vim.split(text, "\n", { plain = true })
  local last = vim.api.nvim_buf_line_count(buf)
  if state.line_open then
    local cur = vim.api.nvim_buf_get_lines(buf, last - 1, last, false)[1] or ""
    pieces[1] = cur .. pieces[1]
    vim.api.nvim_buf_set_lines(buf, last - 1, last, false, pieces)
  else
    if last == 1 and (vim.api.nvim_buf_get_lines(buf, 0, 1, false)[1] or "") == "" then
      vim.api.nvim_buf_set_lines(buf, 0, 1, false, pieces)
    else
      vim.api.nvim_buf_set_lines(buf, last, last, false, pieces)
    end
  end
  state.line_open = true
  vim.bo[buf].modifiable = false
  if state.win and vim.api.nvim_win_is_valid(state.win) then
    vim.api.nvim_win_set_cursor(state.win, { vim.api.nvim_buf_line_count(buf), 0 })
  end
end

local function line(text) -- a whole line of its own
  if state.line_open then append("\n") end
  append(text)
  state.line_open = false
end

local function send(obj)
  if not state.job then return end
  vim.fn.chansend(state.job, vim.json.encode(obj) .. "\n")
end

local function request(method, params, cb)
  local id = state.next_id
  state.next_id = id + 1
  state.pending[id] = cb or function() end
  send({ jsonrpc = "2.0", id = id, method = method, params = params })
end

local function on_update(u)
  local kind = u.sessionUpdate
  if kind == "agent_message_chunk" then
    append(u.content.text or "")
  elseif kind == "tool_call" then
    line("⏺ " .. (u.title or "tool"))
  elseif kind == "tool_call_update" then
    for _, c in ipairs(u.content or {}) do
      if c.type == "diff" then
        state.diffs[#state.diffs + 1] = c
        line("  ± " .. (c.path or "?") .. "   (gd: view diff)")
      end
    end
    if u.status == "failed" then line("  ✗ failed") end
  end
end

local function on_permission(msg)
  local options = msg.params.options or {}
  local title = msg.params.toolCall and msg.params.toolCall.title or "a tool"
  vim.schedule(function()
    vim.ui.select(options, {
      prompt = "hexiel wants to: " .. title,
      format_item = function(o) return o.name end,
    }, function(choice)
      local outcome = choice and { outcome = "selected", optionId = choice.optionId } or { outcome = "cancelled" }
      send({ jsonrpc = "2.0", id = msg.id, result = { outcome = outcome } })
    end)
  end)
end

local function handle(msg)
  if msg.method == nil then
    local cb = state.pending[msg.id]
    state.pending[msg.id] = nil
    if cb then cb(msg.result, msg.error) end
  elseif msg.method == "session/update" then
    on_update(msg.params.update)
  elseif msg.method == "session/request_permission" then
    on_permission(msg)
  elseif msg.id ~= nil then
    send({ jsonrpc = "2.0", id = msg.id, error = { code = -32601, message = "not supported: " .. msg.method } })
  end
end

-- bytes from the process arrive in arbitrary pieces; JSON lines are rebuilt here (exposed for tests)
function M._feed(data)
  for i, piece in ipairs(data) do
    if i == 1 then
      state.partial = state.partial .. piece
    else
      local text = state.partial
      state.partial = piece
      if text ~= "" then
        local ok, msg = pcall(vim.json.decode, text)
        if ok and type(msg) == "table" then vim.schedule(function() handle(msg) end) end
      end
    end
  end
end

local function start(cb)
  if state.job and state.session then return cb() end
  state.partial, state.pending, state.session = "", {}, nil
  state.job = vim.fn.jobstart(M.config.command, {
    cwd = vim.fn.getcwd(),
    on_stdout = function(_, data) M._feed(data) end,
    on_stderr = function() end,
    on_exit = function(_, code)
      state.job, state.session, state.busy = nil, nil, false
      vim.schedule(function() line("— hexiel stopped (exit " .. code .. ")") end)
    end,
  })
  if state.job <= 0 then
    state.job = nil
    vim.notify("hexiel: could not start `" .. table.concat(M.config.command, " ") .. "` — is it installed? (pipx install hexiel)", vim.log.levels.ERROR)
    return
  end
  request("initialize", { protocolVersion = 1, clientCapabilities = vim.empty_dict(), clientInfo = { name = "hexiel.nvim", version = "0.1.0" } }, function(_, err)
    if err then return vim.notify("hexiel: " .. (err.message or "initialize failed"), vim.log.levels.ERROR) end
    request("session/new", { cwd = vim.fn.getcwd(), mcpServers = {} }, function(res, err2)
      if err2 or not res then return vim.notify("hexiel: " .. ((err2 and err2.message) or "no session"), vim.log.levels.ERROR) end
      state.session = res.sessionId
      cb()
    end)
  end)
end

function M.open()
  local buf = chat_buf()
  if not (state.win and vim.api.nvim_win_is_valid(state.win)) then
    vim.cmd("botright vsplit")
    state.win = vim.api.nvim_get_current_win()
    vim.api.nvim_win_set_buf(state.win, buf)
    vim.api.nvim_win_set_width(state.win, M.config.width)
    vim.wo[state.win].wrap = true
    vim.wo[state.win].linebreak = true
  end
  return buf
end

function M.close()
  if state.win and vim.api.nvim_win_is_valid(state.win) then vim.api.nvim_win_close(state.win, true) end
  state.win = nil
end

function M.prompt(text)
  if state.busy then return vim.notify("hexiel is still working (:HexielStop to interrupt)", vim.log.levels.WARN) end
  M.open()
  line("")
  line("❯ " .. text:gsub("\n", "\n  "))
  line("")
  state.busy = true
  start(function()
    request("session/prompt", { sessionId = state.session, prompt = { { type = "text", text = text } } }, function(res, err)
      state.busy = false
      if err then line("⚠ " .. (err.message or "error")) end
      if res and res.stopReason == "cancelled" then line("(stopped)") end
    end)
  end)
  if not state.job then state.busy = false end
end

function M.ask(question)
  if question and question ~= "" then return M.prompt(question) end
  vim.ui.input({ prompt = "hexiel> " }, function(input)
    if input and input ~= "" then M.prompt(input) end
  end)
end

-- the visual selection (or the :range) as a fenced block, plus the question
function M.send_range(l1, l2, question)
  local lines = vim.api.nvim_buf_get_lines(0, l1 - 1, l2, false)
  local name = vim.fn.fnamemodify(vim.api.nvim_buf_get_name(0), ":.")
  local function with(q)
    M.prompt(string.format("%s\n\n%s (lines %d-%d):\n```%s\n%s\n```", q, name, l1, l2, vim.bo.filetype, table.concat(lines, "\n")))
  end
  if question and question ~= "" then return with(question) end
  vim.ui.input({ prompt = "hexiel (about the selection)> ", default = "Explain this." }, function(q)
    if q and q ~= "" then with(q) end
  end)
end

function M.stop()
  if state.session then send({ jsonrpc = "2.0", method = "session/cancel", params = { sessionId = state.session } }) end
end

-- open the last edit hexiel made as a side-by-side diff in a new tab
function M.show_diff()
  local d = state.diffs[#state.diffs]
  if not d then return vim.notify("hexiel: no edit to show yet") end
  vim.cmd("tabnew")
  local old = vim.api.nvim_get_current_buf()
  vim.bo[old].buftype, vim.bo[old].bufhidden = "nofile", "wipe"
  vim.api.nvim_buf_set_lines(old, 0, -1, false, vim.split(d.oldText or "", "\n", { plain = true }))
  vim.cmd("diffthis | vsplit | enew")
  local new = vim.api.nvim_get_current_buf()
  vim.bo[new].buftype, vim.bo[new].bufhidden = "nofile", "wipe"
  vim.api.nvim_buf_set_lines(new, 0, -1, false, vim.split(d.newText or "", "\n", { plain = true }))
  vim.cmd("diffthis")
  vim.notify("hexiel: " .. (d.path or "") .. " — left: before, right: after")
end

function M.new_chat()
  if state.job then vim.fn.jobstop(state.job) end
  state.job, state.session, state.busy, state.diffs = nil, nil, false, {}
  if state.buf and vim.api.nvim_buf_is_valid(state.buf) then
    vim.bo[state.buf].modifiable = true
    vim.api.nvim_buf_set_lines(state.buf, 0, -1, false, { "" })
    vim.bo[state.buf].modifiable = false
  end
  state.line_open = false
end

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", M.config, opts or {})
  if type(M.config.command) == "string" then M.config.command = vim.split(M.config.command, " ", { trimempty = true }) end
end

return M
