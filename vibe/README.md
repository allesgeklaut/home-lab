# Vibe CLI config (version-controlled)

Symlinked into the Vibe home, same convention as the opencode config
(`~/.config/opencode/...` -> repo). Per device:

- `~/.vibe/config.toml` -> `/opt/stacks/vibe/config.toml`
- `~/.vibe/agents`      -> `/opt/stacks/vibe/agents`
- `~/.vibe/skills`      -> `/opt/stacks/vibe/skills`
- `~/.vibe/.env`        -> `/opt/secrets/vibe.env`

Bootstrap a device with `scripts/setup-vibe.sh` (see bottom).

Edit `config.toml` / `agents/` / `skills/` here in the repo, not in `~/.vibe/`.
Skills are Vibe-side ports of the OpenCode skills: `vibe/skills/` is the
canonical copy for Vibe (loaded via the `~/.vibe/skills` symlink, a default
search path — no `skill_paths` needed). `.opencode/skills/` stays OpenCode's
own copy; do not edit it for Vibe changes. Vibe never reads `.opencode/`
from the project root: its OpenCode foreign-plugin adapter only applies to
folders inside the plugin search dirs (`~/.vibe/plugins/`,
`<project>/.vibe/plugins/`), neither of which exists here.

NOTE: vibe rewrites `config.toml` wholesale whenever it persists a setting
(model switch, thinking level, theme, migrations, approved commands appended
to the `[tools.bash]` allowlist) — and it resolves the symlink first, so the
write lands in this repo file by design. `setup-vibe.sh` therefore marks
`vibe/config.toml` as **skip-worktree**, and that runtime churn never shows
in `git status` (the live file still receives every change). Consequences:

- keep prose in this README — comments in `config.toml` are stripped on the
  next vibe persist anyway;
- keep top-level keys above the first `[[models]]` table (vibe's rewrite once
  stranded `skill_paths` inside a model entry);
- to commit a deliberate config change:

  ```bash
  git update-index --no-skip-worktree vibe/config.toml
  git add vibe/config.toml && git commit -m "vibe: ..." && git push
  git update-index --skip-worktree vibe/config.toml
  ```

  Unset the bit before `git pull`/`git merge` if the remote branch also
  changed the file, or checkout may refuse.

## Device requirements

- **Linux** (the script uses `readlink -f`; the MCP wrappers use `getent` only
  as a fallback when `HOME` is unset).
- The repo must live at **`/opt/stacks`** and secrets at **`/opt/secrets`** on
  every device (paths are hardcoded in `config.toml`; `setup-vibe.sh`'s
  `STACKS_DIR`/`SECRETS_DIR` overrides exist only for testing).
- LAN-only endpoints: the llamacpp provider (`192.168.0.46:4000`) and the
  trilium MCP (`192.168.0.46:9910`) only work on the LAN/VPN. comfyui is
  tailnet-routed (`COMFYUI_MCP_URL`), so it works anywhere.

## Secrets — two aggregates in /opt/secrets, no values in git

`/opt/secrets` (dir `0700`, files `0600`) holds two dotenv aggregates:

- **`mistral.env`** — litellm / Mistral Studio stack. Its `MISTRAL_API_KEY` is
  the shared litellm key, **not** the vibe-cli quota key. Used by the litellm
  stack and pinned for the `deep-research` MCP via `MISTRAL_API_KEY_FILE`.
  Keep it minimal: only the keys the litellm stack/Studio agent need.
- **`vibe.env`** — vibe CLI's own env. Its `MISTRAL_API_KEY` is the **vibe-cli
  quota key** (seeded by `setup-vibe.sh` from a pre-existing real `~/.vibe/.env`
  on first run, from its `~/.vibe/.env.bak` on later runs, or from
  `/opt/secrets/vibe.key`). `~/.vibe/.env` symlinks here, so vibe loads
  exactly these vars at startup.

Key-only files (`github.token`, `trilium.token`, ...) remain for opencode's
`{file:...}` references; `setup-vibe.sh` also copies them into `vibe.env` as
`KEY=VALUE` lines:

| Var in `vibe.env` | Source | Used by |
|---|---|---|
| `MISTRAL_API_KEY` | old `~/.vibe/.env` / `vibe.key` | mistral provider (**vibe quota**) |
| `GITHUB_TOKEN` | `github.token` | github MCP (`api_key_env`) |
| `TRILIUM_API_TOKEN` | `trilium.token` | trilium MCP (stdio) |
| `LITELLM_KEY` | `litellm.key` | llamacpp provider |
| `ENGRAM_CLOUD_TOKEN` | `engram-cloud.token` | engram MCP (stdio) |
| `COMFYUI_MCP_URL` | `comfyui-mcp.url` | comfyui MCP tailnet URL |
| `OPENAI_API_KEY` | *(optional — add manually if you use the gpt-6.* models)* | openai provider |

Rotation: re-running `setup-vibe.sh` **refreshes** each line from its source
file, so update the key-only file and re-run (or regenerate it from vibe.env:
`grep '^GITHUB_TOKEN=' /opt/secrets/vibe.env | cut -d= -f2- > /opt/secrets/github.token`).

Vibe loads exactly one dotenv file, `$VIBE_HOME/.env`, into its process env at
startup (a non-empty shell env var wins over the file — so do not export
`MISTRAL_API_KEY` in your shell). Vibe has **no** `{file:...}` interpolation
and no env expansion in MCP `url`, and stdio children get only a curated env
(`HOME, LOGNAME, PATH, SHELL, TERM, USER`), so entries needing secrets are
wrapped and extract **only their own variable** from `~/.vibe/.env`:

```toml
command = "bash"
args = ["-c", 'export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"; v="$(sed -n "s/^TRILIUM_API_TOKEN=//p" "$HOME/.vibe/.env")"; v="${v%\"}"; v="${v#\"}"; export TRILIUM_API_TOKEN="$v"; exec npx -y triliumnext-mcp']
```

`deep-research` is the exception: it unsets `MISTRAL_API_KEY` and pins
`MISTRAL_API_KEY_FILE` to `/opt/secrets/mistral.env` so it always uses the
litellm/Studio key, never the vibe quota key.

comfyui runs as stdio bridged from http via
`npx -y -p node@22 -p mcp-remote mcp-remote "$COMFYUI_MCP_URL"`
(`-p node@22` shadows system node 18, which current mcp-remote needs). The
service itself listens on `127.0.0.1:8011/mcp` on the host and
`https://<tailnet-host>:8444/mcp` tailnet-wide (see `comfyui-mcp/README.md`).

Never run `vibe mcp add <name> --url <secret-url>`: it persists the URL into
`~/.vibe/config.toml`, which is this repo file via the symlink.

## Setup on a new device

```bash
# 1. clone/pull the repo to /opt/stacks, then copy the key-only files from the server:
scp server:/opt/secrets/{github.token,trilium.token,litellm.key,engram-cloud.token,comfyui-mcp.url,vibe.key} /opt/secrets/
# 2. bootstrap:
bash /opt/stacks/scripts/setup-vibe.sh
```

The script creates `/opt/secrets` (mode 700) and `vibe.env` (mode 600) if
missing, seeds `MISTRAL_API_KEY` from a pre-existing real `~/.vibe/.env`, its
`.env.bak`, or `$VIBE_KEY_FILE`, copies/refreshes the key-only secrets, and
creates the four `~/.vibe` symlinks — moving pre-existing real files/dirs
aside to non-clobbering `*.bak` names — and sets the git skip-worktree bit on
`vibe/config.toml` (see the NOTE above). Idempotent; never prints secret values.

## Activating changes / validating

- running session: `/reload` (while idle); new session: automatic at startup
- validation: `vibe config schema` only *prints* the schema — it does not check
  your file. Real validation is the startup `Warnings` box (config issues,
  MCP load failures); `vibe -p ...` prints them on stderr. Set
  `strict_config = true` (or `VIBE_STRICT_CONFIG=1`) to make vibe refuse to
  start on an invalid config instead of warning.
