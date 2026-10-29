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
| `generate_image(prompt, negative_prompt, width=2048, height=2048, steps=40, seed=None)` | text to image on the Turbo UNet (official 8-step schedule; `steps` is ignored). |
| `edit_image(image_path, prompt, steps=40, seed=None, reference_images=None)` | instruction edit on Turbo, with up to nine extra reference paths. |

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
| `qwen_image_2_1_turbo_t2i_api.json` | `generate_image()` |
| `qwen_image_2_1_turbo_edit_api.json` | `edit_image()` |

Both run the Turbo UNet (`qwen_image_2.1_turbo_int8_convrot`) on the
checkpoint's own 8-step sigma schedule (`ManualSigmas` -> `SamplerCustom`); the
base-model workflows (`qwen_image_2_1_{t2i,edit}_api.json`) retain their base UNet
and 40-step sampler for rollback but are not selected by the MCP. Because the
step count is fixed by `ManualSigmas`, the tools' `steps` argument is ignored.

Edit workflows restore each reference's alpha with `JoinImageWithAlpha` before
Qwen encoding. Unused reference loaders and their alpha joins are removed by the proxy. The
1536 edit resolution budget (~2.36 MP) is the active Turbo default, with the
output aspect ratio following the first reference. The base-model rollback
workflow retains its 1024 budget.

Prompts are passed through unchanged. The optional Prompt Enhancer and its
workflow templates were removed: live tests showed it rewriting explicit alpha
requests into white/checkerboard backgrounds, as well as adding latency and
memory pressure. The tools no longer accept an `enhance` argument. Previously
downloaded PE models/custom nodes are unused and have not been deleted.

## Timing

The first call after ComfyUI has idled out takes about a minute (container
start plus model load). Generation defaults to 2048x2048; editing uses about
1536x1536 pixels while preserving the first reference's aspect ratio.
Measured before the edit-budget increase (1536 edit timings are not yet measured):

- `generate_image()` at the default 2048x2048, warm: ~60 s (pass `width`/`height` to override; 1024x1024 is ~25-55 s)
- `edit_image()` at a 1024 resolution budget: ~90 s for one image; multi-reference
  edits cost more (two-reference smoke test: ~168 s).

## Offline regression tests

From the repository root (stdlib only; no GPU, server, or MCP SDK needed):

```sh
python3 -B -m unittest discover -s comfyui-mcp -p 'test_workflows.py' -v
python3 -B -m unittest discover -s litellm/comfyui_proxy -p 'test_prompt_clean.py' -v
```

Checks cover all four remaining base/Turbo templates; model roles and sampling;
square/non-square generation; and 1, 2, or 10 edit images through the MCP builder
and positional WebUI upload wiring, including alpha preservation and cleanup.
These are graph checks, not a substitute for live generation tests.

## Relation to the official server

Comfy-Org's `comfy-mcp` also works against this proxy, but it is built on
`comfy-cli` (a host Python tool plus a workspace concept), is AGPL, and its
lifecycle/model/template tools target a *local* ComfyUI install — misleading
next to a container whose lifecycle is owned by `comfyui-proxy`. This server is
deliberately narrow: the two workflows we actually use, plus status.

(Comfy-cli's liveness check — `GET /history` expecting 200 — is why the proxy
answers that one read route with `200 {}` while ComfyUI is cold, instead of
`503`. That keeps status reads from starting the GPU.)
