# Vibe CLI config (version-controlled)

Symlinked into the Vibe home, same convention as the opencode config
(`~/.config/opencode/...` -> repo). Per device:

- `~/.vibe/config.toml` -> `/opt/stacks/vibe/config.toml`
- `~/.vibe/agents`      -> `/opt/stacks/vibe/agents`
- `~/.vibe/.env`        -> `/opt/secrets/mistral.env` (back up any pre-existing real `~/.vibe/.env` first)

Edit `config.toml` / `agents/` here in the repo, not in `~/.vibe/`.

NOTE: vibe rewrites `config.toml` wholesale whenever it persists a setting
(comments are stripped, sections reordered, approved commands appended to the
`[tools.bash]` allowlist). Keep prose documentation in this README, not as
comments in the TOML. Review `git diff vibe/config.toml` before committing.

## Secrets

No secret values live in this repo. Secrets stay in `/opt/secrets/`
(dir `0700`, files `0600`). Two shapes coexist there:

- `mistral.env` — aggregate dotenv (`KEY=VALUE`) for env-var consumers (vibe,
  deep-research, litellm stack). `~/.vibe/.env` symlinks to it.
- key-only files (`github.token`, `trilium.token`, `litellm.key`,
  `engram-cloud.token`, `comfyui-mcp.url`, ...) — used by opencode's
  `{file:/opt/secrets/...}` references, which need single-value files.

When rotating a key, update both, or regenerate the key-only file:

```bash
grep '^GITHUB_TOKEN=' /opt/secrets/mistral.env | cut -d= -f2- > /opt/secrets/github.token
```

Vibe loads exactly one dotenv file, `$VIBE_HOME/.env`, at startup and injects
it into its own process env (a non-empty shell env var wins over the file).
Env vars the config relies on (names only, never values):

| Var | Used by |
|---|---|
| `MISTRAL_API_KEY`, `DEEP_RESEARCH_AGENT_ID` | mistral provider, deep-research MCP |
| `GITHUB_TOKEN` | github MCP (`api_key_env`) |
| `TRILIUM_API_TOKEN` | trilium MCP (stdio) |
| `LITELLM_KEY` | llamacpp provider (`api_key_env_var`) |
| `ENGRAM_CLOUD_TOKEN` | engram MCP (stdio) |
| `COMFYUI_MCP_URL` | comfyui MCP tailnet URL (from `comfyui-mcp.url`) |

Vibe has **no** `{file:...}` interpolation (unlike opencode.json) and does not
expand env vars in MCP `url`, so:

- HTTP MCP auth uses `api_key_env` (env var name), never plaintext `headers`.
- stdio MCP children receive only a curated env (`PATH`, `HOME`, ...), so
  entries needing secrets are wrapped:
  `bash -c '[ -f ~/.vibe/.env ] && source ~/.vibe/.env; exec <cmd>'`
- comfyui: the service listens on `127.0.0.1:8011/mcp` on the host and
  `https://<tailnet-host>:8444/mcp` tailnet-wide (see `comfyui-mcp/README.md`).
  Since the URL is per-device, it comes from `COMFYUI_MCP_URL` and the server
  is bridged http -> stdio via `npx -y mcp-remote "$COMFYUI_MCP_URL"`.

Never run `vibe mcp add <name> --url <secret-url>`: it persists the URL into
`~/.vibe/config.toml`, which is this repo file via the symlink.

## Setup on a new device

```bash
bash /opt/stacks/scripts/setup-vibe.sh
```

Idempotent: appends the needed `KEY=VALUE` lines to `/opt/secrets/mistral.env`
(from the key-only files; values are never printed) and creates the three
`~/.vibe` symlinks listed above, backing up pre-existing real files to `*.bak`.

## Activating changes

- running session: `/reload` (works while idle)
- new session: automatic at startup
- validate: `vibe config schema`; set `strict_config = true` to refuse bad config
