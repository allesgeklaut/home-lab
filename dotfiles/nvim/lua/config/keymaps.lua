-- ============================================================================
-- Keymaps
-- Key mappings migrated from .vimrc
-- ============================================================================

local keymap = vim.keymap.set
local opts = { noremap = true, silent = true }

-- ============================================================================
-- Normal Mode
-- ============================================================================

-- Better window navigation (migrated from .vimrc)
keymap("n", "<C-J>", "<C-W><C-J>", opts) -- Move to split below
keymap("n", "<C-K>", "<C-W><C-K>", opts) -- Move to split above
keymap("n", "<C-L>", "<C-W><C-L>", opts) -- Move to split right
keymap("n", "<C-H>", "<C-W><C-H>", opts) -- Move to split left

-- Move vertically by visual line (migrated from .vimrc)
keymap("n", "j", "gj", opts)
keymap("n", "k", "gk", opts)

-- Move to beginning/end of line (migrated from .vimrc)
keymap("n", "B", "^", opts)
keymap("n", "E", "$", opts)

-- Highlight last inserted text (migrated from .vimrc)
keymap("n", "gV", "`[v`]", opts)

-- Save file with Ctrl-W (migrated from .vimrc)
keymap("n", "<C-w>", ":update<CR>", opts)
keymap("n", "<leader>s", ":update<CR>", opts)

-- Leader key shortcuts (migrated from .vimrc)
keymap("n", "<leader><space>", ":nohlsearch<CR>", opts) -- Turn off search highlight

-- Go to last active tab (migrated from .vimrc)
keymap("n", "<C-y>", ":execute 'tabn ' . g:lasttab<CR>", opts)

-- Buffer navigation
keymap("n", "<leader>bn", ":bnext<CR>", opts) -- Next buffer
keymap("n", "<leader>bp", ":bprevious<CR>", opts) -- Previous buffer
keymap("n", "<leader>bd", ":bdelete<CR>", opts) -- Delete buffer

-- Window resizing
keymap("n", "<C-Up>", ":resize +2<CR>", opts)
keymap("n", "<C-Down>", ":resize -2<CR>", opts)
keymap("n", "<C-Left>", ":vertical resize -2<CR>", opts)
keymap("n", "<C-Right>", ":vertical resize +2<CR>", opts)

-- ============================================================================
-- Insert Mode
-- ============================================================================

-- Remap ESC (migrated from .vimrc)
keymap("i", "jj", "<Esc>", opts)
keymap("i", "kk", "<Esc>", opts)

-- Save file with Ctrl-W in insert mode (migrated from .vimrc)
keymap("i", "<C-w>", "<C-o>:update<CR>", opts)
-- Easy paste of unnamed register
keymap("i", ",p", "<C-r>+", opts)

-- ============================================================================
-- Visual Mode
-- ============================================================================

-- Save file with Ctrl-W in visual mode (migrated from .vimrc)
keymap("v", "<C-w>", "<C-c>:update<CR>gv", opts)

-- Stay in indent mode
keymap("v", "<", "<gv", opts)
keymap("v", ">", ">gv", opts)

-- Move text up and down
keymap("v", "J", ":move '>+1<CR>gv=gv", opts)
keymap("v", "K", ":move '<-2<CR>gv=gv", opts)

-- Paste without yanking in visual mode
keymap("v", "p", '"_dP', opts)

-- ============================================================================
-- Terminal mode
-- ============================================================================
keymap("t", "<Esc>", "<C-\\><C-n>", opts)

-- ============================================================================
-- LSP Keymaps (will be set up in LSP config)
-- ============================================================================

-- These are defined in the LSP on_attach function:
-- gd - Go to definition
-- gD - Go to declaration
-- gr - Go to references
-- gi - Go to implementation
-- K - Show hover documentation
-- <leader>rn - Rename symbol
-- <leader>ca - Code action
-- <leader>f - Format document
-- [d - Previous diagnostic
-- ]d - Next diagnostic
-- <leader>q - Show diagnostic in float

-- ============================================================================
-- DAP (Debugging) Keymaps (will be set up in DAP config)
-- ============================================================================

-- These are defined in the DAP config:
-- <F5> - Continue/Start debugging
-- <F10> - Step over
-- <F11> - Step into
-- <F12> - Step out
-- <leader>b - Toggle breakpoint
-- <leader>B - Set conditional breakpoint
-- <leader>dr - Open REPL
-- <leader>dl - Run last debug session
