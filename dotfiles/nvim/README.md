# Neovim Configuration

A modern, well-structured Neovim configuration migrated from Vim, featuring Lua-based setup with lazy.nvim plugin manager, Python LSP and debugging support, and the Dracula colorscheme.

## 📁 Directory Structure

```
~/.config/nvim/
├── init.lua                    # Main configuration entry point
├── lua/
│   ├── config/
│   │   ├── settings.lua        # Core Neovim settings
│   │   ├── keymaps.lua         # Key mappings
│   │   └── autocmds.lua        # Autocommands
│   └── plugins/
│       ├── colorscheme.lua     # Dracula theme
│       ├── store.lua           # Plugin store browser
│       ├── treesitter.lua      # Syntax highlighting
│       ├── lsp.lua             # LSP configuration
│       ├── mason.lua           # Tool installer
│       ├── nvim-cmp.lua        # Autocompletion
│       ├── dap.lua             # Debugging
│       └── extras.lua          # Additional plugins
```

## ✨ Features

### Core Features (Migrated from .vimrc)
- **Leader key**: `,` (comma)
- **Line numbers** and **cursorline** highlighting
- **Smart indentation** with tab/space conversion
- **Search highlighting** with incremental search
- **Folding** based on indentation
- **Mouse support** enabled
- **Spell checking** (English US)
- **Trailing whitespace** detection
- **Python-specific** settings (4-space indentation, 79 char line width)

### Key Mappings (Migrated from .vimrc)
- `jj` / `kk` → Escape from insert mode
- `j` / `k` → Move by visual lines
- `B` / `E` → Jump to beginning/end of line
- `<C-N>` / `<C-K>` / `<C-L>` / `<C-H>` → Navigate splits
- `<C-w>` → Save file (works in all modes)
- `<C-y>` → Switch to last active tab
- `<leader><space>` → Clear search highlighting
- `<leader>s` → Save session

### New Features
- **LSP Support**: Intelligent code completion, go-to-definition, hover documentation
- **Python Debugging**: Full DAP support with debugpy
- **Fuzzy Finding**: Telescope for file/text search
- **Git Integration**: Gitsigns for diff markers and git operations
- **File Explorer**: nvim-tree for project navigation
- **Treesitter**: Advanced syntax highlighting
- **Auto-pairs**: Automatic bracket/quote pairing
- **Comment**: Easy code commenting

## 🚀 Installation

### Prerequisites

1. **Neovim >= 0.9.0**
   ```bash
   # Check your version
   nvim --version
   ```

2. **Git**
   ```bash
   git --version
   ```

3. **Node.js and npm** (for some LSP servers)
   ```bash
   node --version
   npm --version
   ```

4. **Python 3** (for Python development)
   ```bash
   python3 --version
   ```

5. **A C compiler** (for Treesitter)
   ```bash
   # Linux
   gcc --version

   # macOS
   clang --version
   ```

6. **Ripgrep** (optional, for Telescope live grep)
   ```bash
   # Ubuntu/Debian
   sudo apt install ripgrep

   # macOS
   brew install ripgrep

   # Fedora
   sudo dnf install ripgrep
   ```

7. **A Nerd Font** (for icons)
   - Download from: https://www.nerdfonts.com/
   - Recommended: JetBrainsMono Nerd Font, FiraCode Nerd Font

### Setup Steps

1. **Backup existing Neovim configuration** (if any)
   ```bash
   mv ~/.config/nvim ~/.config/nvim.backup
   mv ~/.local/share/nvim ~/.local/share/nvim.backup
   ```

2. **Create the directory structure**
   ```bash
   mkdir -p ~/.config/nvim/lua/{config,plugins}
   ```

