#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install this Neovim configuration.
#
# The config is tracked in git (dotfiles/nvim in the stacks repo). This script
# symlinks that checkout to ~/.config/nvim, so the git working tree *is* the
# live configuration: edit it in the repo and Neovim picks the change up
# immediately, with nothing to copy back and forth.
#
# Safe to re-run: it replaces only its own symlink and backs up any real
# directory it finds first.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
TARGET="$CONFIG_HOME/nvim"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { printf '%b\n' "${BLUE}[INFO]${NC} $*"; }
ok()   { printf '%b\n' "${GREEN}[ OK ]${NC} $*"; }
warn() { printf '%b\n' "${YELLOW}[WARN]${NC} $*"; }
err()  { printf '%b\n' "${RED}[FAIL]${NC} $*" >&2; }

check_prerequisites() {
	info "Checking prerequisites..."

	local missing=()

	if command -v nvim >/dev/null 2>&1; then
		ok "neovim $(nvim --version | head -n1 | awk '{print $2}')"
	else
		err "neovim not found"
		missing+=("neovim")
	fi

	if command -v git >/dev/null 2>&1; then
		ok "$(git --version)"
	else
		err "git not found"
		missing+=("git")
	fi

	# Optional, but used by parts of the config.
	if command -v node >/dev/null 2>&1; then
		ok "node $(node --version)"
	else
		warn "node not found (some LSP servers and tools need it)"
	fi

	if command -v python3 >/dev/null 2>&1; then
		ok "python3 $(python3 --version 2>&1)"
	else
		warn "python3 not found (Python LSP and debugging need it)"
	fi

	if command -v gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1; then
		ok "C compiler found"
	else
		warn "no C compiler (Treesitter parsers need one)"
	fi

	if command -v rg >/dev/null 2>&1; then
		ok "ripgrep $(rg --version | head -n1 | awk '{print $2}')"
	else
		warn "ripgrep (rg) not found (used for live grep)"
	fi

	if ((${#missing[@]})); then
		err "Missing required dependencies: ${missing[*]}"
		exit 1
	fi
}

link_config() {
	mkdir -p "$CONFIG_HOME"

	if [[ -L "$TARGET" ]]; then
		local current
		current="$(readlink -f -- "$TARGET")"
		if [[ "$current" == "$SCRIPT_DIR" ]]; then
			ok "already linked: $TARGET -> $SCRIPT_DIR"
			return 0
		fi
		warn "replacing existing symlink ($current)"
		rm -- "$TARGET"
	elif [[ -e "$TARGET" ]]; then
		local backup="${TARGET}.backup.$(date +%Y%m%d_%H%M%S)"
		warn "existing config found; backing it up to $backup"
		mv -- "$TARGET" "$backup"
	fi

	ln -s -- "$SCRIPT_DIR" "$TARGET"
	ok "linked $TARGET -> $SCRIPT_DIR"
}

main() {
	echo "Neovim configuration installer"
	echo "Source: $SCRIPT_DIR"
	echo

	check_prerequisites
	link_config

	cat <<'EOF'

Done. Next steps:
  1. Launch Neovim:      nvim
     lazy.nvim bootstraps itself and installs plugins on first launch.
  2. Restart Neovim once the plugins have finished installing.
  3. Check health:       :checkhealth

Note: lazy.nvim plugin data lives in ~/.local/share/nvim and is untouched
by this script.
EOF
}

main "$@"
