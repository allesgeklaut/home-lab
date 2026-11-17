# ComfyUI MCP server

Shared Streamable-HTTP MCP server for the local ComfyUI stack. One container
serves opencode (over the tailnet) and Open WebUI (over the Docker network), and
exposes image generation/editing, LTX-2.5 video, first-frame image-to-video, and
a chat-sized GIF preset.

It talks to the **comfyui-proxy** in the litellm stack rather than to ComfyUI
directly, so the on-demand lifecycle is handled for it: every mutating request
wakes the `comfyui` container, and the proxy stops it again when idle. The proxy
also owns `/recycle` (clean process between model families) and `/view` (serves
saved output files while ComfyUI is stopped).

## Tools

| tool | what it does |
|---|---|
| `image_status` | proxy/ComfyUI state and the available diffusion models. Uses the proxy's cached `object_info`, so it never starts the GPU. |
| `generate_image(prompt, negative_prompt="", width=2048, height=2048, steps=40, seed=None)` | text to image on the Turbo UNet (official 8-step schedule; `steps` is ignored). |
| `edit_image(image_path, prompt, steps=40, seed=None, reference_images=None)` | instruction edit on Turbo, with up to nine extra reference paths. |
| `generate_video(prompt, quality="draft", size=None, seconds=None, seed=None, aspect_ratio="1:1")` | LTX-2.5 clip up to 10 s: 512-long-edge single pass (`draft`) or staged 1024 (`final`). |
| `generate_video_from_image(image_path, prompt, quality="draft", size=None, seconds=None, strength=0.7, seed=None, aspect_ratio="1:1")` | first-frame image-to-video with LTX-2.5, up to 10 s; same draft/final pipelines as `generate_video`. |
| `generate_gif(prompt, size=384, seconds=1.0, width=360, fps=8, colors=128, seed=None)` | meme/reaction preset: LTX render, then a small looping GIF returned inline. |

The image tools return the saved path(s) under the ComfyUI output directory
**and** the image itself as MCP image content, so the agent can see the result.
`generate_video` returns the MP4 path, a `/view` URL, and a poster frame inline.
`generate_video_from_image` does the same and uses the supplied image as the
first frame. `generate_gif` returns the MP4 and GIF paths, a GIF URL, and the
GIF inline as `image/gif`.

Video aspect presets:

| `aspect_ratio` | default draft | default final |
|---|---|---|
| `"1:1"` (unchanged default) | 512×512 | 1024×1024 |
| `"16:9"` | 512×288 | 1024×576 |
| `"9:16"` | 288×512 | 576×1024 |

`size` sets the longest edge (256..1280, divisible by 64). The short edge is
rounded to 32 pixels for draft or 64 for staged final, so custom sizes may only
approximate the requested ratio. I2V fits the source to the chosen dimensions;
it does not infer the aspect ratio automatically. GIF sizing stays unchanged.

Renders are limited to ~1 MP. The official template's 1280×720 geometry is
`size=1280` with `aspect_ratio="16:9"` (1280×704 after VAE alignment); square
tops out at 1024×1024. A 1280×1280 request would be 1.6 MP and is refused.

`seconds` accepts 0.5..10.0 and snaps to the nearest valid LTX length
(`8n+1` frames at 24 fps; 10 s is 241 frames). Above 6 s, draft requests
larger than 512×512 are refused so the fast path stays fast; final keeps its
full size. The 22B DiT already fills the 16 GB card at 49 frames, so longer
clips offload more layers rather than fitting more activations. The 10 s
ceiling is our own cap, not a model limit (LTX's duration head defaults to
20 s), but nothing here has been validated beyond 241 frames.

### Configuration vs the official template

The pipeline follows the official LTX-2.5 template: euler_ancestral, an 8-step
base pass, ×2 latent upscale, a 3-step refine, CFG 1/1, the HQ VAE, and
512/64/64/16 tiled decode. I2V follows the official I2V template as well:
source resize to a 1536 longer edge (lanczos), `LTXVPreprocess` compression 18,
`LTXVImgToVideoInplace` at strength 0.7 on the base and 1.0 after the upscale,
HQ VAE for both encoding and decoding. Only these deviations remain:

