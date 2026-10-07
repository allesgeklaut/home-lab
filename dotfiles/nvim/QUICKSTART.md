# Neovim Configuration - Quick Start Guide

## 📦 What You've Received

A complete, production-ready Neovim configuration with:

✅ All settings from your `.vimrc` migrated to Lua  
✅ Lazy.nvim plugin manager configured  
✅ Store.nvim for browsing 5,500+ plugins  
✅ Dracula colorscheme  
✅ Python LSP (Pyright) with full IDE features  
✅ Python debugging (DAP) with debugpy  
✅ Auto-completion (nvim-cmp)  
✅ Syntax highlighting (Treesitter)  
✅ File explorer, fuzzy finder, git integration  

## 📁 File Overview

| File | Location | Purpose |
|------|----------|---------|
| `init.lua` | `~/.config/nvim/` | Main entry point |
| `settings.lua` | `~/.config/nvim/lua/config/` | Core settings (migrated from .vimrc) |
| `keymaps.lua` | `~/.config/nvim/lua/config/` | Key mappings (migrated from .vimrc) |
| `autocmds.lua` | `~/.config/nvim/lua/config/` | Auto-commands |
| `colorscheme.lua` | `~/.config/nvim/lua/plugins/` | Dracula theme |
| `store.lua` | `~/.config/nvim/lua/plugins/` | Plugin browser |
| `treesitter.lua` | `~/.config/nvim/lua/plugins/` | Syntax highlighting |
| `lsp.lua` | `~/.config/nvim/lua/plugins/` | LSP servers (Pyright, etc.) |
| `mason.lua` | `~/.config/nvim/lua/plugins/` | Tool installer |
| `nvim-cmp.lua` | `~/.config/nvim/lua/plugins/` | Auto-completion |
| `dap.lua` | `~/.config/nvim/lua/plugins/` | Python debugging |
| `extras.lua` | `~/.config/nvim/lua/plugins/` | Additional plugins |
| `README.md` | Documentation | Full documentation |
| `install.sh` | Helper script | Symlink installer |

## 🚀 Quick Installation

This configuration lives in git and is meant to be *symlinked* into
`~/.config/nvim`, not copied — that way edits are versioned automatically.

### Option 1: Using the Installation Script

```bash
./install.sh
```

It checks prerequisites and symlinks this checkout to `~/.config/nvim`
(backing up any existing config first). Re-running it is safe.

### Option 2: Manual Setup

```bash
ln -sfn "$(pwd)" ~/.config/nvim
```

### After installing

1. **Launch Neovim:**
   ```bash
   nvim
   ```

2. **Wait** for automatic plugin installation (2-3 minutes)

3. **Restart** Neovim

## 🎯 Essential Keybindings

### Your Migrated Keybindings (from .vimrc)

| Key | Mode | Action |
|-----|------|--------|
| `,` | Leader | Leader key |
| `jj` or `kk` | Insert | Exit to normal mode |
| `j` / `k` | Normal | Move by visual line |
| `B` / `E` | Normal | Beginning/End of line |
| `<C-N/K/L/H>` | Normal | Navigate splits (Down/Up/Right/Left) |
| `<C-w>` | All modes | Save file |
| `<C-y>` | Normal | Switch to last tab |
| `<leader><space>` | Normal | Clear search highlight |
| `<leader>s` | Normal | Save session |

### New LSP Keybindings

| Key | Action |
|-----|--------|
| `gd` | Go to definition |
| `K` | Show documentation |
| `<leader>rn` | Rename symbol |
| `<leader>ca` | Code action |
| `<leader>f` | Format code |
| `[d` / `]d` | Previous/Next diagnostic |

### Debugging Keybindings

| Key | Action |
|-----|--------|
| `<F5>` | Start/Continue debugging |
| `<F10>` | Step over |
| `<F11>` | Step into |
| `<leader>b` | Toggle breakpoint |

### File Navigation

| Key | Action |
|-----|--------|
| `<leader>e` | Toggle file explorer |
| `<leader>ff` | Find files |
| `<leader>fg` | Search text (grep) |
| `<leader>fb` | Find buffers |

## 🐍 Python Development Workflow

1. **Open a Python file:**
   ```bash
   nvim main.py
   ```

2. **LSP automatically provides:**
   - Auto-completion as you type
   - Error/warning diagnostics
   - Type hints
   - Import organization

