# LiteLLM Proxy (+ GPU lifecycle managers)

LiteLLM fronts the local and remote LLMs, and this stack also owns the
lifecycle of every GPU-holding container on the host.

## Services

| service | role |
|---|---|
| **litellm** | OpenAI-compatible proxy on `:4000`. Routes local llama.cpp, OpenCode Go, Mistral API (chat, Voxtral TTS/STT, embeddings), and ChatGPT subscription requests. |
| **chatgpt-images** | Docker-internal Images API shim using the existing ChatGPT subscription Responses route. No host port or OAuth-token access. |
| **comfyui-proxy** | ComfyUI-protocol lifecycle proxy on `:8189`. Open WebUI's ComfyUI image engine points here instead of at ComfyUI. |

Secrets: master key in `/opt/secrets/litellm.key`; provider credentials in
`/opt/secrets/litellm.env`; Mistral's `MISTRAL_API_KEY` in
`/opt/secrets/mistral.env`; Portainer credentials in
`/opt/secrets/portainer.env` (shared by both services).
Keep secret files outside the repository with restricted permissions (`0600`).
After changing an env file, recreate the LiteLLM container to load it;
a plain restart does not reload Compose `env_file` values.

## Mistral-hosted DeepSeek

`deepseek-v4.1-flash-mistral` routes to Mistral's
`deepseek-v4-1-flash-1017` using the existing `MISTRAL_API_KEY`.
It supports text, reasoning, and function calling (no vision on this provider).
The existing `deepseek-v4.1-flash` and `deepseek-v4-pro` Go routes are unchanged.