3. **Place configuration files**
   
   Save the following files in their respective locations:
   
   - `init.lua` → `~/.config/nvim/init.lua`
   - `settings.lua` → `~/.config/nvim/lua/config/settings.lua`
   - `keymaps.lua` → `~/.config/nvim/lua/config/keymaps.lua`
   - `autocmds.lua` → `~/.config/nvim/lua/config/autocmds.lua`
   - `colorscheme.lua` → `~/.config/nvim/lua/plugins/colorscheme.lua`
   - `store.lua` → `~/.config/nvim/lua/plugins/store.lua`
   - `treesitter.lua` → `~/.config/nvim/lua/plugins/treesitter.lua`
   - `lsp.lua` → `~/.config/nvim/lua/plugins/lsp.lua`
   - `mason.lua` → `~/.config/nvim/lua/plugins/mason.lua`
   - `nvim-cmp.lua` → `~/.config/nvim/lua/plugins/nvim-cmp.lua`
   - `dap.lua` → `~/.config/nvim/lua/plugins/dap.lua`
   - `extras.lua` → `~/.config/nvim/lua/plugins/extras.lua`

4. **Launch Neovim**
   ```bash
   nvim
   ```
   
   On first launch:
   - lazy.nvim will automatically bootstrap and install itself
   - All plugins will be automatically installed
   - LSP servers will be installed via Mason
   - Treesitter parsers will be installed

5. **Wait for installation to complete**
   - You'll see a lazy.nvim window showing installation progress
   - Once complete, press `q` to close the window
   - Restart Neovim

## 📦 Plugin Manager: lazy.nvim

### Basic Commands

- `:Lazy` - Open lazy.nvim UI
- `:Lazy install` - Install missing plugins
- `:Lazy update` - Update plugins
- `:Lazy sync` - Install missing and update plugins
- `:Lazy clean` - Remove unused plugins
- `:Lazy check` - Check for updates

## 🛠️ LSP Configuration

### Installed Language Servers

- **Pyright** - Python LSP
- **lua_ls** - Lua LSP
- **bashls** - Bash LSP
- **jsonls** - JSON LSP
- **yamlls** - YAML LSP

### LSP Keybindings

| Key | Action |
|-----|--------|
| `gd` | Go to definition |
| `gD` | Go to declaration |
| `gr` | Go to references |
| `gi` | Go to implementation |
| `K` | Show hover documentation |
| `<leader>rn` | Rename symbol |
| `<leader>ca` | Code action |
| `<leader>f` | Format document |
| `[d` | Previous diagnostic |
| `]d` | Next diagnostic |
| `<leader>q` | Show diagnostic list |
| `<leader>d` | Show diagnostic float |

### Mason Commands

- `:Mason` - Open Mason UI to manage LSP servers, linters, formatters
- `:MasonInstall <package>` - Install a package
- `:MasonUninstall <package>` - Uninstall a package

## 🐛 Debugging (DAP)

### Installed Debuggers

- **debugpy** - Python debugger

### Debug Keybindings

| Key | Action |
|-----|--------|
| `<F5>` | Start/Continue debugging |
| `<F10>` | Step over |
| `<F11>` | Step into |
| `<F12>` | Step out |
| `<leader>b` | Toggle breakpoint |
| `<leader>B` | Set conditional breakpoint |
| `<leader>dr` | Open debug REPL |
| `<leader>dl` | Run last debug session |
| `<leader>dt` | Terminate debug session |
| `<leader>du` | Toggle debug UI |
| `<leader>de` | Evaluate expression |

### Python Debugging Configurations

Pre-configured debug profiles:
1. **Launch file** - Debug current Python file
2. **Launch file with arguments** - Debug with custom arguments
3. **Django** - Debug Django applications
4. **FastAPI** - Debug FastAPI applications

## 🔍 Plugin Store (store.nvim)

Browse and install 5,500+ Neovim plugins through an intuitive UI.

- **Command**: `:Store` or `<leader>ps`
- Features:
  - Search plugins by name, tags, author
  - Live README preview
  - Automatic installation with lazy.nvim

## 🎨 Colorscheme: Dracula

The Dracula theme is automatically applied on startup.

### Change colorscheme temporarily
```vim
:colorscheme dracula
```

### Customize colors
Edit `~/.config/nvim/lua/plugins/colorscheme.lua`

## ⌨️ Key Mappings Reference

### Normal Mode

