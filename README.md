# stacks

Docker Compose configurations for a homelab running on a single host
(`/opt/stacks`). Each subdirectory is one stack. All secrets are kept
outside this repository in `/opt/secrets/<stack>.env` and loaded via
`env_file` — nothing secret is ever committed.

## Layout

| Stack              | What it is                                            |
| ------------------ | ----------------------------------------------------- |
| `audiobookshelf`   | Audiobookshelf + calibre-web (ebooks) + storyteller    |
| `cloudflare`       | Cloudflare Tunnel (remote access)                      |
| `freshrss`         | RSS reader                                             |
| `immich`           | Photo library (server, ML, redis, postgres)            |
| `jellyfin`         | Media server                                           |
| `llama-cpp`        | llama.cpp OpenAI-compatible server (GPU)               |
| `litellm`          | LiteLLM proxy with idle-stop / on-demand start         |
| `mcp-searxng`      | SearXNG MCP server for AI assistants                   |
| `memos`            | Lightweight note-taking                                 |
| `navidrome`        | Music streaming                                        |
| `ntfy`             | Push notifications                                     |
| `ollama`           | Ollama model runner (ROCm)                             |
| `paperless`        | Document management (paperless-ngx)                    |
| `portainer`        | Container management UI                                |
| `trilium`          | TriliumNext notes                                       |
| `watchtower`       | Automatic container image updates                      |

## Secrets

Secrets live in `/opt/secrets/<stack>.env`, loaded by compose via
`env_file`. They are not part of this repository.

- [x] git-secret-hunter (gitleaks) pre-commit hook runs on every commit
- `.gitignore` excludes `.env*`, data/runtime directories, and backups
- `*.env.example` files are committed instead with placeholder values

## llama-server (llama-cpp stack)

`llama-cpp` runs [llama.cpp](https://github.com/ggml-org/llama.cpp)'s
OpenAI-compatible server (`server-rocm` image) on a ROCm GPU. It serves
the multimodal Qwen3.8-27B model with a draft (speculative decoding)
model and is consumed by `litellm` as its only model backend.

### Model download

The server reads models from `/opt/stacks/llama-cpp/models` (mounted
read-only into the container at `/models`). `.env` selects them via
`LLAMA_MODEL`, `LLAMA_MMPROJ` and `LLAMA_ALIAS`.

Download and place them there using the Hugging Face CLI, for example:

```bash
cd /opt/stacks/llama-cpp/models

# Main model (IQ3_S quant for 24 GB VRAM)
hf download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-IQ3_S.gguf --local-dir .

# Vision projector: required for image support (multimodal)
# Both files must be present, or llama-server fails to start
# (--mmproj points at a file that does not exist).
hf download unsloth/Qwen3.8-27B-GGUF mmproj-F16.gguf --local-dir .
```

`hf` ships with `huggingface_hub`; install it if missing:
`pip install -U huggingface_hub` (Python 3.8+). Alternatively use `wget`
with `https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/<file>`.

Then point `LLAMA_MODEL` / `LLAMA_MMPROJ` in `llama-cpp/.env` at the
downloaded files and restart:

```bash
docker compose -f llama-cpp/compose.yml up -d
```

### Configuration

All knobs are environment variables in `llama-cpp/.env`
(a copy of the template in `llama-cpp/.env.example`):

| Variable                | Meaning                                          |
| ----------------------- | ------------------------------------------------- |
| `LLAMA_MODEL`           | GGUF file served at `/models/${LLAMA_MODEL}`     |
| `LLAMA_MMPROJ`          | Vision projector GGUF (multimodal)               |
| `LLAMA_ALIAS`           | Model alias used by clients (e.g. `qwen3.8:27b`) |
| `LLAMA_CTX_SIZE`        | Context window in tokens                         |
| `LLAMA_PARALLEL`        | Parallel request slots                           |
| `LLAMA_CACHE_TYPE_K/V`  | KV cache quantization                            |
| `LLAMA_FIT` / `LLAMA_FIT_TARGET` | `off` disables auto-fit, `LLAMA_N_GPU_LAYERS` then sets layer offload |
| `LLAMA_N_GPU_LAYERS`    | Layers offloaded to GPU (99 = all)               |
| `LLAMA_FLASH_ATTN`      | Flash attention on/off                           |
| `LLAMA_TEMP`/`LLAMA_TOP_P`/`LLAMA_TOP_K`/`LLAMA_MIN_P`/`LLAMA_PRESENCE_PENALTY` | Sampling parameters        |
| `LLAMA_SPEC_DRAFT_N_MAX`| Speculative-decoding draft length                |
| `LLAMA_IMAGE_MIN_TOKENS`| Min image tokens (vision)                        |

The container exposes the OpenAI-compatible API on
`${LLAMA_HOST_IP}:8084` (port 8084 on the host → 8080 in the container).

### Idle management

`llama-server` is stopped when idle and started on demand:

- The litellm custom callback (`litellm/custom_callbacks.py`) starts the
  container via the Portainer API (`PORTAINER_API_KEY`,
  `PORTAINER_ENV_ID` from `/opt/secrets/portainer.env`) when a request
  arrives and polls `/health` until the model is loaded.
- A background loop stops the container again after `IDLE_TIMEOUT`
  (900 s) without requests.
- `IDLE_TIMEOUT`, `BOOT_TIMEOUT` and `LLAMA_HEALTH_URL` are set in
  `litellm/compose.yml` environment.

## Setting up a new stack

1. Create `stack/compose.yml`.
2. Put any secrets in `/opt/secrets/stack.env` and reference them with
   `env_file: - /opt/secrets/stack.env`.
3. If you need to expose variables, copy `.env.example` and fill in
   `/opt/stacks/stack/.env`; keep placeholders in the example file.
4. Add ignore rules to `.gitignore` for any runtime/data directories.
5. `docker compose -f stack/compose.yml up -d`