3. **Debug your code:**
   - Set breakpoint: `<leader>b`
   - Start debugging: `<F5>`
   - Step through: `<F10>` / `<F11>`

4. **Format code:** `<leader>f`

## 🔧 Common Commands

### Plugin Management
```vim
:Lazy                 " Open plugin manager UI
:Lazy sync           " Sync plugins (install/update)
:Store               " Browse available plugins
```

### LSP Management
```vim
:Mason               " Manage LSP servers/tools
:LspInfo            " Show LSP status
:LspInstall pyright " Install Python LSP
```

### Debugging
```vim
:DapContinue        " Start/continue debugging
:DapToggleBreakpoint " Toggle breakpoint
```

### Health Check
```vim
:checkhealth        " Check Neovim health
:checkhealth lsp    " Check LSP status
:checkhealth nvim-treesitter " Check Treesitter
```

## 📊 What's Configured

### Python Tools (Auto-installed via Mason)
- ✅ **Pyright** - LSP for Python
- ✅ **debugpy** - Python debugger
- ✅ **Black** - Code formatter
- ✅ **isort** - Import sorter
- ✅ **Pylint** - Linter
- ✅ **Mypy** - Type checker

### File Types Supported (via Treesitter)
- Python, Lua, Bash, JSON, YAML, TOML
- Markdown, Vim script
- And more (auto-installs as needed)

### Quality of Life Features
- ✅ Auto-pairs for brackets/quotes
- ✅ Easy commenting (gcc to toggle line comment)
- ✅ Git integration (see changes inline)
- ✅ Indent guides
- ✅ Status line with git branch, diagnostics
- ✅ Buffer tabs
- ✅ Which-key helper (shows available keybindings)

## 🎨 Customization

### Change Leader Key
Edit `init.lua`:
```lua
vim.g.mapleader = " "  -- Change from "," to space
```

### Add More LSP Servers
Edit `lua/plugins/lsp.lua`, add to `ensure_installed`:
```lua
ensure_installed = {
  "pyright",
  "tsserver",  -- TypeScript
  "gopls",     -- Go
  -- etc.
}
```

### Install New Plugins
Create `lua/plugins/myplugin.lua`:
```lua
return {
  "author/plugin-name",
  config = function()
    -- setup code
  end,
}
```

### Change Colorscheme
Edit `lua/plugins/colorscheme.lua` or try another:
```vim
:colorscheme dracula
:colorscheme dracula-soft
```

## 🆘 Troubleshooting

### "Plugins not installing"
```vim
:Lazy sync
:Lazy clean
```

### "LSP not working"
```vim
:LspInfo
:Mason
:checkhealth lsp
```

### "Python debugger not found"
```vim
:Mason
" Install 'debugpy'
```

### "Slow startup"
- First launch is always slow (installing everything)
- Subsequent launches should be <100ms
- Check: `:Lazy profile`

## 📝 Directory Structure After Installation

```
~/.config/nvim/
├── init.lua
└── lua/
    ├── config/
    │   ├── settings.lua
    │   ├── keymaps.lua
    │   └── autocmds.lua
    └── plugins/
        ├── colorscheme.lua
        ├── store.lua
        ├── treesitter.lua
        ├── lsp.lua
        ├── mason.lua
        ├── nvim-cmp.lua
        ├── dap.lua
        └── extras.lua
```

## 🔗 Useful Resources

- **Neovim docs**: `:help` or https://neovim.io/doc/
- **Lazy.nvim**: https://github.com/folke/lazy.nvim
- **Mason**: https://github.com/williamboman/mason.nvim
- **Store.nvim**: https://github.com/alex-popov-tech/store.nvim
- **LSP config**: https://github.com/neovim/nvim-lspconfig

## ✨ Key Differences from Vim

| Feature | Vim | Neovim |
|---------|-----|--------|
| Config language | Vimscript | Lua (much faster) |
| Plugin manager | Vundle/Plug | Lazy.nvim (lazy-loading) |
| LSP | External plugins | Built-in client |
| Async | Limited | Native support |
| Performance | Good | Excellent |
| Modern features | Plugins needed | Built-in |

## 🎉 You're All Set!

Your Neovim is now configured with:
- All your original `.vimrc` settings preserved
- Modern IDE features (LSP, debugging, completion)
- Python development ready
- 5,500+ plugins available via Store.nvim
- Beautiful Dracula theme

**Next:** Launch `nvim` and start coding! 🚀

For questions or issues, refer to the full `README.md` or use `:help` within Neovim.
