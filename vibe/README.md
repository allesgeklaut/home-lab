# Vibe CLI config (version-controlled)

Symlinked into the Vibe home, same convention as the opencode config
(`~/.config/opencode/...` -> repo). Per device:

- `~/.vibe/config.toml` -> `/opt/stacks/vibe/config.toml`
- `~/.vibe/agents`      -> `/opt/stacks/vibe/agents`
- `~/.vibe/.env`        -> `/opt/secrets/vibe.env`

Bootstrap a new device with `scripts/setup-vibe.sh` (see bottom).

Edit `config.toml` / `agents/` here in the repo, not in `~/.vibe/`.

NOTE: vibe rewrites `config.toml` wholesale whenever it persists a setting
(comments are stripped, sections reordered, approved commands appended to the
`[tools.bash]` allowlist). Keep prose in this README, keep top-level keys
above the first `[[models]]` table (vibe's rewrite once stranded
`skill_paths` inside a model entry), and review `git diff vibe/config.toml`
before committing.

## Secrets — two aggregates in /opt/secrets, no values in git

`/opt/secrets` (dir `0700`, files `0600`) holds two dotenv aggregates:

- **`mistral.env`** — litellm / Mistral Studio stack. Its `MISTRAL_API_KEY` is
  the shared litellm key, **not** the vibe-cli quota key. Used by the litellm
  stack and pinned for the `deep-research` MCP via `MISTRAL_API_KEY_FILE`.
- **`vibe.env`** — vibe CLI's own env. Its `MISTRAL_API_KEY` is the **vibe-cli
  quota key** (seeded by `setup-vibe.sh` from the previous real
  `~/.vibe/.env` or `/opt/secrets/vibe.key`). `~/.vibe/.env` symlinks here,
  so vibe loads exactly these vars at startup.

Key-only files (`github.token`, `trilium.token`, ...) remain for opencode's
`{file:...}` references; `setup-vibe.sh` also copies them into `vibe.env` as
`KEY=VALUE` lines:

| Var in `vibe.env` | Source | Used by |
|---|---|---|
| `MISTRAL_API_KEY` | old `~/.vibe/.env(.bak)` or `vibe.key` | mistral provider (**vibe quota**) |
| `GITHUB_TOKEN` | `github.token` | github MCP (`api_key_env`) |
| `TRILIUM_API_TOKEN` | `trilium.token` | trilium MCP (stdio) |
| `LITELLM_KEY` | `litellm.key` | llamacpp provider |
| `ENGRAM_CLOUD_TOKEN` | `engram-cloud.token` | engram MCP (stdio) |
| `COMFYUI_MCP_URL` | `comfyui-mcp.url` | comfyui MCP tailnet URL |

Rotation: update both the key-only file and the line in `vibe.env`, or
regenerate the file: `grep '^GITHUB_TOKEN=' /opt/secrets/vibe.env | cut -d= -f2- > /opt/secrets/github.token`

Vibe loads exactly one dotenv file, `$VIBE_HOME/.env`, into its process env at
startup (a non-empty shell env var wins over the file — so do not export
`MISTRAL_API_KEY` in your shell). Vibe has **no** `{file:...}` interpolation
and no env expansion in MCP `url`, and stdio children get only a curated env,
so entries needing secrets are wrapped:

```toml
command = "bash"
args = ["-c", 'export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"; set -a; [ -f "$HOME/.vibe/.env" ] && source "$HOME/.vibe/.env"; set +a; exec <cmd> ...']
```

(`set -a` is required — plain `source` does not export. `deep-research` is
the exception: it unsets `MISTRAL_API_KEY` and pins `MISTRAL_API_KEY_FILE` to
`/opt/secrets/mistral.env` so it always uses the litellm/Studio key.)

comfyui runs as stdio bridged from http via
`npx -y -p node@22 -p mcp-remote mcp-remote "$COMFYUI_MCP_URL"`
(`-p node@22` shadows system node 18, which current mcp-remote needs). The
service itself listens on `127.0.0.1:8011/mcp` on the host and
`https://<tailnet-host>:8444/mcp` tailnet-wide (see `comfyui-mcp/README.md`).

Never run `vibe mcp add <name> --url <secret-url>`: it persists the URL into
`~/.vibe/config.toml`, which is this repo file via the symlink.

## Setup on a new device

```bash
bash /opt/stacks/scripts/setup-vibe.sh
```

Creates/populates `/opt/secrets/vibe.env` and creates the three `~/.vibe`
symlinks, backing up pre-existing real files to `*.bak`.

## Activating changes

- running session: `/reload` (while idle); new session: automatic at startup
- validate: `vibe config schema`; set `strict_config = true` to refuse bad config
