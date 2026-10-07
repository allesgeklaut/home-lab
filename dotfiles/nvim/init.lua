-- ============================================================================
-- Neovim Init Configuration
-- Migrated from Vim to Neovim with Lua
-- ============================================================================

-- Set leader key before loading plugins
vim.g.mapleader = ","
vim.g.maplocalleader = ","

-- Bootstrap lazy.nvim plugin manager
local lazypath = vim.fn.stdpath("data") .. "/lazy/lazy.nvim"
if not vim.loop.fs_stat(lazypath) then
  vim.fn.system({
    "git",
    "clone",
    "--filter=blob:none",
    "https://github.com/folke/lazy.nvim.git",
    "--branch=stable",
    lazypath,
  })
end
vim.opt.rtp:prepend(lazypath)

-- Load core settings
require("config.settings")

-- Load plugins
require("lazy").setup("plugins", {
  defaults = { lazy = false },
  install = { colorscheme = { "dracula" } },
  checker = { enabled = true },
  performance = {
    rtp = {
      disabled_plugins = {
        "gzip",
        "tarPlugin",
        "tohtml",
        "tutor",
        "zipPlugin",
      },
    },
  },
})

-- Load keymaps after plugins
require("config.keymaps")

-- Load autocommands
require("config.autocmds")
