#!/usr/bin/env bash
# Enable this repository's tracked hooks for the current checkout.
set -euo pipefail

root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"

if [ -n "${HOME:-}" ] && [ -d "$HOME/.local/bin" ]; then
    PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v gitleaks >/dev/null 2>&1; then
    printf '%s\n' \
        "Install gitleaks first: https://github.com/gitleaks/gitleaks#installing" >&2
    exit 1
fi

configured="$(git -C "$root" config --get core.hooksPath || true)"
if [ -n "$configured" ] && [ "$configured" != "hooks" ]; then
    printf 'Refusing to replace existing core.hooksPath: %s\n' "$configured" >&2
    exit 1
fi

chmod +x "$root/hooks/pre-commit"
git -C "$root" config --local core.hooksPath hooks
printf '%s\n' "Enabled staged secret scanning via hooks/pre-commit."