| Key | Action |
|-----|--------|
| `<leader>` | `,` (comma) |
| `jj` / `kk` | Exit insert mode |
| `j` / `k` | Move by visual line |
| `B` / `E` | Beginning/end of line |
| `<C-N/K/L/H>` | Navigate splits |
| `<C-w>` | Save file |
| `<C-y>` | Last active tab |
| `<leader><space>` | Clear search highlight |
| `<leader>s` | Save session |
| `<leader>e` | Toggle file explorer |
| `<leader>ff` | Find files |
| `<leader>fg` | Live grep |
| `<leader>fb` | Find buffers |
| `<leader>fh` | Help tags |

### Visual Mode

| Key | Action |
|-----|--------|
| `<` / `>` | Indent left/right (stays in visual) |
| `J` / `K` | Move selection up/down |
| `p` | Paste without yanking |

## 🐍 Python Development

### Features
- **LSP**: Pyright for intelligent code completion, type checking
- **Debugging**: Full DAP support with debugpy
- **Formatting**: Black (auto-install via Mason)
- **Linting**: Pylint, Mypy (auto-install via Mason)
- **Import sorting**: isort (auto-install via Mason)

### Python-specific settings
- Tab size: 4 spaces
- Text width: 79 characters
- Auto-indent enabled
- Unix file format

### Workflow

1. **Open a Python file**
   ```bash
   nvim myfile.py
   ```

2. **LSP will automatically attach** and provide:
   - Autocompletion as you type
   - Diagnostic errors/warnings
   - Hover documentation with `K`

3. **Set a breakpoint**: `<leader>b`

4. **Start debugging**: `<F5>`

5. **Format code**: `<leader>f`

## 🔧 Customization

### Change leader key
Edit `~/.config/nvim/init.lua`:
```lua
vim.g.mapleader = ","  -- Change to your preferred key
```

### Add/remove plugins
Create a new file in `~/.config/nvim/lua/plugins/` or edit existing ones:
```lua
return {
  "author/plugin-name",
  -- optional configuration
  config = function()
    -- plugin setup
  end,
}
```

### Modify settings
Edit `~/.config/nvim/lua/config/settings.lua`

### Add keymaps
Edit `~/.config/nvim/lua/config/keymaps.lua`

## 🆘 Troubleshooting

### Plugins not installing
```vim
:Lazy sync
:Lazy clean
```

### LSP not working
```vim
:LspInfo          " Check LSP status
:Mason            " Install/reinstall LSP servers
:checkhealth lsp  " Diagnose LSP issues
```

### Treesitter parsing errors
```vim
:TSUpdate         " Update parsers
:checkhealth nvim-treesitter
```

### Python debugger not working
1. Ensure debugpy is installed:
   ```vim
   :Mason
   ```
   Find and install "debugpy"

2. Check DAP configuration:
   ```vim
   :checkhealth dap
   ```

### Performance issues
Edit `~/.config/nvim/lua/plugins/treesitter.lua`:
```lua
auto_install = false  -- Disable auto-install
```

## 📚 Learning Resources

- **Neovim documentation**: `:help`
- **lazy.nvim**: `:help lazy.nvim`
- **LSP**: `:help lsp`
- **DAP**: `:help dap`
- **Treesitter**: `:help treesitter`

## 🔄 Migration from Vim

All your original .vimrc settings have been migrated:

✅ Tab and indentation settings  
✅ Search behavior  
✅ Window navigation  
✅ Custom keymaps (jj/kk to escape, B/E for line navigation)  
✅ Split navigation (Ctrl+N/K/L/H)  
✅ Save with Ctrl-W  
✅ Python-specific settings  
✅ Trailing whitespace highlighting  
✅ Spell checking  
✅ Cursor line highlighting  
✅ Folding configuration  

## 📝 Notes

- **First launch** may take 2-3 minutes while everything installs
- **Python virtual environments** are automatically detected
- **Git integration** works out of the box if in a git repository
- **Session management**: Use `<leader>s` to save sessions, then `nvim -S` to restore

## 🎯 Next Steps

1. **Learn the keybindings**: Run `:WhichKey` to see available shortcuts
2. **Explore plugins**: Browse with `:Store` or `:Lazy`
3. **Configure LSP for other languages**: Add them in `~/.config/nvim/lua/plugins/lsp.lua`
4. **Customize appearance**: Modify colorscheme settings
5. **Add your own plugins**: Create files in `~/.config/nvim/lua/plugins/`

Happy coding! 🚀
