# ChatGPT subscription Images API shim

**Unofficial, personal-use integration. No OpenAI approval is implied.** It uses
only the ChatGPT/Codex subscription behind the existing LiteLLM route. Access,
features, and quotas may change; it does not bypass authentication, access
controls, rate limits, or safety refusals.

## Flow and credentials

```text
Open WebUI (OpenAI image + edit engines)
  -> http://chatgpt-images:8180/v1/images/generations   (JSON, text-to-image)
  -> http://chatgpt-images:8180/v1/images/edits         (multipart, image edit)
  -> http://litellm:4000/v1/responses, model chatgpt-gpt-6.1-sol
  -> LiteLLM's native chatgpt/ provider
```

The image tool is pinned to **`gpt-image-2.5-sunburst`** (the quality-focused
GPT Image model) at reasoning effort low; the default `quality` is **high**
(`low`/`medium` also accepted). The coordinator model does not change the
underlying image model.

The adapter runs on the LiteLLM and existing `webui_default` Docker networks,
with **no published host port**. It requires the existing LiteLLM gateway Bearer
key on `/v1/*`; only a minimal `/health` response is unauthenticated. Never
publish it through a public reverse proxy or share the gateway key.

It mounts `/opt/secrets/litellm.key` read-only, **not** the OAuth token directory.
LiteLLM exclusively owns its existing login and refresh lifecycle. There are no
copies of OpenCode tokens, alternate upstreams, paid API credentials, redirects,
or automatic retries. The container is read-only, has all capabilities dropped,
and runs as UID/GID 1000 to match the gateway key's `0600` owner. Adjust that UID
if your host key has a different owner; do not loosen the secret's permissions.

No third-party plugin source was copied. This shim uses aiohttp and Pillow;
normal dependency licenses still apply.

## Start and configure

Authenticate the main LiteLLM provider first with `../login-chatgpt.sh` if needed.
Then, on this host:

```sh
docker compose -f /opt/stacks/litellm/compose.yml up -d --build --no-deps chatgpt-images
python3 /opt/stacks/litellm/chatgpt_images/configure_webui.py
```

The configuration helper updates only the Open WebUI image-generation and
image-edit settings in the current per-key SQLite config (14 keys): it points
both OpenAI image engines to `http://chatgpt-images:8180/v1`, selects
`chatgpt-image`, sets generation quality high with PNG output, and saves the
gateway key in WebUI's private config DB, like its other provider credentials.
**That DB and any backups are secrets; never commit them.** If the gateway key
rotates, rerun the helper and restart the adapter to reload it.

Chat settings are preserved. This **replaces local ComfyUI for both generation
and editing** while enabled; the ComfyUI workflows and settings remain on disk
and are restorable. (This was the active configuration 2026-10-18 → 2026-10-26;
ComfyUI is active again now — run this helper to re-enable the shim.) The
running WebUI reads these settings per request, so no restart is
needed — reload the browser to refresh its settings. The helper prints a private
rollback file path (`0700` directory, `0600` file), never its credential contents.

To restore the previous image settings (both engines):

```sh
python3 /opt/stacks/litellm/chatgpt_images/configure_webui.py --restore /path/printed/by/helper/previous.json
```

Keep the printed backup until the switch is accepted; `/tmp/opencode` may be
cleaned on reboot. Do not roll back the whole DB or discard newer chat data.
If the helper is re-run after a switch, the new backup contains the currently
stored gateway key — treat every rollback file as a secret and delete it once
it is no longer needed.

## Supported surface and limits

- `GET /v1/models`: advertises only `chatgpt-image`.
- `POST /v1/images/generations`: JSON; one image (`n=1`); PNG; `b64_json` only.
- `POST /v1/images/edits`: `multipart/form-data`; exactly one input image file
  (`image`/`image[]`), PNG, JPEG or WebP; one edit per request; PNG `b64_json` out.
- Quality: `low`, `medium`, `high` (default). No model switching or paid fallback.
- `size`: `auto` or dimensions from 256 to 4096, conveyed as a **prompt hint**.
  Exact dimensions are not guaranteed; native tests returned 1774x887 for a
  1536x1024 request and 1254x1254 for square requests. WebUI defaults to a
  1024x1024 hint.
- `background`: `auto`, `transparent`, or `opaque`, prompt-driven on both
  endpoints. Native alpha-zero output has been verified, including on an edit
  that preserved the input's transparency, but is not guaranteed for every
  prompt. No automatic background removal, resize, or palette conversion.
- Edits are prompt-driven (`action: edit`); no masks, no multiple inputs, no
  variations, no image URLs, no client streaming.
- 16 KiB JSON body / 8 KiB UTF-8 prompt; 17 MiB edit form cap; compressed
  request bodies rejected; prompt-less or duplicate form fields rejected.
- The container needs a writable `/tmp` (compose mounts a small tmpfs):
  aiohttp spools multipart file parts above ~1 MiB to disk, and the adapter's
  read-only root filesystem would otherwise fail real photos with
  `FileNotFoundError`.
- One generation or edit in flight (shared slot); excess requests get 429
  without being queued.
- 180-second total timeout, 90-second upstream idle timeout.
- Bounded SSE: 24 MiB/event, 48 MiB/stream. Requires a completed response and
  exactly one valid image result; never treats a truncated/refused stream as
  success. Input images are Pillow-verified (format, single frame, 16 MiB and
  16-megapixel caps); output is PNG-only, signature-checked and decoded.
- Disconnects cancel the adapter handler and close its upstream stream. This
  cannot guarantee OpenAI stops work already submitted or refunds quota usage.
- No prompts, tokens, image payloads, or raw provider exceptions in adapter logs.

## Tests

Offline tests use a fake local Responses server; no secrets or subscription
quota are needed:

```sh
docker run --rm --network none --read-only --tmpfs /tmp \
  -v /opt/stacks/litellm/chatgpt_images:/tests:ro -w /tests \
  chatgpt-images:local python -B -m unittest -v test_app
```

35 tests cover authentication, known-length and chunked body limits, generation
and edit parameter validation, pinned model/quality, edit file and field rules,
input-image validation, PNG/SSE limits, refusals/truncation, no redirects or
retries, redacted errors, concurrency across both endpoints, timeout/disconnect
propagation, and scoped WebUI configuration rollback. Real native Responses
image generation and editing (including a cyan-to-magenta recolor that preserved
1254x1254 dimensions and alpha-zero transparency) were verified separately
before implementing the adapter, and the full Open WebUI generation path passed a
live test. Raw SSE payloads and credentials were not saved in the repository.
