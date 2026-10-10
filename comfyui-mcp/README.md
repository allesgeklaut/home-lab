# ComfyUI MCP server

A small stdio MCP server so opencode can generate and edit images with the
local ComfyUI (Qwen-Image-2.1-Turbo).

It talks to the **comfyui-proxy** in the litellm stack rather than to ComfyUI
directly, so the on-demand lifecycle is handled for it: every mutating request
wakes the `comfyui` container, and the proxy stops it again when idle. There is
no lifecycle logic here.

## Tools

| tool | what it does |
|---|---|
| `image_status` | proxy/ComfyUI state and the available diffusion models. Uses the proxy's cached `object_info`, so it never starts the GPU. |
| `generate_image(prompt, negative_prompt, width=1024, height=1024, steps=40, seed=None, enhance=False)` | text to image on the Turbo UNet (official 8-step schedule; `steps` is ignored). `enhance=True` (opt-in) runs the Qwen-Image-2.1 Prompt Enhancer first. |
| `edit_image(image_path, prompt, steps=40, seed=None, enhance=False)` | instruction edit of a local image file on Turbo. `enhance=True` (opt-in) rewrites the instruction with PE-I2I, which sees the input image. |

Both image tools return the saved path(s) under the ComfyUI output directory
**and** the image itself as MCP image content, so the agent can see the result.

## Wiring

Registered in `/opt/stacks/opencode.json` under `mcp.comfyui`:

```json
"comfyui": {
  "type": "local",
  "command": ["uv", "run", "--with", "mcp>=2,<3", "python", "/opt/stacks/comfyui-mcp/server.py"],
  "timeout": 600000,
  "environment": { "COMFYUI_URL": "http://127.0.0.1:8189" }
}
```

The only dependency is the official `mcp` SDK, resolved on demand by `uv`
(nothing is installed system-wide). The `timeout` is generous because a cold
generation takes over a minute.

Environment:

| var | default |
|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8189` (the proxy) |
| `COMFYUI_WORKFLOWS_DIR` | `/opt/stacks/webui/workflows` |
| `COMFYUI_OUTPUT_DIR` | `/opt/stacks/comfyui/output` |
| `COMFYUI_POLL_TIMEOUT` | `900` seconds |

## Workflows

It runs the committed API workflows in `webui/workflows/`, finding nodes by
`class_type` rather than hardcoded ids — so edits to those files (steps,
sampler, model filenames) are picked up with no change here. Only `cfg` is left
alone at `1`, the model's official path, where the negative prompt is ignored.

| path | when |
|---|---|
| `qwen_image_2_1_turbo_t2i_api.json` | `generate_image(enhance=False)` (default) |
| `qwen_image_2_1_turbo_edit_api.json` | `edit_image(enhance=False)` (default) |
| `qwen_image_2_1_turbo_pe_t2i_api.json` | `generate_image(enhance=True)` |
| `qwen_image_2_1_turbo_pe_edit_api.json` | `edit_image(enhance=True)` |

All four run the Turbo UNet (`qwen_image_2.1_turbo_int8_convrot`) on the
checkpoint's own 8-step sigma schedule (`ManualSigmas` -> `SamplerCustom`); the
base-model workflows (`qwen_image_2_1_{t2i,edit}_api.json` and their `_pe_`
variants) stay on disk for rollback but are no longer referenced. Because the
step count is fixed by `ManualSigmas`, the tools' `steps` argument is ignored.

The PE workflows prepend a `CLIPLoader` (the PE checkpoint, `type=qwen_image`)
and a prompt-rewrite node (`QwenImage21_T2IPromptRewrite` /
`QwenImage21_EditPromptRewrite`, from the
`benjiyaya/ComfyUI-Qwen-Image-2.1-Prompt-Enhancer` custom node) whose
`positive_prompt` feeds `TextEncodeQwenImage21`. A `PreviewAny` node exposes the
rewritten text, which the server reports back in the result note. A second
`PreviewAny` on the rewrite node's `parse_ok` output lets the server detect a
failed rewrite (a refusal, or a truncated output); when that happens it
automatically **retries once with `enhance=False`** and says so in the note, so a
refused/odd prompt still yields an image from the raw text instead of garbage.
The PE runs on the GPU and is evicted before generation
(`--disable-smart-memory`), so it does not need to be resident with the UNet.

## Timing

The first call after ComfyUI has idled out takes about a minute (container
start plus model load). Turbo sampling itself is ~8 s; the rest is model
loading and, when enabled, the enhancer. Measured at 1024x1024:

- `generate_image(enhance=False)`, warm: ~25-55 s
- `edit_image(enhance=False)`: ~90 s (the reference image goes through the text encoder too)
- `generate_image(enhance=True)`: ~140-170 s (includes the ~70-100 s rewrite)
- `edit_image(enhance=True)`: ~340 s (the PE-I2I reads the input image)

The enhancer is the dominant cost. It is a Qwen3.5-9B autoregressive decode that
is memory-bandwidth bound, and it is charged ~1.5-2x extra because the custom
node runs it with `thinking=True` (a chain-of-thought that is generated and then
discarded). The rewritten prompt is deterministic for a given `seed` (it drives
both the rewrite and the sampler).

## Relation to the official server

Comfy-Org's `comfy-mcp` also works against this proxy, but it is built on
`comfy-cli` (a host Python tool plus a workspace concept), is AGPL, and its
lifecycle/model/template tools target a *local* ComfyUI install — misleading
next to a container whose lifecycle is owned by `comfyui-proxy`. This server is
deliberately narrow: the two workflows we actually use, plus status.

(Comfy-cli's liveness check — `GET /history` expecting 200 — is why the proxy
answers that one read route with `200 {}` while ComfyUI is cold, instead of
`503`. That keeps status reads from starting the GPU.)
