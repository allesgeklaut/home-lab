#!/bin/bash

# ============================================================================
# Neovim Configuration Installation Script
# ============================================================================

set -e

echo "======================================================================"
echo "  Neovim Configuration Installation"
echo "======================================================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    print_info "Checking prerequisites..."
    
    local missing_deps=()
    
    # Check Neovim version
    if command -v nvim &> /dev/null; then
        NVIM_VERSION=$(nvim --version | head -n1 | cut -d' ' -f2 | cut -d'v' -f2)
        print_success "Neovim found: v$NVIM_VERSION"
    else
        missing_deps+=("neovim")
        print_error "Neovim not found"
    fi
    
    # Check Git
    if command -v git &> /dev/null; then
        print_success "Git found: $(git --version)"
    else
        missing_deps+=("git")
        print_error "Git not found"
    fi
    
    # Check Node.js (optional but recommended)
    if command -v node &> /dev/null; then
        print_success "Node.js found: $(node --version)"
    else
        print_warning "Node.js not found (optional, but recommended for some LSP servers)"
    fi
    
    # Check Python
    if command -v python3 &> /dev/null; then
        print_success "Python3 found: $(python3 --version)"
    else
        print_warning "Python3 not found (required for Python development)"
    fi
    
    # Check C compiler
    if command -v gcc &> /dev/null || command -v clang &> /dev/null; then
        print_success "C compiler found"
    else
        print_warning "C compiler not found (required for Treesitter)"
    fi
    
    # Check ripgrep (optional)
    if command -v rg &> /dev/null; then
        print_success "Ripgrep found: $(rg --version | head -n1)"
    else
        print_warning "Ripgrep not found (optional, for Telescope live grep)"
    fi
    
    if [ ${#missing_deps[@]} -ne 0 ]; then
        print_error "Missing required dependencies: ${missing_deps[*]}"
        echo ""
        echo "Please install the missing dependencies and run this script again."
        exit 1
    fi
    
    echo ""
}

# Backup existing configuration
backup_existing_config() {
    print_info "Checking for existing Neovim configuration..."
    
    if [ -d "$HOME/.config/nvim" ]; then
        BACKUP_DIR="$HOME/.config/nvim.backup.$(date +%Y%m%d_%H%M%S)"
        print_warning "Existing configuration found. Creating backup..."
        mv "$HOME/.config/nvim" "$BACKUP_DIR"
        print_success "Backup created at: $BACKUP_DIR"
    fi
    
    if [ -d "$HOME/.local/share/nvim" ]; then
        BACKUP_DATA_DIR="$HOME/.local/share/nvim.backup.$(date +%Y%m%d_%H%M%S)"
        print_warning "Existing Neovim data found. Creating backup..."
        mv "$HOME/.local/share/nvim" "$BACKUP_DATA_DIR"
        print_success "Backup created at: $BACKUP_DATA_DIR"
    fi
    
    echo ""
}

# Create directory structure
create_directories() {
    print_info "Creating directory structure..."
    
    mkdir -p "$HOME/.config/nvim/lua/config"
    mkdir -p "$HOME/.config/nvim/lua/plugins"
    
    print_success "Directories created"
    echo ""
}

# Display final instructions
show_final_instructions() {
    echo ""
    echo "======================================================================"
    print_success "Installation directory structure created!"
    echo "======================================================================"
    echo ""
    echo "Next steps:"
    echo ""
    echo "1. Copy the configuration files to the following locations:"
    echo "   - init.lua → ~/.config/nvim/init.lua"
    echo "   - settings.lua → ~/.config/nvim/lua/config/settings.lua"
    echo "   - keymaps.lua → ~/.config/nvim/lua/config/keymaps.lua"
    echo "   - autocmds.lua → ~/.config/nvim/lua/config/autocmds.lua"
    echo "   - colorscheme.lua → ~/.config/nvim/lua/plugins/colorscheme.lua"
    echo "   - store.lua → ~/.config/nvim/lua/plugins/store.lua"
    echo "   - treesitter.lua → ~/.config/nvim/lua/plugins/treesitter.lua"
    echo "   - lsp.lua → ~/.config/nvim/lua/plugins/lsp.lua"
    echo "   - mason.lua → ~/.config/nvim/lua/plugins/mason.lua"
    echo "   - nvim-cmp.lua → ~/.config/nvim/lua/plugins/nvim-cmp.lua"
    echo "   - dap.lua → ~/.config/nvim/lua/plugins/dap.lua"
    echo "   - extras.lua → ~/.config/nvim/lua/plugins/extras.lua"
    echo ""
    echo "2. Launch Neovim:"
    echo "   $ nvim"
    echo ""
    echo "3. Wait for plugins to install automatically (first launch)"
    echo ""
    echo "4. Restart Neovim after installation completes"
    echo ""
    echo "5. Check health:"
    echo "   :checkhealth"
    echo ""
    echo "For more information, see the README.md file."
    echo ""
    echo "======================================================================"
    echo "  Directory structure: ~/.config/nvim/"
    echo "======================================================================"
    tree -L 3 "$HOME/.config/nvim" 2>/dev/null || find "$HOME/.config/nvim" -type d | sed 's|[^/]*/| |g'
    echo ""
}

# Main installation
main() {
    check_prerequisites
    backup_existing_config
    create_directories
    show_final_instructions
}

# Run main function
main
