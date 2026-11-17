if vim.g.loaded_hexiel then return end
vim.g.loaded_hexiel = true
local h = function() return require("hexiel") end
vim.api.nvim_create_user_command("Hexiel", function() h().open() end, { desc = "hexiel: open the chat" })
vim.api.nvim_create_user_command("HexielAsk", function(o) h().ask(o.args) end, { nargs = "*", desc = "hexiel: ask a question" })
vim.api.nvim_create_user_command("HexielSend", function(o) h().send_range(o.line1, o.line2, o.args) end,
  { nargs = "*", range = true, desc = "hexiel: ask about the selected lines" })
vim.api.nvim_create_user_command("HexielStop", function() h().stop() end, { desc = "hexiel: stop the current work" })
vim.api.nvim_create_user_command("HexielNew", function() h().new_chat() end, { desc = "hexiel: start a new chat" })
vim.api.nvim_create_user_command("HexielDiff", function() h().show_diff() end, { desc = "hexiel: view the last edit as a diff" })
