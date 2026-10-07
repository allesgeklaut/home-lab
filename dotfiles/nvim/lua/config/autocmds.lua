-- ============================================================================
-- Autocommands
-- Automated commands and file-specific settings
-- ============================================================================

local augroup = vim.api.nvim_create_augroup
local autocmd = vim.api.nvim_create_autocmd

-- ============================================================================
-- General Settings
-- ============================================================================

-- Remove trailing whitespace on save
augroup("TrimWhitespace", { clear = true })
autocmd("BufWritePre", {
  group = "TrimWhitespace",
  pattern = "*",
  callback = function()
    local save = vim.fn.winsaveview()
    vim.cmd([[%s/\s\+$//e]])
    vim.fn.winrestview(save)
  end,
})

-- ============================================================================
-- Highlight trailing whitespace (migrated from .vimrc)
-- ============================================================================

augroup("ExtraWhitespace", { clear = true })
autocmd({ "BufRead", "BufNewFile" }, {
  group = "ExtraWhitespace",
  pattern = { "*.py", "*.pyw", "*.c", "*.h", "*.tex" },
  callback = function()
    vim.fn.matchadd("ExtraWhitespace", [[\s\+$]])
  end,
})

autocmd("ColorScheme", {
  group = "ExtraWhitespace",
  pattern = "*",
  callback = function()
    vim.api.nvim_set_hl(0, "ExtraWhitespace", { bg = "#FF0000", fg = "#FFFFFF" })
  end,
})

-- ============================================================================
-- Python-specific settings (migrated from .vimrc)
-- ============================================================================

augroup("PythonSettings", { clear = true })
autocmd({ "BufNewFile", "BufRead" }, {
  group = "PythonSettings",
  pattern = "*.py",
  callback = function()
    vim.opt_local.tabstop = 4
    vim.opt_local.softtabstop = 4
    vim.opt_local.shiftwidth = 4
    vim.opt_local.textwidth = 79
    vim.opt_local.expandtab = true
    vim.opt_local.autoindent = true
    vim.opt_local.fileformat = "unix"
  end,
})

-- ============================================================================
-- Tab tracking (migrated from .vimrc)
-- ============================================================================

augroup("TabTracking", { clear = true })
autocmd("TabLeave", {
  group = "TabTracking",
  pattern = "*",
  callback = function()
    vim.g.lasttab = vim.fn.tabpagenr()
  end,
})

-- ============================================================================
-- File type specific settings
-- ============================================================================

augroup("FileTypeSettings", { clear = true })

-- Auto-format on save for certain filetypes
autocmd("BufWritePre", {
  group = "FileTypeSettings",
  pattern = { "*.lua", "*.py" },
  callback = function()
    -- Format will be handled by LSP if available
    vim.lsp.buf.format({ async = false })
  end,
})

-- ============================================================================
-- LSP-specific autocommands
-- ============================================================================

augroup("LspFormatting", { clear = true })
autocmd("BufWritePre", {
  group = "LspFormatting",
  pattern = "*",
  callback = function()
    -- Only format if LSP is attached
    if #vim.lsp.get_clients({ bufnr = 0 }) > 0 then
      vim.lsp.buf.format({ async = false })
    end
  end,
})

-- ============================================================================
-- Neovim-specific improvements
-- ============================================================================

-- Check if we need to reload the file when it changed
augroup("CheckTime", { clear = true })
autocmd({ "FocusGained", "TermClose", "TermLeave" }, {
  group = "CheckTime",
  command = "checktime",
})

-- Resize splits if window is resized
augroup("ResizeSplits", { clear = true })
autocmd("VimResized", {
  group = "ResizeSplits",
  command = "tabdo wincmd =",
})

-- Close certain filetypes with q
augroup("CloseWithQ", { clear = true })
autocmd("FileType", {
  group = "CloseWithQ",
  pattern = {
    "help",
    "qf",
    "lspinfo",
    "man",
    "checkhealth",
  },
  callback = function(event)
    vim.bo[event.buf].buflisted = false
    vim.keymap.set("n", "q", "<cmd>close<cr>", { buffer = event.buf, silent = true })
  end,
})
