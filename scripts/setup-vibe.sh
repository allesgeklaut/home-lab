#!/usr/bin/env bash
# setup-vibe.sh — per-device bootstrap for the version-controlled Vibe config
# in /opt/stacks/vibe (see /opt/stacks/vibe/README.md).
#
# Secrets model — two aggregates in /opt/secrets, no secret values in git:
#   - mistral.env : litellm / Studio stack. Its MISTRAL_API_KEY is the shared
#     litellm key, NOT the vibe-cli quota key (used by litellm + deep-research).
#   - vibe.env    : vibe CLI's own env; its MISTRAL_API_KEY is the vibe-cli
#     quota key. ~/.vibe/.env symlinks to this file.
#
# This script:
# 1. Creates /opt/secrets/vibe.env (mode 600) if missing and seeds its
#    MISTRAL_API_KEY from ~/.vibe/.env.bak (a previous real ~/.vibe/.env) or
#    from $VIBE_KEY_FILE (default /opt/secrets/vibe.key).
# 2. Appends the key-only secrets below as KEY=VALUE lines (refreshes existing
#    lines from the source files, so rotation propagates; values are never
#    printed — printf is a bash builtin, so they never appear in any
#    process's cmdline or in this script's output).
# 3. Symlinks ~/.vibe/config.toml, ~/.vibe/agents and ~/.vibe/.env into the
#    repo and vibe.env. Pre-existing regular files are backed up to *.bak.
#
# Run from YOUR OWN terminal:  bash /opt/stacks/scripts/setup-vibe.sh
# (override locations with STACKS_DIR / SECRETS_DIR / VIBE_HOME / VIBE_KEY_FILE)
set -euo pipefail

STACKS_DIR=${STACKS_DIR:-/opt/stacks}
SECRETS_DIR=${SECRETS_DIR:-/opt/secrets}
VIBE_DIR=${VIBE_HOME:-$HOME/.vibe}
ENV_FILE="$SECRETS_DIR/vibe.env"
VIBE_KEY_FILE=${VIBE_KEY_FILE:-$SECRETS_DIR/vibe.key}

echo "== 1. $ENV_FILE =="
if [ ! -d "$SECRETS_DIR" ]; then
  mkdir -p -m 700 "$SECRETS_DIR"
  echo "  created $SECRETS_DIR (mode 700)"
fi
if [ ! -f "$ENV_FILE" ]; then
  (umask 077 && : > "$ENV_FILE")
  echo "  created $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# seed the vibe-cli quota key under the var name vibe's mistral provider reads.
# Prefer a pre-existing real ~/.vibe/.env (first run, before link() moves it
# to .env.bak), then the .bak, then VIBE_KEY_FILE.
if ! grep -q '^MISTRAL_API_KEY=' "$ENV_FILE"; then
  if [ -f "$VIBE_DIR/.env" ] && [ ! -L "$VIBE_DIR/.env" ] && grep -q '^MISTRAL_API_KEY=' "$VIBE_DIR/.env"; then
    grep '^MISTRAL_API_KEY=' "$VIBE_DIR/.env" >> "$ENV_FILE"
    echo "  MISTRAL_API_KEY: seeded from $VIBE_DIR/.env (verify it is the vibe-cli quota key!)"
  elif [ -f "$VIBE_DIR/.env.bak" ] && grep -q '^MISTRAL_API_KEY=' "$VIBE_DIR/.env.bak"; then
    grep '^MISTRAL_API_KEY=' "$VIBE_DIR/.env.bak" >> "$ENV_FILE"
    echo "  MISTRAL_API_KEY: seeded from $VIBE_DIR/.env.bak"
  elif [ -f "$VIBE_KEY_FILE" ]; then
    printf 'MISTRAL_API_KEY=%s\n' "$(cat "$VIBE_KEY_FILE")" >> "$ENV_FILE"
    echo "  MISTRAL_API_KEY: seeded from $VIBE_KEY_FILE"
  else
    echo "  MISTRAL_API_KEY: NOT seeded — add the vibe-cli quota key to $ENV_FILE yourself" >&2
  fi
fi

add() {
  local var=$1 f=$2 content tmp
  if [ ! -f "$SECRETS_DIR/$f" ]; then
    if grep -q "^${var}=" "$ENV_FILE"; then
      echo "  $var: already present, skipping"
    else
      echo "  $var: WARNING $SECRETS_DIR/$f missing, skipping" >&2
    fi
    return 0
  fi
  content=$(cat "$SECRETS_DIR/$f")   # shell memory only, never echoed
  if [[ $content =~ ^[A-Za-z_][A-Za-z_0-9]*= ]]; then
    content="${content#*=}"           # file was already KEY=VALUE: re-key to $var
  fi
  content="${content%\"}"; content="${content#\"}"   # strip one layer of quotes
  content="${content%\'}"; content="${content#\'}"
  if grep -q "^${var}=" "$ENV_FILE"; then
    # refresh in place so rotated source files propagate (perms kept via mktemp+mv)
    tmp=$(mktemp)
    grep -v "^${var}=" "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$var" "$content" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
    echo "  $var: refreshed from $f"
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

mkdir -p "$VIBE_DIR"

echo "== 2. symlinks =="
backup() {  # move aside without clobbering: f -> f.bak, f.bak.1, f.bak.2, ...
  local f=$1
  local bak="$f.bak"
  local n=1
  while [ -e "$bak" ] || [ -L "$bak" ]; do bak="$f.bak.$n"; n=$((n+1)); done
  mv "$f" "$bak"          # works for regular files, directories and symlinks
  echo "$bak"
}

link() {
  local target=$1 link=$2 old bak
  if [ -L "$link" ]; then
    old=$(readlink "$link")
    if [ "$(readlink -f "$link")" = "$(readlink -f "$target")" ]; then
      echo "  ok: $link -> $target"
    else
      rm -f "$link"
      ln -s "$target" "$link"
      echo "  relinked: $link -> $target (was -> $old)"
    fi
  elif [ -e "$link" ]; then
    bak=$(backup "$link")
    ln -s "$target" "$link"
    echo "  linked: $link -> $target (previous entry saved as $bak)"
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