The route declares Mistral's standard USD pricing in `model_info` for LiteLLM
cost calculations: **$0.30 input, $0.007 cached input, and $1.20 output per
million tokens**. Cached-input discounts depend on upstream cache usage being
reported. Source: [Mistral model card](https://docs.mistral.ai/models/deepseek-v4-flash-1017).

This route uses Mistral Chat Completions, like the existing Mistral routes.
Mistral's new Responses API is optional; it does not require migrating these
routes or changing client URLs. A native upstream Responses migration should
be tested separately, especially multi-turn reasoning and tool calls.

## Response speed in Open WebUI

For new streamed chat replies, hover the **Info** button beneath the response
(the `chat.usage` control must be visible). `InferenceMetrics` adds:

- **Speed [tok/s]**: provider-reported completion tokens
  divided by full proxy request duration, including time before the first output.
- **First output [s]**: time until the first text, reasoning,
  refusal or tool-call delta; empty/role-only/tool-placeholder chunks are ignored.
- **Total time [s]**: time from the pre-call hook to that usage chunk.

These are **end-to-end proxy estimates, not GPU decode speed**. They include
network waits, routing, prefill and local cold starts. Reasoning is included to
the extent the provider includes it in `completion_tokens`; no tokens are guessed.
Metrics accompany existing usage chunks only, so missing usage, cancelled/failed
replies, and non-chat/native Responses/audio APIs may have no speed figures.
Tool follow-ups are separate requests: token totals can accumulate in WebUI, but
these timing/speed fields describe the latest model call, not the whole agent run.
Existing chats are not backfilled. No WebUI patch or rebuild is required.
Earlier replies retain the longer `proxy_*` field names they were saved with.

## ChatGPT subscription login

The `chatgpt/` provider uses the Codex access included in eligible ChatGPT
subscriptions, not separately billed OpenAI API credentials. Authenticate once
from a host terminal before adding model routes:

```sh
sh /opt/stacks/litellm/login-chatgpt.sh
```

Open the URL printed by the helper, sign in with your ChatGPT account, and enter
the displayed device code. If necessary, enable device-code login in ChatGPT's
security settings first. Never share the resulting tokens or `auth.json`.

The helper uses the pinned LiteLLM image in a temporary container, without
starting the proxy or exposing ports. Tokens persist in
`/opt/secrets/chatgpt/auth.json` (file mode `0600`, directory mode `0700`).
The proxy mounts that directory **read-write** at `/run/secrets/chatgpt` because
OAuth refresh rotates the tokens. Do not use a read-only mount or independent
copies of the same refresh token. The current LiteLLM image runs as root; if
switching to a non-root image, adjust directory/file ownership for its UID.

After login, select models available to the account, add the corresponding
`chatgpt/` routes to `config.yaml`, and recreate only LiteLLM:

```sh
docker compose -f /opt/stacks/litellm/compose.yml up -d --no-deps litellm
```

Configured subscription routes: `chatgpt-gpt-6.1-sol`, `chatgpt-gpt-6-astra`,
and `chatgpt-gpt-6-luna`. These use the native Responses
provider; LiteLLM bridges Open WebUI's chat-completions requests to it.

Do not enable routes before login: missing credentials can block deployment
initialization. All callers of these routes share the signed-in account's
subscription limits. This is not blanket access to every ChatGPT feature or
image generation. Re-run the helper when reauthentication is required.

### Subscription image generation (unofficial)

The native `chatgpt-gpt-6.1-sol` Responses route supports the `image_generation`
tool with the current account. LiteLLM 1.104.2 does not bridge that tool to the
Images API, so [`chatgpt_images/`](chatgpt_images/README.md) provides the small
authenticated compatibility layer used by Open WebUI's image and edit engines:

`Open WebUI -> chatgpt-images (/v1/images/generations, /v1/images/edits) -> LiteLLM /v1/responses`

Only LiteLLM owns OAuth login/refresh. The adapter mounts just the gateway key,
has no host port, uses the existing chat route, and has no paid-provider fallback.
This is an **unofficial, personal-use integration**, not an OpenAI-approved API.
Account access and subscription limits can change.

Open WebUI image generation and editing are currently back on local ComfyUI
(Qwen-Image-2.1-Turbo; ComfyUI settings were retained and have been restored).
The shim is kept as an alternative: run `chatgpt_images/configure_webui.py` to
switch Open WebUI's engines to `chatgpt-image`, and its rollback to come back.
Existing chat routes are unchanged. See the adapter README for setup, limits,
tests, and scoped rollback of WebUI settings.

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
- the proxy stops `GPU_CONTAINERS` (`llama-server`, `llama-companion`,
  `llama-bolt`) before starting `comfyui`.

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
| `POST /prompt` | wake ComfyUI (stopping the llama tenants first), **strip unused multi-reference `LoadImage` slots and their alpha joins** (`prompt_clean.py`), then reverse-proxy |
| `POST /interrupt`, `/queue`, `/api/upload/image` | wake ComfyUI (stopping the llama tenants first), then reverse-proxy |
| `GET /ws` | wake ComfyUI, connect the upstream websocket, **then** upgrade the client |
| `GET /object_info` | served from cache — never wakes the GPU |
| `GET /system_stats` | proxied when running, else `{"proxy":"up","comfyui":"down"}` |
| `GET /health` | this proxy's own health |
| other `GET`s | proxied when ComfyUI is running, else `503` — except `/history`, which answers `200 {}` while cold so client liveness probes (e.g. comfy-cli's) do not wake the GPU |

`POST /prompt` is sanitized: `LoadImage` nodes still on the sentinel
`multi_ref_unused.png` (unfilled multi-reference slots) and their dependent
`JoinImageWithAlpha` helpers are dropped before forwarding. Used references
retain their alpha joins; dangling links are removed from consumer inputs.
The why lives in `prompt_clean.py`. Tests:
`cd comfyui_proxy && python3 -m unittest test_prompt_clean.py`.

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

Both active image engines use local ComfyUI with the plain Qwen-Image-2.1-Turbo
workflows (2048² generation, 1024 edit budget). The ChatGPT Images API shim
described above is an alternative, not the active backend.
