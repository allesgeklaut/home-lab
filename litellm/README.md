# LiteLLM Proxy (+ GPU lifecycle managers)

LiteLLM fronts the local and remote LLMs, and this stack also owns the
lifecycle of every GPU-holding container on the host.

## Services

| service | role |
|---|---|
| **litellm** | OpenAI-compatible proxy on `:4000`. Routes chat completions to the local llama.cpp servers and to the OpenCode Go gateway. |
| **comfyui-proxy** | ComfyUI-protocol lifecycle proxy on `:8189`. Open WebUI's ComfyUI image engine points here instead of at ComfyUI. |

Secrets: master key in `/opt/secrets/litellm.env`; Portainer credentials in
`/opt/secrets/portainer.env` (shared by both services).

## GPU lifecycle

The RX 9060 XT has 16 GB, so only one container may hold the GPU at a time.
Two managers cover two different traffic paths, because neither sees the
other's requests:

- **`LlamaCppIdleManager`** (`custom_callbacks.py`) — LLM traffic flows
  *through* litellm, so `async_pre_call_hook` cold-starts the requested
  llama.cpp container and a background watcher stops it after `IDLE_TIMEOUT`
  (900 s) without requests. Configured via `MODEL_CONTAINERS`.
- **`comfyui-proxy`** (`comfyui_proxy/app.py`) — image traffic never touches
  litellm (Open WebUI speaks ComfyUI's own protocol), so a proxy in front of
  ComfyUI does the same job.

Each manager stops the other's tenants before starting its own:

- litellm stops `EXTRA_GPU_CONTAINERS` (`comfyui`) before a llama cold start.
- the proxy stops `GPU_CONTAINERS` (`llama-server`, `llama-companion`) before
  starting `comfyui`.

Both call Portainer through the shared `gpu_containers.py` helpers. There is no
shared lock — with a single user the window for two simultaneous switches is
negligible.

`ollama` is deliberately **not** managed: it serves cloud models only and never
holds the GPU.

## comfyui-proxy

Sits between Open WebUI and ComfyUI so `comfyui` can be stopped when idle and
started on demand. `comfyui/compose.yml` therefore sets `restart: "no"` — the
proxy owns it.

| request | behaviour |
|---|---|
| `POST /prompt`, `/interrupt`, `/queue`, `/api/upload/image` | wake ComfyUI (stopping the llama tenants first), then reverse-proxy |
| `GET /ws` | wake ComfyUI, connect the upstream websocket, **then** upgrade the client |
| `GET /object_info` | served from cache — never wakes the GPU |
| `GET /system_stats` | proxied when running, else `{"proxy":"up","comfyui":"down"}` |
| `GET /health` | this proxy's own health |
| other `GET`s | proxied when ComfyUI is running, else `503` — except `/history`, which answers `200 {}` while cold so client liveness probes (e.g. comfy-cli's) do not wake the GPU |

Two non-obvious details:

- **`/object_info` is answered from cache** so that Open WebUI's model dropdown
  and its "Verify URL" button do not start the GPU. Only the model node's
  `*_name` list is read by Open WebUI, so the static seed in `app.py` is enough
  for the very first call; the cache is then refreshed on every successful
  proxy. The trade-off: Verify URL reports success even when ComfyUI is
  stopped.
- **The websocket `101` is delayed** until ComfyUI is up and the upstream ws is
  connected. Open WebUI's client has no ws handshake timeout
  (`ClientTimeout(total=None)`), and this ordering guarantees it cannot send
  `POST /prompt` before the proxy can receive completion events — otherwise the
  generation would hang waiting for `executing`.

Tunables (compose-interpolated, overridable in `.env`):
`COMFYUI_IDLE_TIMEOUT` (900), `COMFYUI_BOOT_TIMEOUT` (90).

### Open WebUI wiring

`image_generation.comfyui.base_url` and `images.edit.comfyui.base_url` point at
`http://<LAN_IP>:8189` (the proxy), not at ComfyUI's `:8188`. See
`../webui/README.md`.
