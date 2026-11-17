# hexiel for Neovim

A chat split on top of your local `hexiel --acp` (Agent Client Protocol). Neovim 0.9+, no dependencies.

```lua
-- lazy.nvim
{ dir = "/path/to/hexiel/editors/nvim", config = function() require("hexiel").setup({}) end }
-- or copy/symlink this folder into ~/.config/nvim/pack/hexiel/start/hexiel
```

| Command | What it does |
|---|---|
| `:Hexiel` | open the chat split (`q` closes it, `i` asks, `Ctrl-C` stops) |
| `:HexielAsk [question]` | ask (prompts when no question is given) |
| `:'<,'>HexielSend [question]` | ask about the selected lines (sent as a fenced block with file and line numbers) |
| `:HexielStop` / `:HexielNew` | interrupt the current work / start a fresh chat |
| `:HexielDiff` (or `gd` in the chat) | the last edit as a side-by-side diff |

Edits and commands ask for permission through `vim.ui.select`. Options: `require("hexiel").setup({ command = { "hexiel", "--acp", "-m", "gmktec" }, width = 80 })`.
