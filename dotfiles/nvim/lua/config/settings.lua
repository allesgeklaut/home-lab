-- ============================================================================
-- Core Settings
-- Basic Neovim settings migrated from .vimrc
-- ============================================================================

local opt = vim.opt
local g = vim.g

-- ============================================================================
-- Editor Behavior
-- ============================================================================

-- Line numbers
opt.number = true          -- Show line numbers
opt.relativenumber = false -- Use absolute line numbers

-- Tabs and indentation
opt.expandtab = true   -- Tabs are spaces
opt.shiftwidth = 2     -- Number of spaces per indentation level
opt.softtabstop = 2    -- Number of spaces in tab when editing
opt.tabstop = 2        -- Number of spaces tabs count for
opt.autoindent = true  -- Copy indent from current line when starting new line
opt.smartindent = true -- Smart autoindenting when starting new line

-- Search settings
opt.incsearch = true  -- Search as characters are entered
opt.hlsearch = true   -- Highlight matches
opt.ignorecase = true -- Ignore case in search patterns
opt.smartcase = true  -- Override ignorecase if search contains capitals

-- UI settings
opt.showcmd = true       -- Show command in bottom bar
opt.showmode = true      -- Show current mode
opt.showmatch = true     -- Highlight matching brackets
opt.cursorline = true    -- Highlight current line
opt.wildmenu = true      -- Visual autocomplete for command menu
opt.wildmode = "longest:full,full"
opt.mouse = "a"          -- Enable mouse support
opt.termguicolors = true -- True color support
opt.signcolumn = "yes"   -- Always show sign column
opt.colorcolumn = "120"  -- Show column at 80 characters

-- Status line
opt.laststatus = 2 -- Always show status line

-- Folding
opt.foldenable = true     -- Enable folding
opt.foldlevelstart = 10   -- Open most folds by default
opt.foldnestmax = 10      -- 10 nested fold max
opt.foldmethod = "indent" -- Fold based on indent level

-- Completion options
opt.completeopt = { "menu", "menuone", "noselect" }

-- File handling
opt.fileformat = "unix" -- Use Unix line endings
opt.encoding = "utf-8"  -- Set encoding
opt.backup = false      -- No backup file
opt.writebackup = false -- No backup when writing
opt.swapfile = false    -- No swap file
opt.undofile = true     -- Enable persistent undo
opt.undodir = vim.fn.stdpath("data") .. "/undo"

-- Splitting
opt.splitright = true -- Vertical splits go to the right
opt.splitbelow = true -- Horizontal splits go below

-- Performance
opt.updatetime = 250 -- Faster completion
opt.timeoutlen = 300 -- Time to wait for mapped sequence

-- Display
opt.list = true -- Show invisible characters
opt.listchars = {
  tab = "→ ",
  trail = "·",
  extends = "›",
  precedes = "‹",
  nbsp = "␣",
}

-- Spell checking
opt.spell = true                  -- Enable spell checking
opt.spelllang = { "en_us", "de" } -- Set spell check language

-- Scrolling
opt.scrolloff = 8     -- Keep 8 lines above/below cursor
opt.sidescrolloff = 8 -- Keep 8 columns left/right of cursor

-- Clipboard
opt.clipboard = "unnamedplus" -- Use system clipboard

-- ============================================================================
-- Netrw settings (built-in file browser)
-- ============================================================================
g.netrw_browse_split = 4 -- Open files in previous window
g.netrw_liststyle = 3    -- Tree list view
g.netrw_winsize = 15     -- Width of netrw window
g.netrw_banner = 0       -- Hide banner

-- ============================================================================
-- Python for vim
-- ============================================================================
vim.g.python3_host_prog = vim.fn.expand("~/.config/nvim/venv/bin/python")

-- ============================================================================
-- Cursor appearance
-- ============================================================================
opt.guicursor = {
  "n-v-c:block",   -- Normal, visual, command: block cursor
  "i-ci-ve:ver25", -- Insert: vertical bar cursor
  "r-cr:hor20",    -- Replace: horizontal bar cursor
  "o:hor50",       -- Operator: horizontal bar cursor
}
