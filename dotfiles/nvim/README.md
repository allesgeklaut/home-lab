# Neovim Configuration

Personal Neovim config (Lua, [lazy.nvim](https://github.com/folke/lazy.nvim)).
It is tracked in git as `dotfiles/nvim` and symlinked to `~/.config/nvim`,
so the working tree **is** the live configuration — edit it here and Neovim
picks the change up immediately.

## Install

```bash
./install.sh
```

The script checks prerequisites, backs up any existing `~/.config/nvim`,
and symlinks this checkout into place. Re-running it is safe. Manual
equivalent:

```bash
ln -sfn "$(pwd)" ~/.config/nvim
```

On first launch lazy.nvim bootstraps itself and installs all plugins;
restart once that finishes. Plugin data lives in `~/.local/share/nvim`,
never in this repo.

### Prerequisites

- **Neovim ≥ 0.11** (the config uses `vim.lsp.config` and blink.cmp)
- `git`, and a **C compiler** (Treesitter parsers)
- Optional but used: `node` (some LSP servers), `python3`, `ripgrep`
- A **Nerd Font** for icons

## Layout

```
init.lua               entry point; bootstraps lazy.nvim
install.sh             symlink installer
lua/config/
  settings.lua         core options
  keymaps.lua          general keymaps
  autocmds.lua         trim whitespace, Python settings, format-on-save
lua/plugins/
  colorscheme.lua      dracula
  lsp.lua              nvim-lspconfig + mason + blink.cmp + roslyn
  treesitter.lua       syntax highlighting + textobjects
  dap.lua              Python debugging (debugpy) with nvim-dap / dap-ui
  extras.lua           telescope, nvim-tree, gitsigns, lualine, bufferline,
                       autopairs, Comment, indent-blankline, which-key
  snacks.lua           snacks input/picker (used by opencode.nvim only)
  opencode.lua         opencode.nvim integration
  store.lua            store.nvim plugin browser
  copilot.vim.lua      GitHub Copilot
  vim-fugitive.lua     tpope/vim-fugitive
spell/en.utf-8.add     personal word list (compiled .spl/.sug are gitignored)
```

## Keymaps

Leader is `,`.

### General (`lua/config/keymaps.lua`)

| Key | Action |
| --- | --- |
| `<C-h>` `<C-j>` `<C-k>` `<C-l>` | Move between splits |
| `j` / `k` | Move by visual line (`gj` / `gk`) |
| `B` / `E` | Beginning / end of line |
| `gV` | Reselect last inserted text |
| `<C-w>` | Save file (normal, insert, visual) |
| `<leader>s` | Save file (`:update`) |
| `<leader><space>` | Clear search highlight |
| `<C-y>` | Go to last active tab |
| `<leader>bn` / `<leader>bp` / `<leader>bd` | Next / previous / delete buffer |
| `<C-Up>` `<C-Down>` `<C-Left>` `<C-Right>` | Resize splits |
| `jj` / `kk` (insert) | Escape to normal |
| `,p` (insert) | Paste system clipboard |
| `<` / `>` (visual) | Indent, keeping selection |
| `J` / `K` (visual) | Move selection down / up |

### Plugins

| Key | Action |
| --- | --- |
| `<leader>ff` `<leader>fg` `<leader>fb` `<leader>fh` `<leader>fo` `<leader>fs` `<leader>fd` | Telescope: files, live grep, buffers, help, recent, workspace symbols, document symbols |
| `<leader>e` | Toggle nvim-tree |
| `]c` / `[c` | Next / previous git hunk |
| `<leader>hs` `<leader>hr` `<leader>hS` `<leader>hu` `<leader>hR` `<leader>hp` `<leader>hb` `<leader>hd` | Gitsigns stage/reset/preview/blame… |
| `<C-space>` | Treesitter incremental selection |
| `af` / `if` / `ac` / `ic` | Select function/class outer/inner |
| `]m` `]M` `[m` `[M` `]]` `][` `[[` `[]` | Move between functions/classes |

### LSP (`lua/plugins/lsp.lua`)

| Key | Action |
| --- | --- |
| `gd` / `gD` / `gr` / `gi` | Definition / declaration / references / implementation |
| `K` | Hover documentation |
| `<leader>rn` | Rename symbol |
| `<leader>ca` | Code action |
| `<leader>f` | Format |
| `[d` / `]d` | Previous / next diagnostic |
| `<leader>q` / `<leader>d` | Diagnostic list / float |

### Debugging (`lua/plugins/dap.lua`)

| Key | Action |
| --- | --- |
| `<F5>` | Start / continue |
| `<F10>` / `<F11>` / `<F12>` | Step over / into / out |
| `<leader>b` / `<leader>B` | Toggle / conditional breakpoint |
| `<leader>dr` | Open REPL |
| `<leader>dl` | Run last session |
| `<leader>dt` | Terminate |
| `<leader>du` / `<leader>de` | Toggle DAP UI / eval |

### opencode.nvim (`lua/plugins/opencode.lua`)

| Key | Action |
| --- | --- |
| `<C-a>` | Ask opencode about the current context |
| `<C-x>` | Select context for opencode |
| `go` / `goo` | Append range / line to opencode (operator) |
| `<S-C-u>` / `<S-C-d>` | Scroll the opencode session |

## LSP, completion and formatting

- Completion via **blink.cmp** (not nvim-cmp); LSP servers are managed by
  **mason** + **mason-lspconfig**, plus **roslyn.nvim** for C#.
- The config does **not** pin a server list. Install what you need with
  `:MasonInstall <server>` or the `:Mason` UI; servers attach automatically.
- `autocmds.lua` formats on save for Lua/Python and for any buffer with an
  LSP client attached.

## Debugging

Python debugging uses the `debugpy` adapter with DAP UI. Predefined
configurations include *Launch file*, *Launch file with arguments*,
*Django*, *FastAPI*, and *Attach to debugpy*. Install `debugpy` via
`:MasonInstall debugpy`.

## Notable settings (`lua/config/settings.lua`)

- Leader `,`; absolute (not relative) line numbers.
- Tabs: 2 spaces by default; **Python files** use 4 spaces and
  `textwidth=79` (`autocmds.lua`).
- Spell checking on, languages `en_us` + `de`.
- System clipboard (`unnamedplus`), persistent undo, `colorcolumn=120`,
  indent-fold, trailing whitespace trimmed on save, netrw configured.
- `python3_host_prog` points at `~/.config/nvim/venv/bin/python` — create
  that venv if you use Python providers (`venv/` is gitignored).
