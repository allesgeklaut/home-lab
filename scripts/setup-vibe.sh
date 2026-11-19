#!/usr/bin/env bash
# setup-vibe.sh — per-device bootstrap for the version-controlled Vibe config
# in /opt/stacks/vibe (see /opt/stacks/vibe/README.md).
#
# 1. Copies key-only secrets from /opt/secrets into the aggregate dotenv
#    /opt/secrets/mistral.env as KEY=VALUE lines. Idempotent. Secret values
#    are never printed: printf is a bash builtin, so values never appear in
#    any process's cmdline or in this script's output either.
# 2. Symlinks ~/.vibe/config.toml, ~/.vibe/agents and ~/.vibe/.env into
#    /opt/stacks/vibe and /opt/secrets/mistral.env. Pre-existing real files
#    are backed up to *.bak first.
#
# Run from YOUR OWN terminal:  bash /opt/stacks/scripts/setup-vibe.sh
# (override locations with STACKS_DIR / SECRETS_DIR / VIBE_HOME if needed)
set -euo pipefail

STACKS_DIR=${STACKS_DIR:-/opt/stacks}
SECRETS_DIR=${SECRETS_DIR:-/opt/secrets}
VIBE_DIR=${VIBE_HOME:-$HOME/.vibe}
ENV_FILE="$SECRETS_DIR/mistral.env"

echo "== 1. secrets -> $ENV_FILE =="
if [ ! -f "$ENV_FILE" ]; then echo "ERROR: $ENV_FILE missing" >&2; exit 1; fi
if [ ! -w "$ENV_FILE" ]; then echo "ERROR: $ENV_FILE not writable" >&2; exit 1; fi

add() {
  local var=$1 f=$2 content
  if grep -q "^${var}=" "$ENV_FILE"; then
    echo "  $var: already present, skipping"
    return 0
  fi
  if [ ! -f "$SECRETS_DIR/$f" ]; then
    echo "  $var: WARNING $SECRETS_DIR/$f missing, skipping" >&2
    return 0
  fi
  content=$(cat "$SECRETS_DIR/$f")   # shell memory only, never echoed
  if [[ $content =~ ^[A-Za-z_][A-Za-z_0-9]*= ]]; then
    # file is already KEY=VALUE: re-key it to $var
    printf '%s=%s\n' "$var" "${content#*=}" >> "$ENV_FILE"
    echo "  $var: appended (re-keyed from $f)"
  else
    printf '%s=%s\n' "$var" "$content" >> "$ENV_FILE"
    echo "  $var: appended from $f"
  fi
}

add GITHUB_TOKEN       github.token
add TRILIUM_API_TOKEN  trilium.token
add LITELLM_KEY        litellm.key
add ENGRAM_CLOUD_TOKEN engram-cloud.token
add COMFYUI_MCP_URL    comfyui-mcp.url

echo "  keys now in $ENV_FILE (names only):"
grep -o '^[A-Za-z_0-9]*=' "$ENV_FILE" | sort -u | sed 's/^/    /'

echo "== 2. symlinks =="
link() {
  local target=$1 link=$2
  if [ -L "$link" ] && [ "$(readlink -f "$link")" = "$(readlink -f "$target")" ]; then
    echo "  ok: $link -> $target"
  elif [ -e "$link" ] || [ -L "$link" ]; then
    cp -a "$link" "$link.bak"
    rm -f "$link"
    ln -s "$target" "$link"
    echo "  linked: $link -> $target (previous file saved as $link.bak)"
  else
    ln -s "$target" "$link"
    echo "  linked: $link -> $target"
  fi
}

link "$STACKS_DIR/vibe/config.toml" "$VIBE_DIR/config.toml"
link "$STACKS_DIR/vibe/agents"      "$VIBE_DIR/agents"
link "$ENV_FILE"                     "$VIBE_DIR/.env"

echo
echo "Done. In vibe: /reload (while idle) or restart, then check /mcp."