- Conditioning is encoded once and cached, then loaded with
  `ConditioningLoader`. The 12.8 GB text encoder and 21.5 GB DiT cannot share
  host RAM.
- Each phase runs as a pruned graph with a `/recycle` between phases, for the
  same reason.
- Renders are limited to ~1 MP, the largest size validated here.
- Drafts are our own shortcut: the official base pass at 512×512 without the
  refine stage, for speed.

The official 1280×704 final at 10 s is the largest validated render here.
Drafts stay at 512×512 above 6 s because 768×768 at 10 s tripped the 1 GiB
guard (1022 MB).

OWUI also runs the optional native
[ComfyUI attachment filter](../webui/openwebui-extensions/comfyui-media/README.md).
It copies generated MP4s into owned chat storage and adds a playable inline
player automatically. Standard MCP clients still receive the same poster/URL
results; no MP4 base64 is added.

Video/GIF outputs and posters are saved under **`output/mcp-video/`**;
conditioning intermediates and staged latents use its `conditioning/` and
`staged/` subfolders. Existing experiment files/links are left untouched.
Large inline stills/posters are reduced to a PNG preview (alpha preserved)
within a 700,000-byte budget; the full-resolution file is unchanged. Oversized
GIFs remain downloadable and return a bounded poster instead of breaking MCP.

## Wiring

The server runs in the `comfyui-mcp` service in `webui/compose.yml`:

| client | URL |
|---|---|
| opencode (and other tailnet devices) | `https://<tailnet-host>:8444/mcp` |
| Open WebUI | `http://comfyui-mcp:8000/mcp` (type `mcp`) |
| local debugging | `http://127.0.0.1:8011/mcp` |

The Tailscale Serve mapping is tailnet-only and proxies to
`http://127.0.0.1:8011`; nothing is exposed on the LAN. The container publishes
only `127.0.0.1:8011:8000` on the host and joins the `webui_default` Docker
network.

Media links use `COMFYUI_PUBLIC_URL` (defaults to `COMFYUI_URL`, currently
`http://192.168.0.46:8189`). This is reachable by the user's LAN devices;
no additional Tailscale media mapping is needed.

The container mounts only:

```text
/opt/stacks/comfyui-mcp                          -> /app:ro
/opt/stacks/webui/workflows                     -> /comfyui/workflows:ro
/opt/stacks/comfyui/models/embeddings            -> /comfyui/embeddings:rw
/opt/stacks/comfyui/input                        -> /comfyui/input:rw
/opt/stacks/comfyui/output                       -> /comfyui/output:rw
```

There is deliberately no `/opt/stacks` mount. Put new image/edit/I2V inputs
in `/opt/stacks/comfyui/input/`. The server accepts that host path or
`/comfyui/input/<name>` and reuses the file instead of uploading. Returned host
output paths also work as edit/I2V inputs: only the known input/output roots
are translated into their narrow mounts.

Environment:

| var | default |
|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8189` (the proxy) |
| `COMFYUI_PUBLIC_URL` | `$COMFYUI_URL` (used for `/view` links) |
| `COMFYUI_WORKFLOWS_DIR` | `/opt/stacks/webui/workflows` |
| `COMFYUI_VIDEO_WORKFLOW` | `comfyui-mcp/workflows/ltx25_distilled_t2v_api.json` |
| `COMFYUI_OUTPUT_DIR` | `/opt/stacks/comfyui/output` |
| `COMFYUI_OUTPUT_HOST_DIR` | `$COMFYUI_OUTPUT_DIR` |
| `COMFYUI_MODELS_DIR` | `/opt/stacks/comfyui/models` |
| `COMFYUI_EMBEDDINGS_DIR` | `$COMFYUI_MODELS_DIR/embeddings` |
| `COMFYUI_INPUT_DIR` | `/opt/stacks/comfyui/input` |
| `COMFYUI_INPUT_HOST_DIR` | `/opt/stacks/comfyui/input` (path translation only) |
| `COMFYUI_POLL_TIMEOUT` | `900` seconds |
| `COMFYUI_MCP_TRANSPORT` | `stdio`; the shared container sets `streamable-http` |
| `COMFYUI_MCP_HOST` | `0.0.0.0` |
| `COMFYUI_MCP_PORT` | `8000` |

The shared container sets `COMFYUI_WORKFLOWS_DIR=/comfyui/workflows` to use
the narrow read-only workflow mount.

## Lifecycle and memory

`/free` unloads VRAM but leaves the encoder's host RAM in the ComfyUI process.
On this 31 GB host the 21.5 GB video DiT cannot load after the 12.8 GB text
encoder, so the server uses the proxy's `POST /recycle` between model families:

- new prompt: recycle the previous video process, load the text encoder and
  cache the conditioning, recycle again, then load the video DiT;
- cached prompt: recycle once before the video;
- failed job: interrupt only if that job is running, delete only its pending
  queue entry, then call `/recycle` with the prompt id. The proxy force-stops
  only when the queue contains no other client's
  prompt; otherwise it still returns 409.

Image/edit/video/GIF jobs share a nonblocking lock in this server, preventing
image calls from interleaving with staged-video recycle boundaries. Other
ComfyUI clients still have their own queues; `/recycle` refuses busy queues.

`/view` is proxied while ComfyUI is running and served from the mounted output
directory while it is stopped, so video/GIF links keep working after the GPU
idles out. Only `type=output` files under the output root are served.

## Workflows

It runs the committed API workflows in `webui/workflows/`, finding nodes by
`class_type` rather than hardcoded ids — so edits to those files (steps,
sampler, model filenames) are picked up with no change here. Only `cfg` is left
alone at `1`, the model's official path, where the negative prompt is ignored.

| path | when |
|---|---|
| `qwen_image_2_1_turbo_t2i_api.json` | `generate_image()` |
| `qwen_image_2_1_turbo_edit_api.json` | `edit_image()` |
| `comfyui-mcp/workflows/ltx25_distilled_t2v_api.json` | `generate_video()`, `generate_video_from_image()`, `generate_gif()` |

The image workflows run the Turbo UNet (`qwen_image_2.1_turbo_int8_convrot`) on
the checkpoint's own 8-step sigma schedule (`ManualSigmas` -> `SamplerCustom`);
the base-model workflows (`qwen_image_2_1_{t2i,edit}_api.json`) retain their base
UNet and 40-step sampler for rollback but are not selected by the MCP. Because
the step count is fixed by `ManualSigmas`, the tools' `steps` argument is
ignored.

Edit workflows restore each reference's alpha with `JoinImageWithAlpha` before
Qwen encoding. Unused reference loaders and their alpha joins are removed by the
proxy. The 1536 edit resolution budget (~2.36 MP) is the active Turbo default,
with the output aspect ratio following the first reference. The base-model
rollback workflow retains its 1024 budget.

Prompts are passed through unchanged. The optional Prompt Enhancer and its
workflow templates were removed: live tests showed it rewriting explicit alpha
requests into white/checkerboard backgrounds, as well as adding latency and
memory pressure. The tools no longer accept an `enhance` argument. Previously
downloaded PE models/custom nodes are unused and have not been deleted.

The video workflow is the official LTX-2.5 distilled 8-step recipe. `draft` is
one 512-long-edge base pass; `final` is the staged pipeline (half-size base, 2× latent
upscale, three refinement steps, HQ VAE decode), 1024 square by default or the
official 1280×704 with `size=1280` and `aspect_ratio="16:9"`. Image-to-video
follows the official I2V template: the source is scaled to a 1536 longer edge
(lanczos), preprocessed at compression 18, and applied with
`LTXVImgToVideoInplace` — strength 0.7 on the base pass, then 1.0 again after
the latent upscale so the refine stage re-anchors to the source. Both encode
with the HQ VAE. This is first-frame conditioning, not Qwen-style
multi-reference identity editing; LTX has no identity-reference encoder.

### Dialogue and audio

LTX generates the soundtrack in the same pass as the video. For spoken
dialogue, quote the exact line, name the language and accent, keep it to one
speaker and a short line, and add "no music" so the voice stays clear:

> A close-up of a woman speaking directly to camera in a warm American
> accent. She says, "Hello, this is a test of spoken dialogue." Her lips
> and expression stay in sync with every word. Soft key light, quiet room
> tone, no music.

A 6 s 576×1024 final with seed 42 produced that line verbatim (local
Whisper transcription) with coherent mouth movement. This is prompt-driven,
not a TTS: longer lines, other accents, and different seeds can drop or blur
words. For guaranteed wording, use a recorded/TTS track with LTX's
audio-driven lip-sync, which this tool does not expose yet.

## Timing

The first call after ComfyUI has idled out takes about a minute (container
start plus model load). Measured on the RX 9060 XT / 31 GB host:

- `generate_image()` at 2048×2048, warm: ~60 s (1024×1024 is ~25–55 s)
- `edit_image()` at a 1024 resolution budget: ~90 s for one image; multi-reference
  edits cost more (two-reference smoke test: ~168 s)
- `generate_video()` draft, 512×512: ~2 min at 4 s, ~2–3 min at 10 s
  (cached prompt; runs include the cold start, so expect ±1 min)
- `generate_video()` final, 1024×576, 10 s: ~6 min
- `generate_video()` final, 1280×704 (official geometry), 10 s: ~7 min,
  15.60 GiB peak VRAM, 3977 MiB free host RAM
- `generate_video()` final, 1024×1024, 10 s: ~8 min
- `generate_video_from_image()` draft, 512×288, 145 frames (6 s), new prompt:
  165.7 s; peak VRAM 15.69 GiB, 2043 MiB free host RAM. First-frame MAE 7.06
  against the source: 0.7 is the official base strength, so a draft can drift
  slightly; the staged final re-applies the source at 1.0 after the upscale.
- `generate_video_from_image()` final, 1024×576, 145 frames (6 s), cached
  prompt: 333.1 s (base 103.6 s + upscale 7.7 s + refine 94.8 s + decode
  42.9 s); peak VRAM 15.63 GiB, 2052 MiB free host RAM. First-frame MAE 4.70
  against the source: the refine pass re-applies it at strength 1.0.
- `generate_gif()` 384×384 render, 25 generated frames, cached prompt: 60.8 s
  (shared HTTP MCP retest: 66.5 s, inline GIF 856,468 base64 characters)
- Shared HTTP MCP `generate_image()` at 512×512: 36.3 s from cold.
- Shared HTTP MCP `edit_image()` at the 1536 budget: 225.6 s from cold;
  host output path reused successfully as input, 2.9 MB full PNG retained,
  inline preview 595,692 base64 characters.
- Landscape shared HTTP retest, cached prompt, 25 frames with audio:
  draft 512×288 116.2 s; staged final 1024×576 270.3 s. All decoded at the
  expected dimensions. Portrait presets have offline coverage only.

## GIF size and the SSE limit

MCP clients cap a single SSE event at 1 MB (the Python client used here does).
Inline images are base64, so a 1 MB GIF becomes ~1.33 MB on the wire and fails.
The GIF preset is therefore tuned to **360 px, 9 frames at 8 fps, 128 colors**:
about 573 KiB on disk / ~764 KiB base64. Raising `width`, `fps`, or `colors`
makes a sharper GIF but can exceed the limit; check the base64 size before
changing the defaults. The GIF has no audio and loops forever.

## Offline regression tests

From the repository root (stdlib only; no GPU, server, or MCP SDK needed):

```sh
python3 -B -m unittest discover -s comfyui-mcp -p 'test_*.py' -v
python3 -B -m unittest discover -s litellm/comfyui_proxy -p 'test_*.py' -v
```

Checks cover all four remaining base/Turbo image templates; model roles and
sampling; square/non-square generation; and 1, 2, or 10 edit images through the
MCP builder and positional WebUI upload wiring, including alpha preservation and
cleanup. Video tests cover draft/staged I2V links, all four staged phases,
output prefixes, scoped latent cleanup, safe cancellation, bounded alpha
previews, and image/video exclusion. These are not a substitute for live tests.

## Relation to the official server

Comfy-Org's `comfy-mcp` also works against this proxy, but it is built on
`comfy-cli` (a host Python tool plus a workspace concept), is AGPL, and its
lifecycle/model/template tools target a *local* ComfyUI install — misleading
next to a container whose lifecycle is owned by `comfyui-proxy`. This server is
deliberately narrow: the workflows we actually use, plus status.

(Comfy-cli's liveness check — `GET /history` expecting 200 — is why the proxy
answers that one read route with `200 {}` while ComfyUI is cold, instead of
`503`. That keeps status reads from starting the GPU.)
